"""End-to-end admin flow against tests/mock_kobo.py (must be running on :8899)."""
import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("KOBO_URL", "http://127.0.0.1:8899")
os.environ.setdefault("KOBO_TOKEN", "x")
os.environ.setdefault("ADMIN_PASSWORD", "testpw")
os.environ.setdefault("ADMIN_COOKIE_SECURE", "false")
os.environ.setdefault("DB_PATH", "/tmp/admin.db")
os.environ.setdefault("PUBLIC_WEBHOOK_URL", "https://autoqa.example.org")
os.environ.setdefault("WEBHOOK_SECRET", "s3cret")

# Start from an empty queue. Several assertions count rows the run creates
# ("backfill enqueued 3"), so a database left over from a previous run would
# fail them for reasons that have nothing to do with the code.
for _leftover in (os.environ["DB_PATH"], os.environ["DB_PATH"] + "-wal",
                  os.environ["DB_PATH"] + "-shm"):
    Path(_leftover).unlink(missing_ok=True)

import httpx  # noqa: E402
from app.webhook import app  # noqa: E402

U = "aMOCK1234567890abcdef"
SURVEY = [
    {"type": "qualText", "labels": {"_default": "Summarise the concern."}},
    {"type": "qualSelectOne", "labels": {"_default": "Sentiment"},
     "choices": [{"labels": {"_default": "Positive"}}, {"labels": {"_default": "Negative"}}]},
    {"type": "qualInteger", "labels": {"_default": "Urgency 1-5"}},
]


def ok(name, cond, extra=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}{'  ' + str(extra) if extra else ''}")
    return bool(cond)


