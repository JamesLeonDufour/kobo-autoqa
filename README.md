# Kobo AutoQA pipeline

Fully unattended transcription → translation → qualitative analysis for
KoboToolbox submissions. A new submission arrives, the pipeline requests
Kobo's own NLP jobs (AWS Transcribe → Google Translate → Bedrock AutoQA), and
the results land back in the submission's `_supplementalDetails`, so they show
up in the data table and in every export with no human clicking anything.

Everything is configurable from a web UI at **`/admin/`** — enter your Kobo
credentials, pick a form, check what the server's NLP API accepts, build the
analysis questions, register the webhook, then watch the queue. The CLI does
the same things headlessly.

## Architecture

```
     browser  ──►  /admin/   (setup wizard + queue dashboard)
                      │
                 REST Service (webhook)
KoboToolbox  ─────────────────────────────►  api  (FastAPI)
     ▲                                        │ enqueue
     │                                        ▼
     │                                   SQLite queue  (/data/pipeline.db)
     │                                        │ claim
     │           advanced_submission_post     ▼
     └──────────────────────────────────  worker  ── poll /data/ every 5 min
                                                    (catch-up for missed hooks)
```

Two containers, one shared SQLite volume:

| service  | role |
|----------|------|
| `api`    | receives Kobo REST Service POSTs, validates a shared secret, enqueues, returns 200 in milliseconds |
| `worker` | drains the queue **and** polls `/data/` on an interval so nothing is lost if the webhook fails |

The worker is a **non-blocking state machine**. Kobo's NLP jobs are async, so
each pass either issues a request or checks whether the previous one finished,
then reschedules itself:

```
new → transcribe → translate → qual → done
        ↑ ↓           ↑ ↓        ↑ ↓
     (re-check every ASYNC_POLL_SECONDS until status == complete)
```

Everything is idempotent and keyed on `(asset_uid, meta/rootUuid)`, so
duplicate webhooks, restarts, and overlapping polls cost nothing.

---

## Stack

- Python 3.12, `httpx`, `FastAPI`, `uvicorn`
- SQLite (WAL) for the job queue and poll cursors — no external DB
- Docker Compose, bound to `127.0.0.1:8077` for Nginx Proxy Manager to front

---

## Prerequisites

1. **AutoQA enabled on the server.** On a private server the `kpi` container
   needs AWS Transcribe + Bedrock credentials and a Google Cloud Translation
   key. AutoQA has been GA since 2.026.21 and each partner needs their own
   AWS Marketplace subscription for the Bedrock model.
2. **An API token** for a user with `change_asset` + `view_submissions` on the
   target forms: `GET /token/?format=json`. Put it in `.env` or paste it into
   the admin UI's Connection tab — either works.
3. **A public HTTPS endpoint** if you want the webhook. Polling alone works
   behind NAT — just leave `PUBLIC_WEBHOOK_URL` empty and skip `register-hook`.

---

## Setup — the UI route (recommended)

```bash
git clone https://github.com/JamesLeonDufour/kobo-autoqa.git && cd kobo-autoqa
cp .env.example .env
$EDITOR .env      # ADMIN_PASSWORD — that is the only required value
docker compose up -d --build
```

Open `http://127.0.0.1:8077/admin/` (or your NPM hostname) and sign in with
`ADMIN_PASSWORD`. Everything else — including the Kobo server and API token —
is done in the browser:

| Tab | What it does |
|---|---|
| **Connection** | Kobo server URL, API token, TLS verification, and the webhook URL + shared secret. Test the credentials before saving. |
| **Forms** | lists every survey on the server, with its submission count and whether the pipeline is set up on it |
| **Setup** | a 7-step wizard for the selected form — see below |
| **Monitor** | live queue tiles, per-submission stage, error detail, retry, and the raw `_supplementalDetails` Kobo currently holds |

On a fresh install with no `KOBO_TOKEN` in `.env`, signing in drops you
straight on the Connection tab. The worker idles quietly until credentials
exist rather than crash-looping, and picks them up within one tick of you
saving — no restart, no `docker compose` command.

