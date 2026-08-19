"""A brand-new deployment's first run, exactly as someone cloning this gets it.

The environment below is `.env.example` verbatim, with only ADMIN_PASSWORD
changed -- which is what the README tells a new user to set. Everything else
is still the shipped example value.

That distinction is the point. An example value is not a weak setting but an
absent one: it is printed in a public repository. Counting it as present made
the app report itself configured and stop asking, which is how a fresh
install skipped its own onboarding and how a webhook came to be "protected"
by a string in .env.example.
"""
import asyncio
import os
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
# Precisely what .env.example ships, plus the one value the README says to set.
os.environ.update({
    "KOBO_URL": "https://kf.example-partner.org",   # placeholder, untouched
    "KOBO_TOKEN": "your_api_token_here",            # placeholder, untouched
    "ADMIN_PASSWORD": "a-real-admin-password",
    "ADMIN_COOKIE_SECURE": "false",
    "PUBLIC_WEBHOOK_URL": "https://autoqa.example.org",
    "WEBHOOK_SECRET": "change-me-to-a-long-random-string",  # placeholder!
    "TRANSCRIPT_LANGUAGE": "fr-FR",
    "DB_PATH": "/tmp/firstrun.db",
})
for f in ("/tmp/firstrun.db", "/tmp/firstrun.db-wal", "/tmp/firstrun.db-shm"):
    Path(f).unlink(missing_ok=True)

import httpx
from app.webhook import app

MOCK = os.environ.get("MOCK_URL", "http://127.0.0.1:8899")
step = 0
def ok(name, cond, extra=""):
    global step; step += 1
    print(f"  {step:2}. {'PASS' if cond else 'FAIL'}  {name}{'  ' + str(extra) if extra else ''}")
    return 0 if cond else 1

async def main():
    bad = 0
    async with httpx.AsyncClient() as raw:
        await raw.post(MOCK + "/__reset"); await raw.post(MOCK + "/__mode/supplement")
    tr = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=tr, base_url="http://t") as c:
        print("\n[first visit]")
        s = (await c.get("/admin/api/session")).json()
        bad += ok("sign-in page loads", (await c.get("/admin/")).status_code == 200)
        bad += ok("it knows no account exists yet", s["has_users"] is False)
        bad += ok("and offers the recovery password", s["admin_enabled"] is True)

        print("\n[creating the first account]")
        r = await c.post("/admin/api/register",
                         json={"username": "newowner", "password": "a-first-real-password"})
        b = r.json()
        bad += ok("first account is created and signed in", b.get("signed_in") is True, r.text[:100])
        bad += ok("and is an administrator", b["user"]["is_admin"] is True)

        print("\n[before any credentials are entered]")
        env = (await c.get("/admin/api/env")).json()
        bad += ok("placeholder token is not mistaken for a real one",
                  env["token_set"] is False, env["token_set"])
        r = await c.get("/admin/api/assets")
        bad += ok("listing forms fails with a clear message", r.status_code == 400, r.status_code)
        bad += ok("that names the screen to fix it",
                  "Connection tab" in r.json()["detail"], r.json()["detail"])

        print("\n[entering credentials]")
        r = await c.put("/admin/api/credentials",
                        json={"kobo_url": MOCK, "kobo_token": "a-real-token"})
        bad += ok("credentials saved", r.status_code == 200, r.text[:100])
        r = await c.get("/admin/api/assets")
        bad += ok("forms now list", r.status_code == 200 and r.json()["count"] >= 1)
        asset = r.json()["results"][0]
        bad += ok("and show as not yet set up", asset["managed"] is False)

        print("\n[one click to turn it on]")
        r = await c.post(f"/admin/api/assets/{asset['uid']}/enable",
                         json={"transcript_language": "fr-FR"})
        e = r.json()
        bad += ok("automation switched on", r.status_code == 200, r.text[:140])
        bad += ok("every recording configured", len(e["configured"]) >= 1, e["configured"])
        bad += ok("webhook registered in the same step", e["hook"] == "registered", e["hook"])
        bad += ok("no warnings for a valid regional language", not e["warnings"], e["warnings"])
        bad += ok("form now shows as managed",
                  (await c.get("/admin/api/assets")).json()["results"][0]["managed"])
    print(f"\n{'FIRST RUN CLEAN' if not bad else str(bad) + ' PROBLEM(S)'}")
    return 1 if bad else 0

raise SystemExit(asyncio.run(main()))
