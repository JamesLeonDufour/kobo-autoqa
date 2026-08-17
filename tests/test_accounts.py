"""Accounts, approval, and isolation between them.

The point of these assertions is that one account cannot see or touch
another's credentials, forms or queue — the failure mode that matters when a
single deployment serves more than one organisation.
"""
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("KOBO_URL", "http://127.0.0.1:8899")
os.environ.setdefault("KOBO_TOKEN", "")          # no env fallback: accounts only
os.environ.setdefault("ADMIN_PASSWORD", "envpw")
os.environ.setdefault("ADMIN_COOKIE_SECURE", "false")
os.environ["DB_PATH"] = "/tmp/accounts.db"

for _leftover in ("/tmp/accounts.db", "/tmp/accounts.db-wal", "/tmp/accounts.db-shm"):
    Path(_leftover).unlink(missing_ok=True)

import httpx  # noqa: E402

from app import users as U  # noqa: E402
from app.common import make_store  # noqa: E402
from app.config import settings  # noqa: E402
from app.webhook import app  # noqa: E402

MOCK = os.environ["KOBO_URL"]


def ok(name, cond, extra=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}{'  ' + str(extra) if extra else ''}")
    return bool(cond)


def client():
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


async def main():
    fails = 0
    async with httpx.AsyncClient() as raw:
        await raw.post(MOCK + "/__reset")

    print("\n[passwords]")
    h = U.hash_password("correct horse battery")
    fails += not ok("hash verifies", U.verify_password("correct horse battery", h))
    fails += not ok("wrong password rejected", not U.verify_password("nope", h))
    fails += not ok("hash is salted", h != U.hash_password("correct horse battery"))
    fails += not ok("short passwords refused", bool(U.password_problem("short")))
    fails += not ok("bad usernames refused", bool(U.username_problem("a b")))
    fails += not ok("good usernames accepted", not U.username_problem("james.l-1"))

    print("\n[registration]")
    async with client() as a:
        r = await a.post("/admin/api/register", json={
            "username": "firstuser", "password": "first-account-pw"})
        body = r.json()
        fails += not ok("first account signs straight in", body.get("signed_in") is True, body)
        fails += not ok("first account is an admin",
                        body["user"]["is_admin"] and body["user"]["status"] == "active")

        r = await a.post("/admin/api/register", json={
            "username": "firstuser", "password": "another-password-x"})
        fails += not ok("duplicate username refused", r.status_code == 400)

    async with client() as b:
        r = await b.post("/admin/api/register", json={
            "username": "seconduser", "password": "second-account-pw"})
        fails += not ok("later accounts need approval", r.json().get("signed_in") is False)
        r = await b.post("/admin/api/login", json={
            "username": "seconduser", "password": "second-account-pw"})
        fails += not ok("pending account cannot sign in", r.status_code == 401)
        fails += not ok("and is told why", "approve" in r.json()["detail"].lower(),
                        r.json()["detail"])

    print("\n[approval]")
    store = make_store(settings)
    second = store.get_user_by_username("seconduser")
    async with client() as a:
        await a.post("/admin/api/login", json={"username": "firstuser",
                                               "password": "first-account-pw"})
        r = await a.get("/admin/api/users")
        fails += not ok("admin sees both accounts", len(r.json()["results"]) == 2)
        r = await a.post(f"/admin/api/users/{second['id']}/status", json={"status": "active"})
        fails += not ok("admin can approve", r.status_code == 200, r.text[:120])

    async with client() as b:
        r = await b.post("/admin/api/login", json={"username": "seconduser",
                                                   "password": "second-account-pw"})
        fails += not ok("approved account can sign in", r.status_code == 200)
        r = await b.get("/admin/api/users")
        fails += not ok("non-admin cannot list accounts", r.status_code == 403, r.status_code)

    print("\n[isolation]")
    async with client() as a, client() as b:
        await a.post("/admin/api/login", json={"username": "firstuser",
                                               "password": "first-account-pw"})
        await b.post("/admin/api/login", json={"username": "seconduser",
                                               "password": "second-account-pw"})
        await a.put("/admin/api/credentials", json={
            "kobo_url": MOCK, "kobo_token": "token-of-account-one"})
        await b.put("/admin/api/credentials", json={
            "kobo_url": MOCK, "kobo_token": "token-of-account-two"})

        ca = (await a.get("/admin/api/credentials")).json()["credentials"]
        cb = (await b.get("/admin/api/credentials")).json()["credentials"]
        fails += not ok("each account keeps its own token",
                        ca["kobo_token"]["hint"] != cb["kobo_token"]["hint"],
                        (ca["kobo_token"]["hint"], cb["kobo_token"]["hint"]))
        fails += not ok("no token value is ever returned",
                        "value" not in ca["kobo_token"] and "value" not in cb["kobo_token"])

        # Account A queues work; B must not see it.
        await a.post("/admin/api/assets/aMOCK1234567890abcdef/backfill", json={"days": 3650})
        ja = (await a.get("/admin/api/jobs")).json()
        jb = (await b.get("/admin/api/jobs")).json()
        fails += not ok("owner sees their queue", ja["stats"].get("new") == 3, ja["stats"])
        fails += not ok("other account sees an empty queue", jb["stats"] == {}, jb["stats"])

        # A form configured by A is not "managed" for B.
        await a.put("/admin/api/assets/aMOCK1234567890abcdef/config",
                    json={"enabled": True, "transcript_language": "fr-FR"})
        la = (await a.get("/admin/api/assets")).json()["results"][0]
        lb = (await b.get("/admin/api/assets")).json()["results"][0]
        fails += not ok("form is managed for its owner", la["managed"])
        fails += not ok("and unmanaged for everyone else", not lb["managed"])

        # B cannot delete A's queue entry.
        sub = ja["results"][0]["submission_uuid"]
        await b.delete(f"/admin/api/jobs/aMOCK1234567890abcdef/{sub}")
        still = (await a.get("/admin/api/jobs")).json()["stats"].get("new")
        fails += not ok("one account cannot delete another's job", still == 3, still)

    print("\n[webhook routing]")
    first = store.get_user_by_username("firstuser")
    async with client() as raw:
        r = await raw.post(f"/kobo/hook/{first['id']}/aMOCK1234567890abcdef",
                           json={"_uuid": "routed-to-first"})
        fails += not ok("per-account hook accepted", r.status_code == 200, r.text[:120])
    owned = store.for_owner(first["id"])
    fails += not ok("submission landed in the right account's queue",
                    any(j["submission_uuid"] == "routed-to-first"
                        for j in owned.list_jobs(limit=50)))
    other = store.for_owner(second["id"])
    fails += not ok("and not in the other's",
                    not any(j["submission_uuid"] == "routed-to-first"
                            for j in other.list_jobs(limit=50)))

    print("\n[password change ends sessions]")
    async with client() as a:
        await a.post("/admin/api/login", json={"username": "firstuser",
                                               "password": "first-account-pw"})
        r = await a.post("/admin/api/me/password",
                         json={"current": "wrong", "new": "a-brand-new-password"})
        fails += not ok("wrong current password refused", r.status_code == 403)
        r = await a.post("/admin/api/me/password",
                         json={"current": "first-account-pw", "new": "a-brand-new-password"})
        fails += not ok("password changed", r.status_code == 200, r.text[:120])
        r = await a.get("/admin/api/env")
        fails += not ok("old session no longer works", r.status_code == 401, r.status_code)

    print(f"\n{'ALL PASSED' if not fails else str(fails) + ' FAILURE(S)'}")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
