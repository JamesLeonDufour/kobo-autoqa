"""Effective per-asset configuration.

Precedence: values saved from the admin UI (SQLite `asset_settings`) override
the .env defaults. Anything the UI has not set falls back to the environment,
so an existing CLI-only deployment keeps working unchanged.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

from .config import Settings


@dataclass
class AssetConfig:
    asset_uid: str
    enabled: bool = True
    transcript_language: str = "en"
    translation_languages: list[str] = field(default_factory=list)
    enable_qual: bool = True
    qual_source_language: str = ""
    schema_dialect: str = "auto"
    qual_trigger_key: str = "qual"
    # Restrict processing to these xpaths. Empty = every transcribable question.
    xpaths: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


FIELDS = {f for f in AssetConfig.__dataclass_fields__ if f != "asset_uid"}


def defaults_from_env(s: Settings, asset_uid: str) -> AssetConfig:
    return AssetConfig(
        asset_uid=asset_uid,
        enabled=True,
        transcript_language=s.transcript_language,
        translation_languages=list(s.translation_languages),
        enable_qual=s.enable_qual,
        qual_source_language=s.qual_source_language,
        schema_dialect=s.schema_dialect,
        qual_trigger_key=s.qual_trigger_key,
        xpaths=[],
    )


def resolve(s: Settings, store, asset_uid: str) -> AssetConfig:
    cfg = defaults_from_env(s, asset_uid)
    saved = store.get_asset_settings(asset_uid) or {}
    for key, value in saved.items():
        if key in FIELDS and value is not None:
            setattr(cfg, key, value)
    return cfg


def save(store, asset_uid: str, patch: dict) -> dict:
    """Merge a patch into the saved overrides. Returns the stored dict, which
    holds only the keys the UI has set -- call resolve() for effective values."""
    current = store.get_asset_settings(asset_uid) or {}
    for key, value in patch.items():
        if key in FIELDS:
            current[key] = value
    store.set_asset_settings(asset_uid, current)
    return current
