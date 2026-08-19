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
            features = self._provision_features(asset_uid, asset, cfg, features)
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

    def _provision_features(self, asset_uid: str, asset: dict, cfg: AssetConfig,
                            features: S.AssetFeatures) -> S.AssetFeatures:
        """Give transcription to any recording on this form that has none.

        Previously this ran only when the form had *no* configuration at all,
        which meant a recording added to a form later was ignored for ever:
        the form as a whole looked configured, so nothing ever looked at the
        new question. Now each recording is judged on its own.

        Only recordings with nothing configured are touched -- an existing
        setting is never overwritten, which matters more than usual here
        because the rows are append-only and a wrong language cannot be taken
        back. Analysis questions are not invented either; they carry uuids the
        server owns.
        """
        wanted = cfg.xpaths or P.media_question_xpaths(asset)
        missing = [x for x in wanted if x not in features.transcribe]
        if not missing:
            return features
        if self.s.dry_run:
            log.info("[DRY RUN] would configure transcription on %s for %s",
                     asset_uid, missing)
            return features

        for xpath in missing:
            self.client.create_advanced_feature(
                asset_uid, question_xpath=xpath, action=S.ACTION_TRANSCRIBE,
                params=[{"language": S.split_language(cfg.transcript_language)[0]}],
            )
            targets, _same = S.usable_targets(cfg.transcript_language,
                                              cfg.translation_languages)
            if targets:
                self.client.create_advanced_feature(
                    asset_uid, question_xpath=xpath, action=S.ACTION_TRANSLATE,
                    params=[{"language": S.split_language(l)[0]} for l in targets],
                )
        log.info("Configured transcription on %s for %s (%s already had it)",
                 asset_uid, missing, len(features.transcribe))
        return S.AssetFeatures(self.client.list_advanced_features(asset_uid))

    # -- job driver ---------------------------------------------------------
    def process(self, asset_uid: str, submission_uuid: str, stage: str, attempts: int,
                failures: int = 0, created_at: float = 0.0) -> None:
        give_up = self._give_up(attempts, failures, created_at)
        if give_up:
            self.store.advance(asset_uid, submission_uuid, STAGE_FAILED,
                               error=give_up, note=give_up)
            log.error("[%s/%s] %s", asset_uid, submission_uuid, give_up)
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
            handler = self._handler_for(stage, new_api)
            next_stage, delay, note = self._run_stage(ctx, submission_uuid, supplement,
                                                       stage, handler, new_api)

            self.store.advance(asset_uid, submission_uuid, next_stage,
                               delay=delay, error=None, bump_attempts=True, note=note,
                               failure=False)
            log.info("[%s/%s] pass %s: %s", asset_uid, submission_uuid,
                     attempts + 1, note or f"-> {next_stage}")

        except KoboError as exc:
            if exc.status == 404:
                # Kobo has no submission with this uuid, so no amount of
                # retrying will help. Park it with a note that says why.
                self.store.advance(
                    asset_uid, submission_uuid, STAGE_FAILED, error=str(exc)[:1000],
                    note="no submission with this uuid on the form — stale queue entry",
                    failure=True)
                log.error("[%s/%s] no such submission in Kobo; giving up",
                          asset_uid, submission_uuid)
                return
            backoff = min(600, 30 * (2 ** min(attempts, 5)))
            log.warning("[%s/%s] %s -- retrying in %ss", asset_uid, submission_uuid, exc, backoff)
            self.store.advance(asset_uid, submission_uuid, stage or STAGE_NEW,
                               delay=backoff, error=str(exc)[:1000], bump_attempts=True,
                               failure=True)
        except Exception as exc:  # noqa: BLE001
            log.exception("[%s/%s] unexpected error", asset_uid, submission_uuid)
            self.store.advance(asset_uid, submission_uuid, stage or STAGE_NEW,
                               delay=300, error=repr(exc)[:1000], bump_attempts=True,
                               failure=True)

    # A stage that finishes with nothing outstanding hands straight on to the
    # next one. Doing that in a fresh pass meant a whole round trip -- and a
    # pass in the count -- spent doing nothing but changing a word in a
    # database row.
    CHAIN_LIMIT = 4

    def _run_stage(self, ctx: AssetContext, uuid: str, supplement: dict, stage: str,
                   handler, new_api: bool) -> tuple[str, float, str]:
        notes: list[str] = []
        for _ in range(self.CHAIN_LIMIT):
            result = (handler(ctx, uuid, supplement)
                      if handler else (STAGE_DONE, 0.0, "nothing to do"))
            # The older dialects' handlers return (stage, delay) only.
            next_stage, delay, note = result if len(result) == 3 else (*result, "")
            if note:
                notes.append(note)
            nxt = self._handler_for(next_stage, new_api)
            if not new_api or next_stage == stage or nxt is None or delay > 5:
                break
            # Only a hand-off is worth chaining: anything waiting on Kobo has
            # to come back later regardless.
            stage, handler = next_stage, nxt
            supplement = self.client.get_data_supplement(ctx.uid, uuid)
        return next_stage, delay, " → ".join(notes)

    def _handler_for(self, stage: str, new_api: bool):
        if stage in (STAGE_NEW, STAGE_TRANSCRIBE):
            return self._sup_transcribe if new_api else self._do_transcribe
        if stage == STAGE_TRANSLATE:
            return self._sup_translate if new_api else self._do_translate
        if stage == STAGE_QUAL:
            return self._sup_qual if new_api else self._do_qual
        return None

    def _give_up(self, attempts: int, failures: int, created_at: float) -> str | None:
        """Why this submission should stop, or None to keep going.

        Passes are deliberately not the measure. Kobo's NLP is asynchronous, so
        most passes are a poll, and a slow transcription used to exhaust the
        budget and be reported as a failure. What matters is how many times
        something actually went wrong, and how long it has been unresolved.
        """
        if failures >= self.s.max_failures:
            return f"gave up after {failures} errors"
        if created_at:
            hours = (time.time() - created_at) / 3600
            if hours >= self.s.max_job_age_hours:
                return f"still unfinished after {int(hours)}h"
        if attempts >= self.s.max_attempts:
            return f"gave up after {attempts} passes"
        return None

    # -- stages: current API (supplement dialect) ---------------------------
    def _sup_transcribe(self, ctx: AssetContext, uuid: str, sup: dict) -> tuple[str, float, str]:
        requested = accepted = waiting = ready = stalled = 0
        longest = 0.0
        empty: list[str] = []
        broken: list[str] = []
        for xpath, row_language in (ctx.features.transcribe if ctx.features else {}).items():
            # The row can only name a base language; this form's settings may
            # name the regional variant. The request needs both.
            language = S.request_language(ctx.cfg.transcript_language, row_language)
            status, _text = S.transcript_state(sup, xpath)
            if S.is_done(status):
                # A "successful" transcription of silence returns an empty
                # string. Accepting it would feed an empty document to
                # translation, which rejects it on every single attempt.
                if S.transcript_is_empty(sup, xpath):
                    empty.append(xpath)
                    continue
                # A finished transcript is not usable downstream until it is
                # accepted; translation 400s against an unaccepted one.
                if not S.transcript_accepted(sup, xpath):
                    self._patch(ctx.uid, uuid, S.accept_transcript_body(xpath, language),
                                f"accept transcript {xpath} [{language}]")
                    accepted += 1
                else:
                    ready += 1
                continue
            node = S.transcript_node(sup, xpath)
            if S.is_pending(status):
                running = S.stalled_for(node)
                longest = max(longest, running)
                if running <= self.s.nlp_stall_seconds:
                    waiting += 1
                    continue
                # Asking again does clear this: a request against a job that
                # has been abandoned produces a fresh version, which completes.
                # It is the only lever there is -- the server ignores a request
                # while one is genuinely running, and `value: null` refuses
                # with "Attempt to delete non-existent value" until something
                # has finished -- so keep using it rather than giving up. The
                # backstop is MAX_JOB_AGE_HOURS, not a guess made here.
                log.warning("[%s/%s] transcription of %s has been running %.0f min; "
                            "asking again", ctx.uid, uuid, xpath, running / 60)
                stalled += 1
            if S.is_failed(status):
                if S.failure_streak(node) >= S.MAX_ACTION_FAILURES:
                    broken.append(f"{xpath}: {S.error_text(node) or 'transcription failed'}")
                    continue
            self._patch(ctx.uid, uuid, S.transcribe_body(xpath, language),
                        f"transcribe {xpath} [{language}]")
            requested += 1

        if requested or accepted or waiting:
            delay = S.poll_delay(self.s.async_poll_seconds, longest)
            note = _note("transcription", requested=requested, accepted=accepted,
                         waiting=waiting, delay=delay)
            if stalled:
                note = f"{note} (restarted {stalled} stalled job(s))"
            return STAGE_TRANSCRIBE, delay, note

        if not ready:
            # Nothing usable came back, and nothing is still running.
            if empty:
                return STAGE_FAILED, 0.0, (
                    f"transcription returned no speech for {', '.join(empty)} — check "
                    f"the recording has audible speech and that the audio language "
                    f"matches what was spoken")
            if broken:
                return STAGE_FAILED, 0.0, "transcription failed — " + "; ".join(broken)[:400]
            return STAGE_DONE, 0.0, "nothing to transcribe on this submission"

        nxt = STAGE_TRANSLATE if (ctx.features and ctx.features.translate) else STAGE_QUAL
        skipped = f", {len(empty)} empty and skipped" if empty else ""
        return nxt, 2.0, f"{ready} transcript(s) ready{skipped}, moving to {nxt}"

    def _sup_translate(self, ctx: AssetContext, uuid: str, sup: dict) -> tuple[str, float, str]:
        requested = accepted = waiting = blocked = 0
        broken: list[str] = []
        impossible: list[str] = []
        for xpath, configured in (ctx.features.translate if ctx.features else {}).items():
            # Advanced-features rows are append-only on current kpi builds --
            # no DELETE, and PUT/PATCH merge rather than replace -- so a target
            # that equals the source language cannot be removed once saved. It
            # has to be skipped here instead, or Google rejects it forever.
            languages, same = S.usable_targets(
                (ctx.features.transcribe if ctx.features else {}).get(xpath, ""), configured)
            impossible.extend(f"{xpath} -> {l}" for l in same)
            # An empty transcript has nothing to translate; asking anyway earns
            # a 400 per language, per pass, indefinitely.
            if S.transcript_is_empty(sup, xpath):
                continue
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
                node = S.translation_node(sup, xpath, language)
                if S.is_pending(status):
                    if not S.is_stalled(node, self.s.nlp_stall_seconds):
                        waiting += 1
                        continue
                    log.warning("[%s/%s] translation of %s to %s stalled for %.0f min; "
                                "re-requesting", ctx.uid, uuid, xpath, language,
                                S.stalled_for(node) / 60)
                if S.is_failed(status):
                    if S.failure_streak(node) >= S.MAX_ACTION_FAILURES:
                        broken.append(f"{language}: "
                                      f"{S.error_text(node) or 'translation failed'}")
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
        if broken:
            # Analysis reads the transcript, not the translation, so a dead
            # translation is worth reporting but not worth stopping for.
            return STAGE_QUAL, 2.0, "translation gave up — " + "; ".join(broken)[:400]
        if impossible:
            return STAGE_QUAL, 2.0, (
                f"skipped {', '.join(impossible)} (same language as the audio), "
                f"moving to analysis")
        return STAGE_QUAL, 2.0, "translations ready, moving to analysis"

    def _sup_qual(self, ctx: AssetContext, uuid: str, sup: dict) -> tuple[str, float, str]:
        if not ctx.cfg.enable_qual:
            return STAGE_DONE, 0.0, "analysis disabled for this form"
        requested = waiting = answered = skipped = 0
        broken: list[str] = []
        for xpath in (ctx.features.qual if ctx.features else {}):
            if S.transcript_is_empty(sup, xpath) or not S.transcript_accepted(sup, xpath):
                skipped += 1
                continue
            for question_uuid in ctx.features.answerable_qual(xpath):
                status = S.qual_state(sup, xpath, question_uuid)
                if S.is_done(status):
                    answered += 1
                    continue
                node = S.qual_node(sup, xpath, question_uuid)
                if S.is_pending(status):
                    if not S.is_stalled(node, self.s.nlp_stall_seconds):
                        waiting += 1
                        continue
                    log.warning("[%s/%s] analysis %s stalled for %.0f min; re-requesting",
                                ctx.uid, uuid, question_uuid[:8], S.stalled_for(node) / 60)
                if S.is_failed(status):
                    if S.failure_streak(node) >= S.MAX_ACTION_FAILURES:
                        broken.append(f"{question_uuid[:8]}: "
                                      f"{S.error_text(node) or 'analysis failed'}")
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
        if broken:
            return STAGE_FAILED, 0.0, "analysis gave up — " + "; ".join(broken)[:400]
        if not answered and skipped:
            return STAGE_FAILED, 0.0, (
                f"no usable transcript on {skipped} recording(s), so nothing could "
                f"be analysed")
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
