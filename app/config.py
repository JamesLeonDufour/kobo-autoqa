"""Configuration loaded from environment variables."""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _csv(value: str | None) -> list[str]:
    return [v.strip() for v in (value or "").split(",") if v.strip()]


# Values shipped in .env.example. People copy that file and fill in only the
# fields they recognise, so these reach the runtime looking like real settings
# and fail later as DNS errors or 401s. Treat them as "not configured".
PLACEHOLDERS = frozenset({
    "your_api_token_here",
    "https://kf.example-partner.org",
    "change-me-to-a-long-random-string",
    "change-me-too",
})


def _bool(value: str | None, default: bool = False) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


@dataclass
class Settings:
    # --- Kobo connection ---------------------------------------------------
    kobo_url: str = field(default_factory=lambda: os.getenv("KOBO_URL", "https://kf.kobotoolbox.org").rstrip("/"))
    kobo_token: str = field(default_factory=lambda: os.getenv("KOBO_TOKEN", ""))
    verify_tls: bool = field(default_factory=lambda: _bool(os.getenv("KOBO_VERIFY_TLS"), True))
    http_timeout: float = field(default_factory=lambda: float(os.getenv("HTTP_TIMEOUT", "60")))

    # --- Scope -------------------------------------------------------------
    # Comma-separated asset UIDs to watch. Empty = watch every asset that a
    # webhook delivers (polling still requires an explicit list).
    asset_uids: list[str] = field(default_factory=lambda: _csv(os.getenv("ASSET_UIDS")))

    # --- NLP behaviour -----------------------------------------------------
    # Source language for automatic transcription, e.g. "fr" or "fr-FR".
    transcript_language: str = field(default_factory=lambda: os.getenv("TRANSCRIPT_LANGUAGE", "en"))
    # Target languages for automatic translation.
    translation_languages: list[str] = field(default_factory=lambda: _csv(os.getenv("TRANSLATION_LANGUAGES")))
    # Run the preset qualitative-analysis questions (AutoQA) after translation.
    enable_qual: bool = field(default_factory=lambda: _bool(os.getenv("ENABLE_QUAL"), True))
    # Which translation language AutoQA should read from. Empty = the transcript.
    qual_source_language: str = field(default_factory=lambda: os.getenv("QUAL_SOURCE_LANGUAGE", ""))

    # --- Payload dialect ---------------------------------------------------
    # "legacy"  -> googlets/googletx keys (kpi 2.024.x - 2.026.x)
    # "20250820"-> newer subsequences schema
    # "auto"    -> probe /advanced_submission_schema/ once and decide
    schema_dialect: str = field(default_factory=lambda: os.getenv("SCHEMA_DIALECT", "auto"))
    # Key used to request automatic Bedrock qual analysis. Confirm against your
    # server with `python -m app.cli introspect <asset_uid>`.
    qual_trigger_key: str = field(default_factory=lambda: os.getenv("QUAL_TRIGGER_KEY", "qual"))

    # --- Scheduling --------------------------------------------------------
    poll_interval_seconds: int = field(default_factory=lambda: int(os.getenv("POLL_INTERVAL_SECONDS", "300")))
    poll_lookback_minutes: int = field(default_factory=lambda: int(os.getenv("POLL_LOOKBACK_MINUTES", "1440")))
    worker_tick_seconds: int = field(default_factory=lambda: int(os.getenv("WORKER_TICK_SECONDS", "15")))
    # How long to wait between checks while Kobo processes an async job.
    async_poll_seconds: int = field(default_factory=lambda: int(os.getenv("ASYNC_POLL_SECONDS", "20")))
    # Real errors before a submission is parked. Polls do not count towards it.
    max_failures: int = field(default_factory=lambda: int(os.getenv("MAX_FAILURES", "5")))
    # Wall-clock ceiling, so a job that never resolves still stops eventually.
    max_job_age_hours: int = field(default_factory=lambda: int(os.getenv("MAX_JOB_AGE_HOURS", "24")))
    # Runaway guard only. Waiting for Kobo's async NLP burns passes, so this
    # has to stay well clear of a healthy run -- MAX_FAILURES is the real limit.
    max_attempts: int = field(default_factory=lambda: int(os.getenv("MAX_ATTEMPTS", "500")))

    # --- Webhook -----------------------------------------------------------
    webhook_secret: str = field(default_factory=lambda: os.getenv("WEBHOOK_SECRET", ""))
    webhook_secret_header: str = field(default_factory=lambda: os.getenv("WEBHOOK_SECRET_HEADER", "X-Pipeline-Secret"))
    public_webhook_url: str = field(default_factory=lambda: os.getenv("PUBLIC_WEBHOOK_URL", ""))

    # --- Admin UI ----------------------------------------------------------
    admin_password: str = field(default_factory=lambda: os.getenv("ADMIN_PASSWORD", ""))
    admin_session_hours: int = field(default_factory=lambda: int(os.getenv("ADMIN_SESSION_HOURS", "12")))
    admin_cookie_secure: bool = field(default_factory=lambda: _bool(os.getenv("ADMIN_COOKIE_SECURE"), True))

    # --- Storage / logging -------------------------------------------------
    db_path: str = field(default_factory=lambda: os.getenv("DB_PATH", "/data/pipeline.db"))
    log_level: str = field(default_factory=lambda: os.getenv("LOG_LEVEL", "INFO"))
    dry_run: bool = field(default_factory=lambda: _bool(os.getenv("DRY_RUN"), False))

    def validate(self) -> None:
        if not self.kobo_token or self.kobo_token in PLACEHOLDERS:
            raise RuntimeError(
                "No Kobo API token. Set KOBO_TOKEN in .env, or enter one on the "
                "Connection tab of the admin UI."
            )
        if not self.kobo_url.startswith("http"):
            raise RuntimeError("Kobo server URL must be a full http(s) URL")
        if self.kobo_url in PLACEHOLDERS:
            raise RuntimeError(
                f"KOBO_URL is still the example value ({self.kobo_url}). Set your "
                "own server on the Connection tab, or in .env."
            )


settings = Settings()
