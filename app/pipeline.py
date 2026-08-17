"""Per-submission state machine: transcribe -> translate -> qual -> done."""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from .assetconf import AssetConfig, resolve as resolve_asset_config
from .config import Settings
from .kobo import KoboClient, KoboError
from . import payloads as P
from . import supplement as S
from .store import (
    Store, STAGE_NEW, STAGE_TRANSCRIBE, STAGE_TRANSLATE, STAGE_QUAL,
    STAGE_DONE, STAGE_FAILED,
)

log = logging.getLogger(__name__)


def _note(what: str, *, requested: int = 0, accepted: int = 0, waiting: int = 0,
          delay: float = 0.0) -> str:
    """One line explaining why a pass ended without finishing.

    Kobo's NLP is asynchronous, so most passes are a poll rather than a retry.
    Saying so is the difference between "8 attempts" reading as trouble and
    reading as normal progress.
    """
    bits = []
    if requested:
        bits.append(f"requested {requested}")
    if accepted:
        bits.append(f"accepted {accepted}")
    if waiting:
        bits.append(f"{waiting} still running")
    return f"{what}: {', '.join(bits) or 'nothing to do'} — re-checking in {int(delay)}s"


@dataclass
class AssetContext:
    uid: str
    xpaths: list[str]
    qual_survey: list[dict]
    dialect: str
    cfg: AssetConfig
    fetched_at: float
    # Only set for the `supplement` dialect: what the server has configured.
    features: S.AssetFeatures | None = None


