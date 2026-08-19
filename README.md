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

> Not affiliated with or endorsed by KoboToolbox. It drives their public API
> as any client would, and the transcription, translation and analysis are
> performed by KoboToolbox itself — this only decides what to ask for, and when.

## Architecture

```
     browser  ──►  /admin/   (setup + queue dashboard)
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
| **Connection** | Kobo server URL, API token, TLS verification, and the webhook URL + shared secret. Test the credentials before saving. Yours alone. |
| **Users** | administrators only: approve pending sign-ups, disable or delete accounts, grant admin |
| **Forms** | lists every survey on the server, with its submission count and whether the pipeline is set up on it |
| **Setup** | opens from **Configure** on a form; it has no tab of its own because it always belongs to one form — see below |
| **Monitor** | live queue tiles, per-submission stage, error detail, retry, and the raw `_supplementalDetails` Kobo currently holds |

On a fresh install with no `KOBO_TOKEN` in `.env`, signing in drops you
straight on the Connection tab. The worker idles quietly until credentials
exist rather than crash-looping, and picks them up within one tick of you
saving — no restart, no `docker compose` command.

The Setup tab follows the shape of the problem: things that belong to the
**form**, then things that belong to each **recording**.

**Turning it on.** The Forms tab has a **Turn on** button per form. It asks
for the audio language, then configures every recording that has none,
registers the webhook, and marks the form active — the three things that
previously had to be done separately, in two places, where forgetting the
webhook quietly downgraded the form to five-minute polling.

It only ever adds. A recording that is already configured is left exactly as
it is, because the rows are append-only and a wrong audio language cannot be
taken back. Use **Configure** for anything beyond the defaults: analysis
questions, per-recording languages, backfill.

**Form setup** — done once per form:

1. **Server capability check** — reports which NLP API the server speaks, and
   whether it can do this work at all. There is nothing to choose: the app
   probes `/advanced-features/` and either gets an answer or does not.
2. **Trigger** — registers the Kobo REST Service webhook with the shared-secret
   header, and shows its success/failure counts. By default it posts **only the
   submission id**: the receiver reads an id and queues it, and never looks at
   the answers, so there is no reason for interview content to leave
   KoboToolbox. Tick *Send the whole submission* only if you have a reason to,
   and note that a restricted payload loses nothing — the poller picks up
   anything the hook misses.
3. **Backfill** — queue existing submissions.

**Audio questions** — a row per recording showing what that recording actually
has on the server: audio language, translation targets, how many analysis
questions, and how many of those the model answers. Differences between
recordings are visible at a glance, and anything unconfigured says so.

Choosing one opens its editor:

1. **Languages** — source language and translation targets, read from and
   written back to the server for that recording.
2. **Analysis questions** — a visual builder (free text, select one/multiple,
   tags, integer, note) with choices and hints on both questions and choices.
   It loads the recording's existing questions first, so you add to what is
   really there; the starter template fills in only what is missing. UUIDs are
   held stable per recording, so re-applying never duplicates columns.
3. **Apply** — writes it to that recording, plus any others ticked under
   *Also apply to*. "Preview payload" shows the exact JSON first.

A recording added to a form *after* it was set up is picked up on the next
pass and given transcription with the form's settings — the worker judges each
recording on its own rather than treating a form with any configuration as
finished.

Forms enabled in the UI are watched by the worker automatically — `ASSET_UIDS`
in `.env` is optional and simply adds to that list.

`ADMIN_PASSWORD` empty disables the break-glass login; with no accounts
created either, that is CLI-only mode. The sign-in page then stops offering
the recovery password rather than sending you to a 503, and the CLI names
`KOBO_URL`/`KOBO_TOKEN` in its own errors instead of pointing at a screen
that is not there. Sessions are
signed cookies keyed on the password itself, so changing the password logs
everyone out. Set `ADMIN_COOKIE_SECURE=false` only if you browse over plain
HTTP.

## Accounts

The pipeline can serve several people, each with their own KoboToolbox
connection, forms, queue and results. Nothing is shared between accounts.

Administrators can also create an account outright from the **Users** tab —
username, password and role — which skips the sign-up-then-approve round trip
when you are handing someone access directly.

**Signing up is open; being let in is not.** Anyone who can reach the app can
request an account, but it stays `pending` until an administrator approves it
on the **Users** tab. That gate matters because an active account can start
billable transcription and analysis work on the AWS credentials behind its
Kobo server.

The **first account created becomes an active administrator** — someone has to
be able to approve the second. It also adopts anything configured before
accounts existed, so upgrading an existing deployment keeps its forms, queue
and saved credentials.

`ADMIN_PASSWORD` still works as a break-glass login: leave the username blank on
the sign-in form. It resolves to the first administrator account when one
exists, so its actions are attributed to a real owner. Keep it for recovery,
and use a real account day to day.

| Action | Who |
|---|---|
| Create an account directly, with a role | administrators |
| Approve, disable, delete accounts | administrators |
| Grant or remove administrator | administrators (never the last one, never yourself) |
| Everything else | the account that owns the form |

Passwords are hashed with scrypt from the standard library — no new
dependency. Session cookies are signed and keyed on the password hash, so
changing a password ends that account's other sessions immediately.

### What "their own" means

| Scoped per account | Shared |
|---|---|
| Kobo server URL and API token | the deployment's `.env` (ports, intervals, log level) |
| Which forms are managed, and their settings | the worker process itself |
| Analysis questions written to Kobo | |
| The job queue, its history and notes | |
| Webhook secret and public URL | |

Webhooks are registered per account at
`/kobo/hook/<account-id>/<asset-uid>`, so the receiver knows whose credentials
to use. The older `/kobo/hook/<asset-uid>` URL still works and is attributed to
whichever account watches that form; if more than one does, it answers 409 and
asks you to re-register.

### Where credentials live

Two sources, and the UI wins:

| Priority | Source | Notes |
|---|---|---|
| 1 | Connection tab | stored in `app_settings` in the SQLite volume, shared by both containers |
| 2 | `.env` | used for any field the UI has not set |

Blanking a field in the UI removes the override and falls back to `.env`;
**Reset to server defaults** drops all of them at once. (The UI itself never
names `.env` — whoever configures a form usually cannot read it — so the
button talks about "server defaults" and the sign-in page about the "recovery
password".) Secrets are never sent back to
the browser — the form shows a masked hint (`ui-••••••456`) and only writes a
new value when you actually type one, so saving the page without retyping your
token does not wipe it.

`ADMIN_PASSWORD` is deliberately **not** editable in the UI. It is the
credential guarding that very screen, so keeping it in `.env` means a bad
value can never lock you out of the box that fixes it.

Two caveats worth knowing:

- Tokens are stored **plaintext** in `/data/pipeline.db`, exactly as they would
  be in `.env`. Treat the Docker volume as secret material and do not commit it.
- The example values shipped in `.env.example` — `your_api_token_here`,
  `https://kf.example-partner.org`, `change-me-too` — are rejected at startup
  rather than used. Leaving one in place used to surface as a DNS error or a
  401 on every request; now it says which field is still unset.
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
`https://autoqa.example.org/kobo/hook/<asset_uid>` with your
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