The Setup wizard steps:

1. **Server capability check** — reads `advanced_submission_schema` live and shows
   the detected payload dialect. Override it if auto-detection is wrong.
2. **Audio questions** — auto-detected from the form definition; untick any
   recording you do not want processed.
3. **Languages** — source language, translation targets, which text AutoQA reads.
4. **Preset analysis questions** — a visual builder for the qual survey (free
   text, select one/multiple, tags, integer, note) with choice editing. A
   humanitarian starter template is one click away. UUIDs are generated once and
   then held stable, so re-applying never duplicates columns.
5. **Apply** — writes it all into the form's `advanced_features`. "Preview
   payload" shows the exact JSON first.
6. **Trigger** — registers the Kobo REST Service webhook with the shared-secret
   header, and shows its success/failure counts.
7. **Backfill** — queue existing submissions.

Forms enabled in the UI are watched by the worker automatically — `ASSET_UIDS`
in `.env` is optional and simply adds to that list.

`ADMIN_PASSWORD` empty disables the UI entirely (CLI-only mode). Sessions are
signed cookies keyed on the password itself, so changing the password logs
everyone out. Set `ADMIN_COOKIE_SECURE=false` only if you browse over plain
HTTP.

### Where credentials live

Two sources, and the UI wins:

| Priority | Source | Notes |
|---|---|---|
| 1 | Connection tab | stored in `app_settings` in the SQLite volume, shared by both containers |
| 2 | `.env` | used for any field the UI has not set |

Blanking a field in the UI removes the override and falls back to `.env`;
**Revert to .env** drops all of them at once. Secrets are never sent back to
the browser — the form shows a masked hint (`ui-••••••456`) and only writes a
new value when you actually type one, so saving the page without retyping your
token does not wipe it.

`ADMIN_PASSWORD` is deliberately **not** editable in the UI. It is the
credential guarding that very screen, so keeping it in `.env` means a bad
value can never lock you out of the box that fixes it.

Two caveats worth knowing:

- Tokens are stored **plaintext** in `/data/pipeline.db`, exactly as they would
  be in `.env`. Treat the Docker volume as secret material and do not commit it.
- Changing the webhook secret invalidates every already-registered Kobo REST
  Service. Re-register them from the Setup tab, or Kobo keeps sending the old
  header and collects 403s.

---

## Setup — the CLI route

```bash
cp .env.example .env
$EDITOR .env                 # KOBO_URL, KOBO_TOKEN, ASSET_UIDS, languages
docker compose build
```

### Step 1 — confirm what your server accepts

The subsequences API changed payload shape between kpi releases. Ask your
server directly instead of guessing:

```bash
docker compose run --rm worker python -m app.cli introspect aBcDeFgHiJkLmNoPqRsTuV
```

This prints the form's current `advanced_features`, the live
`advanced_submission_schema`, and the dialect the pipeline auto-detected
(`legacy` = `googlets`/`googletx`, `20250820` = the newer schema). If
auto-detection is wrong, pin it with `SCHEMA_DIALECT=` in `.env`.

```bash
docker compose run --rm worker python -m app.cli questions aBcDeFgHiJkLmNoPqRsTuV
```

lists the transcribable questions and any qual questions already configured.

### Step 2 — install the preset analysis questions

Edit `qual_survey.example.json` to your actual analysis questions, then:

```bash
docker compose run --rm -v "$PWD:/work" worker \
  python -m app.cli apply-qual aBcDeFgHiJkLmNoPqRsTuV /work/qual_survey.example.json --dry-run
```

Drop `--dry-run` to apply. It PATCHes `advanced_features` with the transcript /
translation / qual configuration and generates UUIDs for each question and
choice. **Copy the printed JSON back into your file** — the UUIDs must stay
stable, otherwise the next apply creates duplicate columns.

Supported question types: `qualText`, `qualInteger`, `qualSelectOne`,
`qualSelectMultiple`, `qualTags`, `qualNote`.

### Step 3 — start it