class Pipeline:
    """Advances jobs one stage per call. Never blocks on Kobo's async NLP."""

    ASSET_CACHE_TTL = 300

    def __init__(self, settings: Settings, client: KoboClient, store: Store) -> None:
        self.s = settings
        self.client = client
        self.store = store
        self._assets: dict[str, AssetContext] = {}

    # -- asset context ------------------------------------------------------
    def asset_context(self, asset_uid: str) -> AssetContext:
        ctx = self._assets.get(asset_uid)
        if ctx and (time.time() - ctx.fetched_at) < self.ASSET_CACHE_TTL:
            return ctx

        cfg = resolve_asset_config(self.s, self.store, asset_uid)
        asset = self.client.get_asset(asset_uid)
        advanced = asset.get("advanced_features") or {}
        xpaths = cfg.xpaths or P.configured_xpaths(advanced, P.media_question_xpaths(asset))

        dialect = cfg.schema_dialect
        features: S.AssetFeatures | None = None

        # Current kpi builds expose /advanced-features/ and have dropped the
        # advanced_submission_* endpoints entirely, so probe for it first --
        # it is a cheap, definitive answer where schema sniffing is a guess.
        if dialect in ("auto", P.SUPPLEMENT):
            try:
                features = S.AssetFeatures(self.client.list_advanced_features(asset_uid))
                dialect = P.SUPPLEMENT
            except KoboError as exc:
                if dialect == P.SUPPLEMENT:
                    raise
                log.debug("No advanced-features endpoint on %s (%s)", asset_uid, exc.status)
                features = None

        if dialect == "auto":
            try:
                schema = self.client.get_advanced_submission_schema(asset_uid)
            except KoboError as exc:
                log.warning("Schema introspection failed for %s: %s", asset_uid, exc)
                schema = None
            dialect = P.detect_dialect(schema)

        if features is not None:
            if not features:
                features = self._provision_features(asset_uid, asset, cfg)
            xpaths = [x for x in features.xpaths if not cfg.xpaths or x in cfg.xpaths]
            log.info("Asset %s: supplement API, %s", asset_uid, features.describe())
        else:
            log.info("Asset %s: using %r payload dialect", asset_uid, dialect)

        ctx = AssetContext(
            uid=asset_uid,
            xpaths=xpaths,
            qual_survey=P.qual_survey(advanced),
            dialect=dialect,
            cfg=cfg,
            fetched_at=time.time(),
            features=features,
        )
        self._assets[asset_uid] = ctx
        return ctx

    def _provision_features(self, asset_uid: str, asset: dict,
                            cfg: AssetConfig) -> S.AssetFeatures:
        """Configure transcription/translation on a form that has none yet.

        Only runs when the server reports an empty advanced-features list, so
        it never disturbs a configuration set up by hand. Qualitative questions
        are not invented here -- they carry uuids the server owns.
        """
        targets = cfg.xpaths or P.media_question_xpaths(asset)
        if not targets:
            return S.AssetFeatures([])
        if self.s.dry_run:
            log.info("[DRY RUN] would configure transcription on %s for %s", asset_uid, targets)
            return S.AssetFeatures([])

        language, _ = S.split_language(cfg.transcript_language)
        for xpath in targets:
            self.client.create_advanced_feature(
                asset_uid, question_xpath=xpath, action=S.ACTION_TRANSCRIBE,
                params=[{"language": language}],
            )
            if cfg.translation_languages:
                self.client.create_advanced_feature(
                    asset_uid, question_xpath=xpath, action=S.ACTION_TRANSLATE,
                    params=[{"language": S.split_language(l)[0]}
                            for l in cfg.translation_languages],
                )
        log.info("Configured transcription on %s for %s", asset_uid, targets)
        return S.AssetFeatures(self.client.list_advanced_features(asset_uid))

    # -- job driver ---------------------------------------------------------
    def process(self, asset_uid: str, submission_uuid: str, stage: str, attempts: int) -> None:
        if attempts >= self.s.max_attempts:
            self.store.advance(asset_uid, submission_uuid, STAGE_FAILED,
                               error=f"gave up after {attempts} attempts")
            log.error("[%s/%s] gave up after %s attempts", asset_uid, submission_uuid, attempts)
            return

        try:
            ctx = self.asset_context(asset_uid)
            if not ctx.cfg.enabled:
                self.store.advance(asset_uid, submission_uuid, stage or STAGE_NEW,
                                   delay=600, error="asset paused in admin UI")
                return
            if not ctx.xpaths:
                self.store.advance(asset_uid, submission_uuid, STAGE_DONE,
                                   error="no transcribable questions on this form")
                return

            if ctx.dialect == P.SUPPLEMENT:
                supplement = self.client.get_data_supplement(asset_uid, submission_uuid)
            else:
                supplement = self.client.get_supplement(asset_uid, submission_uuid)

            new_api = ctx.dialect == P.SUPPLEMENT
            if stage in (STAGE_NEW, STAGE_TRANSCRIBE):
                handler = self._sup_transcribe if new_api else self._do_transcribe
            elif stage == STAGE_TRANSLATE:
                handler = self._sup_translate if new_api else self._do_translate
            elif stage == STAGE_QUAL:
                handler = self._sup_qual if new_api else self._do_qual
            else:
                handler = None
            result = (handler(ctx, submission_uuid, supplement)
                      if handler else (STAGE_DONE, 0.0, "nothing to do"))
            # The older dialects' handlers return (stage, delay) only.
            next_stage, delay, note = result if len(result) == 3 else (*result, "")

            self.store.advance(asset_uid, submission_uuid, next_stage,
                               delay=delay, error=None, bump_attempts=True, note=note)
            log.info("[%s/%s] pass %s: %s", asset_uid, submission_uuid,
                     attempts + 1, note or f"-> {next_stage}")

        except KoboError as exc:
            backoff = min(600, 30 * (2 ** min(attempts, 5)))
            log.warning("[%s/%s] %s -- retrying in %ss", asset_uid, submission_uuid, exc, backoff)
            self.store.advance(asset_uid, submission_uuid, stage or STAGE_NEW,
                               delay=backoff, error=str(exc)[:1000], bump_attempts=True)
        except Exception as exc:  # noqa: BLE001
            log.exception("[%s/%s] unexpected error", asset_uid, submission_uuid)
            self.store.advance(asset_uid, submission_uuid, stage or STAGE_NEW,
                               delay=300, error=repr(exc)[:1000], bump_attempts=True)

    # -- stages: current API (supplement dialect) ---------------------------
    def _sup_transcribe(self, ctx: AssetContext, uuid: str, sup: dict) -> tuple[str, float, str]:
        requested = accepted = waiting = ready = 0
        for xpath, language in (ctx.features.transcribe if ctx.features else {}).items():
            status, _text = S.transcript_state(sup, xpath)
            if S.is_done(status):
                # A finished transcript is not usable downstream until it is
                # accepted; translation 400s against an unaccepted one.
                if not S.transcript_accepted(sup, xpath):
                    self._patch(ctx.uid, uuid, S.accept_transcript_body(xpath, language),
                                f"accept transcript {xpath} [{language}]")
                    accepted += 1
                else:
                    ready += 1
                continue
            if S.is_pending(status):
                waiting += 1
                continue
            self._patch(ctx.uid, uuid, S.transcribe_body(xpath, language),
                        f"transcribe {xpath} [{language}]")
            requested += 1

        if requested or accepted or waiting:
            return STAGE_TRANSCRIBE, self.s.async_poll_seconds, _note(
                "transcription", requested=requested, accepted=accepted,
                waiting=waiting, delay=self.s.async_poll_seconds)
        nxt = STAGE_TRANSLATE if (ctx.features and ctx.features.translate) else STAGE_QUAL
        return nxt, 2.0, f"{ready} transcript(s) ready, moving to {nxt}"

    def _sup_translate(self, ctx: AssetContext, uuid: str, sup: dict) -> tuple[str, float, str]:
        requested = accepted = waiting = blocked = 0
        for xpath, languages in (ctx.features.translate if ctx.features else {}).items():
            # Nothing to translate from until an *accepted* transcript exists.
            if not S.transcript_accepted(sup, xpath):
                blocked += len(languages)
                continue
            for language in languages:
                status = S.translation_state(sup, xpath, language)
                if S.is_done(status):
                    if not S.translation_accepted(sup, xpath, language):
                        self._patch(ctx.uid, uuid,
                                    S.accept_translation_body(xpath, language),
                                    f"accept translation {xpath} [{language}]")
                        accepted += 1
                    continue
                if S.is_pending(status):
                    waiting += 1
                    continue
                self._patch(ctx.uid, uuid, S.translate_body(xpath, language),
                            f"translate {xpath} -> {language}")
                requested += 1

        if requested or accepted or waiting:
            return STAGE_TRANSLATE, self.s.async_poll_seconds, _note(
                "translation", requested=requested, accepted=accepted,
                waiting=waiting, delay=self.s.async_poll_seconds)
        if blocked:
            return STAGE_TRANSCRIBE, 2.0, (
                f"{blocked} translation(s) need an accepted transcript first")
        return STAGE_QUAL, 2.0, "translations ready, moving to analysis"

    def _sup_qual(self, ctx: AssetContext, uuid: str, sup: dict) -> tuple[str, float, str]:
        if not ctx.cfg.enable_qual:
            return STAGE_DONE, 0.0, "analysis disabled for this form"
        requested = waiting = answered = 0
        for xpath in (ctx.features.qual if ctx.features else {}):
            if not S.transcript_accepted(sup, xpath):
                continue
            for question_uuid in ctx.features.answerable_qual(xpath):
                status = S.qual_state(sup, xpath, question_uuid)
                if S.is_done(status):
                    answered += 1
                    continue
                if S.is_pending(status):
                    waiting += 1
                    continue
                # The schema takes one uuid per request, so this is one PATCH
                # per analysis question.
                self._patch(ctx.uid, uuid, S.qual_body(xpath, question_uuid),
                            f"qual {xpath} [{question_uuid[:8]}]")
                requested += 1

        if requested or waiting:
            return STAGE_QUAL, self.s.async_poll_seconds, _note(
                "analysis", requested=requested, waiting=waiting,
                delay=self.s.async_poll_seconds)
        return STAGE_DONE, 0.0, f"complete: {answered} analysis answer(s)"

    def _patch(self, asset_uid: str, root_uuid: str, payload: dict, label: str) -> None:
        if self.s.dry_run:
            log.info("[DRY RUN] %s -> PATCH %s", label, payload)
            return
        log.info("PATCH %s/%s: %s", asset_uid, root_uuid, label)
        self.client.patch_data_supplement(asset_uid, root_uuid, payload)

    # -- stages: older APIs -------------------------------------------------
    def _do_transcribe(self, ctx: AssetContext, uuid: str, supplement: dict) -> tuple[str, float]:
        pending = False
        for xpath in ctx.xpaths:
            status, _text = P.transcript_status(supplement, xpath)
            if status in P.TERMINAL_OK:
                continue
            if status in (P.STATUS_REQUESTED, P.STATUS_IN_PROGRESS):
                pending = True
                continue
            # status is None or error -> (re)request
            lang = ctx.cfg.transcript_language
            payload = P.transcribe_payload(ctx.dialect, uuid, xpath, lang)
            self._post(ctx.uid, payload, f"transcribe {xpath} [{lang}]")
            pending = True

        if pending:
            return STAGE_TRANSCRIBE, self.s.async_poll_seconds
        return (STAGE_TRANSLATE if ctx.cfg.translation_languages else STAGE_QUAL), 2.0

    def _do_translate(self, ctx: AssetContext, uuid: str, supplement: dict) -> tuple[str, float]:
        pending = False
        for xpath in ctx.xpaths:
            t_status, _ = P.transcript_status(supplement, xpath)
            if t_status not in P.TERMINAL_OK:
                continue  # nothing to translate from
            for lang in ctx.cfg.translation_languages:
                status = P.translation_status(supplement, xpath, lang)
                if status in P.TERMINAL_OK:
                    continue
                if status in (P.STATUS_REQUESTED, P.STATUS_IN_PROGRESS):
                    pending = True
                    continue
                payload = P.translate_payload(ctx.dialect, uuid, xpath, lang)
                self._post(ctx.uid, payload, f"translate {xpath} -> {lang}")
                pending = True
                # kpi serialises translation jobs per submission
                time.sleep(1)

        if pending:
            return STAGE_TRANSLATE, self.s.async_poll_seconds
        return STAGE_QUAL, 2.0

    def _do_qual(self, ctx: AssetContext, uuid: str, supplement: dict) -> tuple[str, float]:
        if not ctx.cfg.enable_qual or not ctx.qual_survey:
            return STAGE_DONE, 0.0

        pending = False
        for xpath in ctx.xpaths:
            if P.qual_complete(supplement, xpath, ctx.qual_survey):
                continue
            t_status, _ = P.transcript_status(supplement, xpath)
            if t_status not in P.TERMINAL_OK:
                continue
            payload = P.qual_payload(
                ctx.dialect, uuid, xpath, ctx.qual_survey,
                trigger_key=ctx.cfg.qual_trigger_key,
                source_language=ctx.cfg.qual_source_language,
            )
            self._post(ctx.uid, payload, f"qual {xpath} ({len(ctx.qual_survey)} questions)")
            pending = True

        if pending:
            return STAGE_QUAL, self.s.async_poll_seconds
        return STAGE_DONE, 0.0

    # -- helper -------------------------------------------------------------
    def _post(self, asset_uid: str, payload: dict, label: str) -> None:
        if self.s.dry_run:
            log.info("[DRY RUN] %s -> %s", label, payload)
            return
        log.info("POST %s: %s", asset_uid, label)
        self.client.post_supplement(asset_uid, payload)