## Hardening

What the app does for itself, so it does not depend on how it is fronted:

| Measure | Where |
|---|---|
| `/login` limited to 10 attempts per 5 min per IP; `/register` to 5/hour | `app/ratelimit.py` |
| HSTS, CSP, `X-Frame-Options: DENY`, `nosniff`, `Referrer-Policy` on every response | `app/webhook.py` |
| OpenAPI schema and both doc UIs disabled | `app/webhook.py` |
| Passwords hashed with scrypt; sessions signed and keyed on the password hash | `app/users.py` |
| Constant-time comparison for the recovery password, webhook secret and session signatures | throughout |
| Tokens never returned to the browser — only a masked hint | `app/runtime.py` |

Rate-limit state is per-process and resets on restart. That is deliberate: it
is a brake on automated abuse, not an audit trail, and it costs no dependency.
The client address comes from `X-Forwarded-For` and is used **only** as a
bucket key, never for access control, so a forged value throttles its own
sender and nothing else.

What still has to be right in your deployment:

- **Force SSL and HTTP/2** on the proxy host, so `http://` redirects rather
  than serving. The app sends HSTS, but a browser only honours it after one
  successful HTTPS response.
- **Publish only 80/443.** The API's `127.0.0.1:8077` binding is for local
  debugging; it must not become `0.0.0.0`.
- **Treat the SQLite volume as secret material** — API tokens are stored in
  it in plaintext, exactly as they would be in `.env`.

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
| Domain Names | `autoqa.example.org` |
| Scheme | `http` |
| Forward Hostname / IP | `kobo-autoqa-api` |
| Forward Port | `8000` |
| Websockets Support | on |
| Block Common Exploits | on |
| SSL → Certificate | request a new Let's Encrypt cert |
| SSL → Force SSL + HTTP/2 | on |

