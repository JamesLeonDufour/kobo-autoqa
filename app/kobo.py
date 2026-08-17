"""Thin, synchronous KoboToolbox API v2 client.

Only the endpoints this pipeline needs. Every call raises on HTTP error and
returns parsed JSON.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Iterator
from urllib.parse import quote

import httpx

log = logging.getLogger(__name__)


class KoboError(RuntimeError):
    def __init__(self, message: str, status: int | None = None, body: Any = None):
        super().__init__(message)
        self.status = status
        self.body = body


class KoboClient:
    def __init__(self, base_url: str, token: str, *, verify: bool = True, timeout: float = 60.0):
        self.base_url = base_url.rstrip("/")
        self._client = httpx.Client(
            base_url=self.base_url,
            headers={
                "Authorization": f"Token {token}",
                "Accept": "application/json",
                "User-Agent": "kobo-autoqa-pipeline/1.0",
            },
            verify=verify,
            timeout=timeout,
            follow_redirects=True,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "KoboClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- low level ----------------------------------------------------------
    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        resp = self._client.request(method, path, **kwargs)
        if resp.status_code >= 400:
            try:
                body = resp.json()
            except Exception:
                body = resp.text[:2000]
            raise KoboError(
                f"{method} {path} -> {resp.status_code}: {json.dumps(body)[:1000]}",
                status=resp.status_code,
                body=body,
            )
        if not resp.content:
            return None
        try:
            return resp.json()
        except Exception:
            return resp.text

    # -- assets -------------------------------------------------------------
    def get_asset(self, asset_uid: str) -> dict:
        return self._request("GET", f"/api/v2/assets/{asset_uid}/", params={"format": "json"})

    def patch_asset(self, asset_uid: str, data: dict) -> dict:
        return self._request(
            "PATCH", f"/api/v2/assets/{asset_uid}/",
            params={"format": "json"}, json=data,
        )

    def get_advanced_features(self, asset_uid: str) -> dict:
        return (self.get_asset(asset_uid) or {}).get("advanced_features") or {}

    def set_advanced_features(self, asset_uid: str, advanced_features: dict) -> dict:
        return self.patch_asset(asset_uid, {"advanced_features": advanced_features})

    def get_advanced_submission_schema(self, asset_uid: str) -> dict:
        return self._request(
            "GET", f"/api/v2/assets/{asset_uid}/advanced_submission_schema/",
            params={"format": "json"},
        )

    # -- submissions --------------------------------------------------------
    def get_submission(self, asset_uid: str, submission_id: str | int) -> dict:
        return self._request(
            "GET", f"/api/v2/assets/{asset_uid}/data/{submission_id}/",
            params={"format": "json"},
        )

    def iter_submissions(
        self,
        asset_uid: str,
        *,
        query: dict | None = None,
        fields: list[str] | None = None,
        sort: dict | None = None,
        page_size: int = 200,
        max_pages: int = 100,
    ) -> Iterator[dict]:
        """Page through /data/ with an optional Mongo-style query."""
        start = 0
        for _ in range(max_pages):
            params: dict[str, Any] = {"format": "json", "limit": page_size, "start": start}
            if query:
                params["query"] = json.dumps(query)
            if fields:
                params["fields"] = json.dumps(fields)
            if sort:
                params["sort"] = json.dumps(sort)
            data = self._request("GET", f"/api/v2/assets/{asset_uid}/data/", params=params) or {}
            results = data.get("results") or []
            if not results:
                return
            yield from results
            if len(results) < page_size:
                return
            start += page_size

    # -- supplemental details (NLP) -----------------------------------------
    def get_supplement(self, asset_uid: str, submission_uuid: str) -> dict:
        """Current transcript/translation/qual state for one submission."""
        try:
            return self._request(
                "GET", f"/api/v2/assets/{asset_uid}/advanced_submission_post/",
                params={"submission": submission_uuid, "format": "json"},
            ) or {}
        except KoboError as exc:
            if exc.status == 404:
                return {}
            raise

    def post_supplement(self, asset_uid: str, payload: dict) -> dict:
        """Submit a transcription / translation / qual instruction."""
        return self._request(
            "POST", f"/api/v2/assets/{asset_uid}/advanced_submission_post/",
            json=payload,
        ) or {}

    # -- advanced features / supplements (current kpi API) ------------------
    def list_advanced_features(self, asset_uid: str) -> list[dict]:
        """Configured NLP actions for an asset. 404 = this server is older."""
        data = self._request(
            "GET", f"/api/v2/assets/{asset_uid}/advanced-features/",
            params={"format": "json"},
        )
        if isinstance(data, dict):
            return data.get("results") or []
        return data or []

    def create_advanced_feature(
        self, asset_uid: str, *, question_xpath: str, action: str, params: list[dict]
    ) -> dict:
        return self._request(
            "POST", f"/api/v2/assets/{asset_uid}/advanced-features/",
            params={"format": "json"},
            json={"question_xpath": question_xpath, "action": action, "params": params},
        )

    def update_advanced_feature(self, asset_uid: str, feature_uid: str,
                                params: list[dict]) -> dict:
        return self._request(
            "PATCH", f"/api/v2/assets/{asset_uid}/advanced-features/{quote(feature_uid)}/",
            params={"format": "json"}, json={"params": params},
        )

    def get_data_supplement(self, asset_uid: str, root_uuid: str) -> dict:
        """Current NLP results for one submission. 404 when the uuid is unknown."""
        try:
            return self._request(
                "GET", f"/api/v2/assets/{asset_uid}/data/{quote(root_uuid)}/supplement/",
                params={"format": "json"},
            ) or {}
        except KoboError as exc:
            if exc.status == 404:
                return {}
            raise

    def patch_data_supplement(self, asset_uid: str, root_uuid: str, payload: dict) -> dict:
        """Request transcription / translation / qual for one submission."""
        return self._request(
            "PATCH", f"/api/v2/assets/{asset_uid}/data/{quote(root_uuid)}/supplement/",
            params={"format": "json"}, json=payload,
        ) or {}

    # -- REST services (hooks) ----------------------------------------------
    def list_hooks(self, asset_uid: str) -> list[dict]:
        data = self._request(
            "GET", f"/api/v2/assets/{asset_uid}/hooks/", params={"format": "json"}
        ) or {}
        return data.get("results", [])

    def create_hook(
        self,
        asset_uid: str,
        *,
        name: str,
        endpoint: str,
        custom_headers: dict[str, str] | None = None,
        subset_fields: list[str] | None = None,
    ) -> dict:
        body = {
            "name": name,
            "endpoint": endpoint,
            "active": True,
            "email_notification": False,
            "export_type": "json",
            "auth_level": "no_auth",
            "subset_fields": subset_fields or [],
            "settings": {"custom_headers": custom_headers or {}},
        }
        return self._request(
            "POST", f"/api/v2/assets/{asset_uid}/hooks/", params={"format": "json"}, json=body
        )

    def delete_hook(self, asset_uid: str, hook_uid: str) -> None:
        self._request("DELETE", f"/api/v2/assets/{asset_uid}/hooks/{quote(hook_uid)}/")
