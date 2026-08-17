"""Per-submission state machine: transcribe -> translate -> qual -> done."""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from .assetconf import AssetConfig, resolve as resolve_asset_config
from .config import Settings
from .kobo import KoboClient, KoboError
from . import payloads as P
from .store import (
    Store, STAGE_NEW, STAGE_TRANSCRIBE, STAGE_TRANSLATE, STAGE_QUAL,
    STAGE_DONE, STAGE_FAILED,
)

log = logging.getLogger(__name__)


@dataclass
class AssetContext:
    uid: str
    xpaths: list[str]
    qual_survey: list[dict]
    dialect: str
    cfg: AssetConfig
    fetched_at: float


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
        if dialect == "auto":
            try:
                schema = self.client.get_advanced_submission_schema(asset_uid)
            except KoboError as exc:
                log.warning("Schema introspection failed for %s: %s", asset_uid, exc)
                schema = None
            dialect = P.detect_dialect(schema)
            log.info("Asset %s: using %r payload dialect", asset_uid, dialect)

        ctx = AssetContext(
            uid=asset_uid,
            xpaths=xpaths,
            qual_survey=P.qual_survey(advanced),
            dialect=dialect,
            cfg=cfg,
            fetched_at=time.time(),
        )
        self._assets[asset_uid] = ctx
        return ctx

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

            supplement = self.client.get_supplement(asset_uid, submission_uuid)

            if stage in (STAGE_NEW, STAGE_TRANSCRIBE):
                next_stage, delay = self._do_transcribe(ctx, submission_uuid, supplement)
            elif stage == STAGE_TRANSLATE:
                next_stage, delay = self._do_translate(ctx, submission_uuid, supplement)
            elif stage == STAGE_QUAL:
                next_stage, delay = self._do_qual(ctx, submission_uuid, supplement)
            else:
                next_stage, delay = STAGE_DONE, 0.0

            self.store.advance(asset_uid, submission_uuid, next_stage,
                               delay=delay, error=None, bump_attempts=True)
            if next_stage == STAGE_DONE:
                log.info("[%s/%s] complete", asset_uid, submission_uuid)

        except KoboError as exc:
            backoff = min(600, 30 * (2 ** min(attempts, 5)))
            log.warning("[%s/%s] %s -- retrying in %ss", asset_uid, submission_uuid, exc, backoff)
            self.store.advance(asset_uid, submission_uuid, stage or STAGE_NEW,
                               delay=backoff, error=str(exc)[:1000], bump_attempts=True)
        except Exception as exc:  # noqa: BLE001
            log.exception("[%s/%s] unexpected error", asset_uid, submission_uuid)
            self.store.advance(asset_uid, submission_uuid, stage or STAGE_NEW,
                               delay=300, error=repr(exc)[:1000], bump_attempts=True)

    # -- stages -------------------------------------------------------------
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
