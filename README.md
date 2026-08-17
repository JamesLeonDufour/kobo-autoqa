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
- SQLite (WAL) for the job queue, poll cursors, per-form settings, and
  UI-entered credentials — no external DB
- Docker Compose. The API is published to Nginx Proxy Manager over a shared
  Docker network, plus `127.0.0.1:8077` for local debugging

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

The CLI reads the same effective settings as the rest of the pipeline, so if
you have already saved credentials on the Connection tab you can leave
`KOBO_URL` and `KOBO_TOKEN` out of `.env` and the commands below still work.

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

Publish the API first (see [Putting it behind Nginx Proxy Manager](#putting-it-behind-nginx-proxy-manager)), then:

```bash
docker compose run --rm worker python -m app.cli register-hook aBcDeFgHiJkLmNoPqRsTuV
docker compose run --rm worker python -m app.cli list-hooks    aBcDeFgHiJkLmNoPqRsTuV
```

This creates a Kobo REST Service pointing at
`https://autoqa.bareit.be/kobo/hook/<asset_uid>` with your
`X-Pipeline-Secret` header attached.

How the receiver decides what to accept:

- Wrong or missing secret → **403**. If `WEBHOOK_SECRET` is empty the check is
  skipped entirely, so anyone who knows the URL can enqueue work — set it.
- Asset not enabled in the UI and not in `ASSET_UIDS` → **404**. If *both*
  lists are empty the pipeline accepts any asset the hook delivers, which is
  convenient for a first test and too permissive for a public deployment.
- A payload with no usable submission uuid → **200** with `"status":"ignored"`,
  on purpose: retrying a malformed payload would never help.

The poller keeps running regardless, so a webhook outage means late results,
never lost ones.

---

## Putting it behind Nginx Proxy Manager

The `api` container joins two networks: its own `autoqa` network and the
external `nginxproxy_default` network that the NPM stack creates. That second
one is what lets NPM resolve the container **by name**, which is how every
other proxied service on this host is wired — no host-port hop involved.

The `127.0.0.1:8077` binding stays, but only for local `curl` and debugging.
NPM does not use it (it cannot: the port is bound to the *host's* loopback,
which is not reachable from inside the NPM container).

In the NPM UI → **Hosts → Proxy Hosts → Add Proxy Host**:

| Field | Value |
|---|---|
| Domain Names | `autoqa.bareit.be` |
| Scheme | `http` |
| Forward Hostname / IP | `kobo-autoqa-api` |
| Forward Port | `8000` |
| Websockets Support | on |
| Block Common Exploits | on |
| SSL → Certificate | request a new Let's Encrypt cert |
| SSL → Force SSL + HTTP/2 | on |

Then set `PUBLIC_WEBHOOK_URL=https://autoqa.bareit.be` in `.env` (or on the
Connection tab) so registered webhooks point at the right place.

> If you rename or recreate the compose project, the container name must stay
> `kobo-autoqa-api` or NPM's proxy host will 502. The `name:` key at the top of
> `docker-compose.yml` pins the project name so a renamed checkout does not
> silently create a second stack with a fresh, empty volume.

**Before exposing it publicly**, make sure `ADMIN_PASSWORD` and
`WEBHOOK_SECRET` are real values and not the `change-me` placeholders from
`.env.example`. The admin UI is the control plane for a form's NLP
configuration and your Kobo token — it must not be reachable with a guessable
password.

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(24))"   # admin password
python3 -c "import secrets; print(secrets.token_urlsafe(32))"   # webhook secret
docker compose up -d          # picks up the new .env
```

Changing `ADMIN_PASSWORD` signs out every existing session; changing
`WEBHOOK_SECRET` means re-registering the Kobo REST Services.

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

## When something looks wrong

The Monitor tab is the first place to look: it shows a tile per stage, and each
row carries the submission's stage, attempt count, and last error. "View" prints
the raw `_supplementalDetails` Kobo currently holds for that submission, which
is the ground truth for whether the NLP actually ran.

| Symptom | Most likely cause |
|---|---|
| Badge reads **Kobo unreachable** | Wrong server URL or a rejected token. Use **Test connection** on the Connection tab — it reports the difference between "cannot reach the host" and "token rejected". |
| Forms tab is empty | The token belongs to a user with no surveys, or lacks `view_submissions` on them. |
| Jobs sit in **Queued** forever | The worker is not running or has no credentials. Check `docker compose logs -f worker`. |
| Jobs cycle in **Transcribing** and never finish | Kobo accepted the request but its NLP is not completing — AutoQA may not be enabled server-side, or the audio language is not ASR-supported. Check the supplement with "View". |
| Everything lands in **Failed** with a 400 | Almost always the payload dialect. Run `introspect` against the server and pin `SCHEMA_DIALECT`; see [Known version sensitivity](#known-version-sensitivity). |
| Webhook shows failures in Kobo | Secret mismatch (403) or the form is not enabled here (404). Re-register the hook from the Setup tab after any secret change. |
| NPM returns **502** | The `api` container is down, or it is not on the `nginxproxy_default` network — confirm with `docker exec nginxproxy-app-1 curl -s -o /dev/null -w '%{http_code}' http://kobo-autoqa-api:8000/healthz`. |

A job parked in `failed` is not lost. **Retry all failed** on the Monitor tab
resets them to `new` and the worker picks them straight back up.

---

## Tuning

The first three are **defaults**. The Setup wizard saves them per form, and a
form's own values win — so one form can be `fr-FR → en` while another is
`ar → en,fr`. The scheduling variables below are process-wide.

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
python tests/test_supplement_flow.py           # 16 assertions, current NLP API

KOBO_URL=http://127.0.0.1:8899 KOBO_TOKEN=x ADMIN_PASSWORD=testpw \
ADMIN_COOKIE_SECURE=false DB_PATH=/tmp/mock.db \
PUBLIC_WEBHOOK_URL=https://autoqa.example.org WEBHOOK_SECRET=s3cret \
uvicorn app.webhook:app --port 8000            # then open /admin/
```

---

## Cost warning

Every submission triggers billable Transcribe minutes, Translate characters,
and Bedrock tokens on **your** AWS account. On a busy form this adds up fast,
and a backfill over months of history bills all of it at once.

Start with `DRY_RUN=true` to watch the payloads without sending them, then
enable a single form, then widen. The **Pipeline enabled** switch at the top of
the Setup tab pauses a form without losing its configuration.

---

## Which NLP API your server speaks

kpi has shipped three generations of the automated-NLP API. The pipeline
implements all three and picks one per form at runtime, so you normally do not
need to care — but when something 404s, this is the table to read.

| Dialect | Configure with | Trigger with | Seen on |
|---|---|---|---|
| `supplement` | `POST /assets/{uid}/advanced-features/` | `PATCH /assets/{uid}/data/{root_uuid}/supplement/` | current kpi |
| `20250820` | `advanced_features` on the asset | `POST /assets/{uid}/advanced_submission_post/` | interim releases |
| `legacy` | `advanced_features` on the asset | same, with `googlets` / `googletx` keys | kpi 2.024–2.026 |

Detection probes `/advanced-features/` first, because a 200 there is a
definitive answer where sniffing the submission schema is a guess. Pin it with
`SCHEMA_DIALECT` if you need to.

### The `supplement` dialect

Three things differ from the older dialects, and they are the reason the older
payloads fail against a current server:

- The submission uuid moved out of the request body and into the URL path.
- There is no `status: "requested"`. Issuing the `PATCH` *is* the request, and
  `language` is the only required field.
- Language and locale are separate fields, so `fr-FR` is sent as
  `{"language": "fr", "locale": "FR"}`.

A transcription request looks like this in full:

```
PATCH /api/v2/assets/<uid>/data/<root_uuid>/supplement/
{"_version": "20250820",
 "main_impacts": {"automatic_google_transcription": {"language": "en"}}}
```

Results come back as an append-only `_versions` list per action, newest last,
each wrapping a `_data` object whose `status` is `in_progress`, `complete`,
`failed`, or `deleted`. Translations are keyed by language and qualitative
answers by question uuid. Qualitative analysis is requested one question at a
time — the schema accepts a single `uuid`, not a list.

**A finished result is not usable until it is accepted.** A completed
transcript sits unaccepted (no `_dateAccepted` on its latest version) and
anything downstream refuses to run against it:

```
PATCH .../supplement/  {"_version": "20250820",
  "main_impacts": {"automatic_google_translation": {"language": "es"}}}
-> 400 {"detail": "No transcription found"}
```

despite the transcript being right there with `status: "complete"`. So the
sequence per question is really: request transcription → wait → **accept it**
→ request each translation → wait → **accept each** → request each analysis
question. The pipeline does all of that unattended; the acceptance steps are
the ones that are easy to miss when reading the schema alone.

On this dialect the **server owns the configuration**. The pipeline reads
`/advanced-features/` to learn which questions to transcribe, which languages
to translate into, and which analysis questions to run, rather than imposing
its own. That matters most for the analysis questions, whose uuids have to
match the ones the server already holds. If a form has *no* advanced-features
rows at all, the pipeline creates the transcription and translation rows from
that form's settings; it never invents analysis questions.

---

## Known version sensitivity

`kobo/apps/subsequences/` has been refactored upstream more than once. The two
older shapes live in `app/payloads.py` and the current one in
`app/supplement.py`; see the dialect table above.

**Check what your server accepts before trusting any of it.** Current kpi
publishes a full OpenAPI document, which is the fastest way to settle a
question:

```bash
curl -sH "Authorization: Token $KOBO_TOKEN" \
  "$KOBO_URL/api/v2/schema/?format=json" | jq '.paths | keys[]' | grep -Ei 'advanced|supplement'
```

If that returns paths, your server speaks the `supplement` dialect. If it
404s, fall back to `python -m app.cli introspect <asset_uid>`, which prints the
older `advanced_submission_schema`. A server that has neither does not have
automated NLP enabled at all.

Symptoms of a dialect mismatch are distinctive: every request 404s while the
asset itself reads fine, and the 404 body is an HTML page rather than JSON —
that means Django never routed the request to the API.