```bash
docker compose up -d
docker compose logs -f worker
curl -s http://127.0.0.1:8077/healthz | jq
```

### Step 4 — register the webhook (optional but recommended)

Point an NPM proxy host at `127.0.0.1:8077` with a Let's Encrypt cert, then:

```bash
docker compose run --rm worker python -m app.cli register-hook aBcDeFgHiJkLmNoPqRsTuV
docker compose run --rm worker python -m app.cli list-hooks    aBcDeFgHiJkLmNoPqRsTuV
```

This creates a Kobo REST Service pointing at
`https://autoqa.bareit.be/kobo/hook/<asset_uid>` with your
`X-Pipeline-Secret` header attached. Requests without the right secret get a
403; requests for an asset not in `ASSET_UIDS` get a 404.

The poller keeps running regardless, so a webhook outage means late results,
never lost ones.

---

## Operations

```bash
# Queue overview
docker compose run --rm worker python -m app.cli status

# Process one submission in the foreground, printing every stage
docker compose run --rm worker python -m app.cli run-once <asset_uid> <submission_uuid>

# What does Kobo currently hold for this submission?
docker compose run --rm worker python -m app.cli supplement <asset_uid> <submission_uuid>

# Reprocess history
docker compose run --rm worker python -m app.cli backfill <asset_uid> --since 2026-08-01T00:00:00
```

Set `DRY_RUN=true` to log every payload without POSTing it — useful for the
first run against a production form.

---

## Tuning

| Variable | Effect |
|---|---|
| `TRANSCRIPT_LANGUAGE` | BCP-47 source language, e.g. `fr-FR`. Must be an ASR-supported language (80 languages / 145 regional variants). |
| `TRANSLATION_LANGUAGES` | Comma-separated targets, e.g. `en,es`. Empty skips translation entirely. |
| `QUAL_SOURCE_LANGUAGE` | Which text AutoQA reads. Empty = the original transcript. |
| `ASYNC_POLL_SECONDS` | How often to re-check a running NLP job. 20s is fine; lower just burns API calls. |
| `POLL_INTERVAL_SECONDS` | Catch-up poll frequency. 300s is a good default; drop to 60s if the webhook is not registered. |
| `MAX_ATTEMPTS` | Passes before a submission is parked in `failed`. At 20s/pass, 40 ≈ 13 minutes of NLP wall time. |

---

## Testing without a Kobo server

A fake KoboToolbox is included. It exposes the same endpoints and completes NLP
jobs instantly, so you can exercise the whole flow offline:

```bash
pip install -r requirements.txt
python tests/mock_kobo.py &                    # http://127.0.0.1:8899
python tests/test_admin_flow.py                # 40 assertions, all should PASS

KOBO_URL=http://127.0.0.1:8899 KOBO_TOKEN=x ADMIN_PASSWORD=testpw \
ADMIN_COOKIE_SECURE=false DB_PATH=/tmp/mock.db \
PUBLIC_WEBHOOK_URL=https://autoqa.example.org WEBHOOK_SECRET=s3cret \
uvicorn app.webhook:app --port 8000            # then open /admin/
```

---

## Cost warning

Every submission triggers billable Transcribe minutes, Translate characters,
and Bedrock tokens on **your** AWS account. On a busy form this adds up fast.
Start with `DRY_RUN=true`, then a single form, then widen `ASSET_UIDS`.

---

## Known version sensitivity

`kobo/apps/subsequences/` was refactored upstream — `main` now carries a
`SCHEMA_VERSIONS = ['20250820', None]` constant and an `Action` enum
(`automatic_google_transcription`, `automatic_google_translation`,
`automatic_bedrock_qual`) where earlier releases used the `googlets` /
`googletx` keys. `app/payloads.py` implements both and picks at runtime, but
**run `introspect` against your actual server before trusting the qual payload
shape** — the AutoQA trigger is the newest and least-documented part of the
API. If it rejects the payload, the error body from
`advanced_submission_post` names the accepted keys; adjust
`app/payloads.py::qual_payload` or set `QUAL_TRIGGER_KEY`.