async def main():
    fails = 0
    async with httpx.AsyncClient() as raw:
        await raw.post(os.environ["KOBO_URL"] + "/__reset")
    tr = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=tr, base_url="http://t") as c:
        print("\n[auth]")
        fails += not ok("unauthenticated /env is 401", (await c.get("/admin/api/env")).status_code == 401)
        fails += not ok("wrong password rejected",
                        (await c.post("/admin/api/login", json={"password": "no"})).status_code == 401)
        r = await c.post("/admin/api/login", json={"password": "testpw"})
        fails += not ok("login sets cookie", r.status_code == 200 and "autoqa_session" in c.cookies)
        fails += not ok("authenticated /env is 200", (await c.get("/admin/api/env")).status_code == 200)

        print("\n[ui]")
        r = await c.get("/admin/")
        fails += not ok("UI served", r.status_code == 200 and "AutoQA pipeline" in r.text, f"{len(r.text)}b")
        fails += not ok("/ redirects", (await c.get("/")).status_code in (307, 200))
        p = (await c.get("/admin/api/ping")).json()
        fails += not ok("Kobo reachable", p.get("username") == "mockuser")

        print("\n[discovery]")
        d = (await c.get(f"/admin/api/assets/{U}")).json()
        fails += not ok("audio questions found",
                        d["media_questions"] == ["section_a/Recording_001", "ambient"], d["media_questions"])
        fails += not ok("dialect detected", d["detected_dialect"] == "legacy", d["detected_dialect"])
        fails += not ok("expected endpoint built", d["expected_endpoint"].endswith(f"/kobo/hook/0/{U}"),
                        d["expected_endpoint"])

        print("\n[qual survey]")
        body = {"qual_survey": json.loads(json.dumps(SURVEY)),
                "xpaths": ["section_a/Recording_001"], "translation_languages": ["en"]}
        r = await c.put(f"/admin/api/assets/{U}/qual", json={**body, "dry_run": True})
        after_dry = (await c.get(f"/admin/api/assets/{U}")).json()["qual_survey"]
        fails += not ok("dry run does not persist", r.status_code == 200 and not after_dry)
        js = (await c.put(f"/admin/api/assets/{U}/qual", json=body)).json()
        fails += not ok("uuids generated", all(q["uuid"] for q in js["qual_survey"]))
        fails += not ok("choice uuids generated",
                        all(ch["uuid"] for ch in js["qual_survey"][1]["choices"]))
        af = js["advanced_features"]
        fails += not ok("advanced_features written",
                        af["transcript"]["values"] == ["section_a/Recording_001"]
                        and af["translation"]["languages"] == ["en"]
                        and len(af["qual"]["qual_survey"]) == 3)
        # idempotency: re-applying with the same uuids must not duplicate
        js2 = (await c.put(f"/admin/api/assets/{U}/qual",
                           json={**body, "qual_survey": js["qual_survey"]})).json()
        fails += not ok("re-apply is idempotent",
                        [q["uuid"] for q in js2["qual_survey"]] == [q["uuid"] for q in js["qual_survey"]])

        print("\n[config]")
        r = await c.put(f"/admin/api/assets/{U}/config", json={
            "enabled": True, "transcript_language": "fr-FR",
            "translation_languages": "en, es", "qual_source_language": "en",
            "enable_qual": True, "schema_dialect": "auto",
            "xpaths": ["section_a/Recording_001"]})
        cfg = r.json()["config"]
        fails += not ok("csv languages parsed", cfg["translation_languages"] == ["en", "es"], cfg["translation_languages"])
        listed = (await c.get("/admin/api/assets")).json()["results"][0]
        fails += not ok("form shows as managed+enabled", listed["managed"] and listed["enabled"])

        print("\n[webhook]")
        h1 = (await c.post(f"/admin/api/assets/{U}/hook", json={})).json()
        h2 = (await c.post(f"/admin/api/assets/{U}/hook", json={})).json()
        fails += not ok("hook created then idempotent", not h1["existing"] and h2["existing"])
        d = (await c.get(f"/admin/api/assets/{U}")).json()
        fails += not ok("hook recognised as ours", any(h["is_ours"] for h in d["hooks"]))
        await c.delete(f"/admin/api/assets/{U}/hook/{h1['uid']}")
        d = (await c.get(f"/admin/api/assets/{U}")).json()
        fails += not ok("hook deleted", not d["hooks"])

        print("\n[queue]")
        r = (await c.post(f"/admin/api/assets/{U}/backfill", json={"days": 3650})).json()
        fails += not ok("backfill enqueued 3", r["enqueued"] == 3, r)
        j = (await c.get("/admin/api/jobs")).json()
        fails += not ok("jobs visible", j["stats"].get("new") == 3, j["stats"])
        fails += not ok("asset auto-watched", U in j["watched"], j["watched"])

        print("\n[webhook receiver]")
        r = await c.post(f"/kobo/hook/{U}", json={"meta": {"rootUuid": "uuid:sub-9"}},
                         headers={"X-Pipeline-Secret": "s3cret"})
        fails += not ok("hook accepts", r.json()["status"] == "queued")
        r = await c.post(f"/kobo/hook/{U}", json={"_uuid": "x"}, headers={"X-Pipeline-Secret": "bad"})
        fails += not ok("hook rejects bad secret", r.status_code == 403)

        print("\n[credentials]")
        cr = (await c.get("/admin/api/credentials")).json()["credentials"]
        fails += not ok("token reported as set from env",
                        cr["kobo_token"]["set"] and cr["kobo_token"]["source"] == "env",
                        cr["kobo_token"]["source"])
        fails += not ok("token value never sent to browser",
                        "value" not in cr["kobo_token"] and "x" not in cr["kobo_token"]["hint"])
        fails += not ok("url shown from env",
                        cr["kobo_url"]["value"] == os.environ["KOBO_URL"], cr["kobo_url"]["value"])

        r = await c.post("/admin/api/credentials/test",
                         json={"kobo_url": os.environ["KOBO_URL"], "kobo_token": "anything"})
        fails += not ok("test connection succeeds", r.json().get("username") == "mockuser")
        r = await c.post("/admin/api/credentials/test",
                         json={"kobo_url": "http://127.0.0.1:9", "kobo_token": "t"})
        fails += not ok("test connection reports failure", r.status_code == 502)
        r = await c.post("/admin/api/credentials/test", json={"kobo_url": "not-a-url"})
        fails += not ok("bad url rejected", r.status_code == 400)

        r = await c.put("/admin/api/credentials", json={"admin_password": "hack"})
        fails += not ok("admin password not editable", r.status_code == 400)

        r = await c.put("/admin/api/credentials", json={
            "kobo_url": os.environ["KOBO_URL"], "kobo_token": "ui-token-123456",
            "webhook_secret": "ui-secret", "verify_tls": True})
        cr = r.json()["credentials"]
        fails += not ok("saved values marked as ui-sourced",
                        cr["kobo_token"]["source"] == "ui" and cr["webhook_secret"]["source"] == "ui")
        fails += not ok("secret hint is masked",
                        cr["kobo_token"]["hint"] == "ui-••••••456", cr["kobo_token"]["hint"])

        # Credentials are resolved per account rather than mutated onto the
        # process-wide settings, so check the resolution the app actually uses.
        from app import runtime  # noqa: PLC0415
        from app.common import make_store  # noqa: PLC0415
        from app.config import settings as base  # noqa: PLC0415

        class live:  # noqa: N801 - keeps the assertions below readable
            pass
        def _resolved():
            return runtime.for_owner(make_store(base), 0)
        fails += not ok("saved token is what the app resolves",
                        _resolved().kobo_token == "ui-token-123456")
        r = await c.post(f"/kobo/hook/{U}", json={"_uuid": "sub-ui"},
                         headers={"X-Pipeline-Secret": "ui-secret"})
        fails += not ok("webhook honours the UI secret", r.status_code == 200, r.text)
        r = await c.post(f"/kobo/hook/{U}", json={"_uuid": "sub-ui"},
                         headers={"X-Pipeline-Secret": "s3cret"})
        fails += not ok("old .env secret no longer accepted", r.status_code == 403)

        # blank string clears an override rather than blanking the .env value
        r = await c.put("/admin/api/credentials", json={"kobo_token": ""})
        fails += not ok("blank field falls back to env",
                        r.json()["credentials"]["kobo_token"]["source"] == "env"
                        and _resolved().kobo_token == "x",
                        _resolved().kobo_token)

        r = await c.delete("/admin/api/credentials")
        cr = r.json()["credentials"]
        fails += not ok("revert restores every env value",
                        all(v.get("source") != "ui" for v in cr.values() if isinstance(v, dict))
                        and _resolved().webhook_secret == "s3cret",
                        _resolved().webhook_secret)

        print("\n[one-click enable]")
        # Everything this does was possible before, but only as three separate
        # actions -- and forgetting the webhook silently downgraded the form to
        # five-minute polling.
        async with httpx.AsyncClient() as raw:
            await raw.post(os.environ["KOBO_URL"] + "/__mode/supplement")
        r = await c.post(f"/admin/api/assets/{U}/enable",
                         json={"transcript_language": "fr-FR"})
        fails += not ok("enable succeeds", r.status_code == 200, r.text[:160])
        body = r.json()
        fails += not ok("nothing is configured twice",
                        not set(body["configured"]) & set(body["already_configured"]), body)
        fails += not ok("the webhook is registered in the same step",
                        body["hook"] in ("registered", "already registered"), body["hook"])
        fails += not ok("the form is now managed",
                        any(x["managed"] for x in
                            (await c.get("/admin/api/assets")).json()["results"]))

        # A recording already set up must not be touched: the rows are
        # append-only, so overwriting its language could not be undone.
        r2 = (await c.post(f"/admin/api/assets/{U}/enable",
                           json={"transcript_language": "de-DE"})).json()
        fails += not ok("re-running configures nothing new", not r2["configured"], r2)
        detail = (await c.get(f"/admin/api/assets/{U}")).json()
        langs = {x: v["transcript_language"]
                 for x, v in (detail.get("per_question") or {}).items()}
        fails += not ok("and no existing language was overwritten",
                        "de-DE" not in langs.values(), langs)

        print("\n[hints survive the round trip]")
        # Hints are the guidance sent to the model with each question, and are
        # where most of the answer quality comes from. They travel as
        # {"labels": {"_default": ...}} on both questions and choices.
        r = await c.put(f"/admin/api/assets/{U}/qual", json={
            "xpaths": ["section_a/Recording_001"],
            "edit_xpath": "section_a/Recording_001", "dry_run": True,
            "qual_survey": [{"uuid": "", "type": "qualSelectOne",
                             "labels": {"_default": "Probe"},
                             "hint": {"labels": {"_default": "question guidance"}},
                             "choices": [{"uuid": "", "labels": {"_default": "Yes"},
                                          "hint": {"labels": {"_default": "choice guidance"}}}]}]})
        q = (r.json().get("manual_qual") or [{}])[0]
        fails += not ok("a question hint is kept",
                        (q.get("hint") or {}).get("labels", {}).get("_default")
                        == "question guidance", q.get("hint"))
        fails += not ok("a choice hint is kept",
                        ((q.get("choices") or [{}])[0].get("hint") or {})
                        .get("labels", {}).get("_default") == "choice guidance",
                        (q.get("choices") or [{}])[0])

        print("\n[logout]")
        await c.post("/admin/api/logout")
        fails += not ok("session cleared", (await c.get("/admin/api/env")).status_code == 401)

    print(f"\n{'ALL PASSED' if not fails else str(fails) + ' FAILURE(S)'}")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