Then set `PUBLIC_WEBHOOK_URL=https://autoqa.example.org` in `.env` (or on the
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
| Worker logs `Poll failed` against a server you never configured | It resolved the wrong account's connection. `docker compose logs worker \| grep 'connected to'` prints the server each account is using; a deployment that predates accounts is account 0. |
| **transcription ... has been running N min; asking again** | The job is still marked `in_progress` well past a normal run. Re-requesting produces a fresh attempt that completes, so the pipeline nudges it every `NLP_STALL_MINUTES` (30) rather than waiting indefinitely. Observed once on a live server: a job sat 25 minutes, and a nudge finished it in under a second. |
| Jobs cycle in **Transcribing** and never finish | Kobo accepted the request but its NLP is not completing — AutoQA may not be enabled server-side, or the audio language is not ASR-supported. Check the supplement with "View". |
| Everything lands in **Failed** with a 400 | Read the note on the job — the pipeline records the server's own error. Common ones: the audio language is not a recognition model (see [Audio languages need a region](#audio-languages-need-a-region)), or a translation target equals the source language. |
| Webhook shows failures in Kobo | Secret mismatch (403) or the form is not enabled here (404). Re-register the hook from the Setup tab after any secret change. |
| **transcription returned no speech** | Transcription succeeded but produced an empty string — Google heard no speech in the recording. Check the clip is audible and long enough, and that the audio language matches what was actually spoken. Nothing downstream can run without text. |
| **translation gave up — Target language can't be equal to source language** | A translation target is the same language as the audio. Remove it from "Translate into"; the Setup tab now warns before you save and drops it on apply. |
| NPM returns **502** | The `api` container is down, or it is not on the `nginxproxy_default` network — confirm with `docker exec nginxproxy-app-1 curl -s -o /dev/null -w '%{http_code}' http://kobo-autoqa-api:8000/healthz`. |

A job parked in `failed` is not lost. **Retry all failed** on the Monitor tab
resets them to `new` and the worker picks them straight back up.

### "Passes" is not a retry count

Passes and failures are counted separately, and only failures park a job. This
matters because Kobo's NLP is asynchronous: an eleven-minute transcription is
perfectly normal and would otherwise exhaust a budget meant for errors, so a
slow submission used to be reported as a broken one.

A failed action is retried at most `MAX_ACTION_FAILURES` (2) times before the
pipeline stops and records the server's own explanation. Most failures here are
permanent — empty source text, or a target language equal to the source — and
re-requesting them costs a billable call each time for a result that cannot
change.

The number on the Monitor tab counts how many times the worker has looked at a
submission, and a healthy run uses several. The worker never blocks on Kobo's
async NLP: each pass either issues one request or checks whether an earlier one
finished, then reschedules itself. On the current API a single submission with
one recording, three translations and four analysis questions takes roughly:

```
1  request transcription        5  accept each translation
2  poll, accept the transcript  6  request each analysis question
3  request the translations     7  poll for the answers
4  poll                         8  done
```

So 6–10 passes is the normal shape of success. Retries after an *error* are
visible in the Detail column, and only those are worth investigating.

You do not have to infer any of this. Every pass records what it did and what
it is waiting for, shown in the Monitor tab's Detail column and logged:

```
pass 4: translation: requested 3 — re-checking in 20s
pass 5: translation: accepted 3 — re-checking in 20s
pass 6: analysis: requested 8 — re-checking in 20s
pass 9: 2 transcript(s) ready, moving to translate
pass 11: complete: 16 analysis answer(s)
```

Errors still take precedence in that column and are shown in red; the note is
what you see when a job is simply working.

```bash
docker compose logs -f worker | grep 'pass '
```

---

## Tuning

The first three are **defaults**. The Setup tab saves them per form, and a
form's own values win — so one form can be `fr-FR → en` while another is
`ar → en,fr`. The scheduling variables below are process-wide.

| Variable | Effect |
|---|---|
| `TRANSCRIPT_LANGUAGE` | BCP-47 source language, e.g. `fr-FR`. Must be an ASR-supported language (80 languages / 145 regional variants). |
| `TRANSLATION_LANGUAGES` | Comma-separated targets, e.g. `en,es`. Empty skips translation entirely. |
| `QUAL_SOURCE_LANGUAGE` | Which text AutoQA reads. Empty = the original transcript. |
| `ASYNC_POLL_SECONDS` | How often to re-check a running NLP job. 20s is fine; lower just burns API calls. |
| `NLP_STALL_MINUTES` | How long a job may claim to be running before it is nudged with a fresh request. Kobo can leave one `in_progress` long past a normal run; asking again produces an attempt that completes. Set it above your typical transcription time — 11 to 25 minutes on the servers seen so far. |
| `POLL_INTERVAL_SECONDS` | Catch-up poll frequency. 300s is a good default; drop to 60s if the webhook is not registered. |
| `MAX_FAILURES` | Real errors before a submission is parked in `failed`. Polls do **not** count towards it, so a slow transcription is never mistaken for a broken one. |
| `MAX_JOB_AGE_HOURS` | Wall-clock ceiling. A submission still unresolved after this long stops being chased. |
| `MAX_ATTEMPTS` | Runaway guard only. Waiting on async NLP burns passes, so keep this well clear of a healthy run — `MAX_FAILURES` is the real limit. |

---

## Testing without a Kobo server

A fake KoboToolbox is included. It exposes the same endpoints and completes NLP
jobs instantly, so you can exercise the whole flow offline. It models the
append-only behaviour of `advanced-features` faithfully — `PUT` and `PATCH`
merge params, `DELETE` answers 405 — because an earlier version replaced them
instead, and that one difference hid three bugs that reached production. It can impersonate
either generation of the API:

```bash
curl -X POST http://127.0.0.1:8899/__mode/supplement   # current API
curl -X POST http://127.0.0.1:8899/__reset             # back to legacy, clean state
```

In `supplement` mode the older `advanced_submission_*` endpoints 404 exactly as
they do on a real current server, and the strict parameter schema and the
accept-before-use rule are both enforced — so a payload the real server would
reject fails here too.

```bash
pip install -r requirements.txt
python tests/mock_kobo.py &                    # http://127.0.0.1:8899
python tests/test_admin_flow.py                # 40 assertions, all should PASS
python tests/test_supplement_flow.py           # 35 assertions, current NLP API
python tests/test_accounts.py                  # accounts, approval, isolation, rate limits
python tests/test_first_run.py                 # a clean install's first run, start to finish

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

Detection is automatic and is a probe rather than a guess: `/advanced-features/`
either answers or 404s. The Setup tab shows the result and offers no override,
because on any given server every value except the detected one names an
endpoint that is not there. `SCHEMA_DIALECT` in `.env` remains as an escape
hatch if you ever need to pin it.

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

### Requesting is not the shape it is stored in

Transcription takes the whole regional code in `language`, and a request has
**no `locale` field at all**:

```json
{"language": "en-GB"}          ✅ accepted
{"language": "en", "locale": "en-GB"}   ❌ 400 Invalid payload
{"language": "en", "locale": "GB"}      ❌ 400 Invalid payload
```

The stored result comes back as `{"language": "en", "locale": "en-GB"}`, which
is where the confusion comes from — reading a result and echoing its shape
back is rejected.

### Audio languages need a region

There are **no bare languages in Google's ASR list**. Ask the server:

```bash
curl -s -H "Authorization: Token $KOBO_TOKEN" \
     "$KOBO_URL/api/v2/languages/fr/" | jq .transcription_services
# { "goog": { "fr-CA": "fr-CA", "fr-FR": "fr-FR" } }      <- no plain "fr"
```

Transcription therefore carries both fields, and the locale is the *whole*
regional code, exactly as a live server records it:

```json
{"language": "en", "locale": "en-GB"}
```

Translation is the opposite: `translation_services` lists a single bare code
per language, so targets never carry a region.

The Setup tab looks this up as you type and offers the valid variants as
buttons. Take them — a language the recogniser does not have produces a
*successful* transcription containing an empty string, which then fails every
downstream translation with `400 Empty request`.

And note what no API can check for you: **the language must match what was
actually spoken.** English audio transcribed as French also returns an empty
string, with no error anywhere.

### Advanced-features rows are append-only

Worth knowing before you save a form, because it is not reversible. On current
kpi builds the per-question configuration rows accept additions but no
removals:

```
/advanced-features/            GET, POST
/advanced-features/<uid>/      GET, PUT, PATCH        (no DELETE)
```

and both `PUT` and `PATCH` **merge** `params` rather than replacing them —
sending `[{"language":"es"}]` to a row holding `es, fr` leaves it holding
`es, fr`. So a translation target or an analysis question, once written to a
form, cannot be removed through the API at all.

Three consequences worth knowing before you apply a configuration:

- **An analysis question cannot be deleted.** Removing one in the editor
  removes it from the editor only; it stays on the form and keeps being
  answered, and billed, on every submission. The Setup tab now says so when
  you apply, and the ✕ button says so before you click it.
- **A translation target cannot be removed** once saved.
- **The audio language can only be added to**, so a recording edited from
  `fr` to `en` holds both; the newest wins.

The pipeline copes by filtering at request time rather than trusting the
stored configuration: a target equal to the source language is skipped, and an
an analysis question is skipped when the model cannot answer its type **or
when nothing defines it any more** -- an auto-answer entry whose question was
removed is refused with `400 Invalid qualitative analysis question uuid` on
every submission otherwise. Both are reported in the Monitor tab's note so the reason is visible.

The **audio language** is the case where this bites hardest, because only one
value can apply. Changing a recording from `fr` to `en` leaves the row holding
`["fr", "en"]`, so the pipeline takes the **last** entry — the one saved most
recently — and the Setup tab notes which older values are still stuck on the
form. Reading the first entry instead meant a form kept transcribing in the
language you had just changed away from, with Save & apply appearing to do
nothing.

Practically: **get the languages right before you apply**, and treat the
Setup tab's warnings as blocking rather than advisory.

### Configuration is per audio question

On this dialect a configuration row is keyed on `(question_xpath, action)`, so
**analysis questions belong to one recording, not to the form**. A form with
three audio questions needs three sets of rows.

The Setup tab handles as many as you have: tick every recording the
configuration should apply to and apply once. Each gets its own copy, so they
can also differ — use **Edit** on a recording to load its saved configuration
and change it independently.

Question uuids are minted **per recording**, never shared, because analysis
answers are stored per recording under the question uuid. Re-applying is
stable: a question already on a recording keeps its uuid (matched by uuid, then
by type and label), so editing a set and applying it again does not orphan
existing answers or pile up duplicates.

Applying to several recordings at once treats them differently on purpose:

- the recording open in the editor is **replaced**, so deleting a question
  there actually removes it;
- the others are **merged into** — the applied questions are added or updated,
  and anything they have of their own is left alone.

Without that split, ticking a second recording would quietly strip whatever
questions belonged only to it.

The Setup tab loads the recording's current questions from the server before
you edit, so you are always adding to what is really there. **Load humanitarian
template** adds only the template questions that are missing and reports how
many it skipped, rather than overwriting your set. Two questions with the same
type and wording are flagged in the editor, since they would otherwise become
two near-identical columns in every export.

Four actions matter:

| Action | Holds |
|---|---|
| `automatic_google_transcription` | `[{"language": "en"}]` |
| `automatic_google_translation` | one entry per target language |
| `manual_qual` | the analysis **question definitions** — uuid, type, labels, hint, choices |
| `automatic_bedrock_qual` | which of those question uuids the AI should answer |

Note the split in the last two: `manual_qual` defines the questions,
`automatic_bedrock_qual` selects which get answered automatically.

**Not every question type can be answered automatically**, and this is a sharp
edge: the configuration endpoint accepts any type under
`automatic_bedrock_qual`, and only the *trigger* rejects it — once per
submission, forever.

| Type | Answered by the model |
|---|---|
| `qualText`, `qualSelectOne`, `qualSelectMultiple`, `qualInteger` | yes |
| `qualTags` | **no** — `400 "Invalid qualitative analysis question uuid"` |
| `qualNote` | no — a heading for human coders |
| `qualAutoKeywordCount` | no — computed by the server |

The pipeline filters these out when writing a configuration, and also skips
them at trigger time, so a configuration written by hand or by an older version
degrades to a warning rather than parking every submission:

```
WARNING app.supplement: Skipping qualTags question 0ae66c93 on main_impacts:
        not auto-answerable
```

Those questions are still defined on the form and still collectable by a human
coder — they just are not sent to the model.

Both questions **and individual choices** can carry a **hint** — extra guidance
for the model, in the same shape on either:

```json
{"hint": {"labels": {"_default": "1 is lowest, 5 is best"}}}
```

The Setup tab exposes it as a second field under each question and beside each
choice, so you can say what a question means *and* when to pick a particular
option. Choice hints are worth the effort on select-one scales, where the label
is often just a number.

The pipeline reads all of this from the server rather than imposing its own,
because the analysis question uuids have to match what the server holds. If a
form has *no* rows at all, it creates the transcription and translation rows
from that form's settings; it never invents analysis questions.

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

---

## Licence

MIT — see [LICENSE](LICENSE). Use it, fork it, run it commercially; just keep
the copyright notice.
