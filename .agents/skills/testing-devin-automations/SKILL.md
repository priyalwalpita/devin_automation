---
name: testing-devin-automations
description: How to run and end-to-end test the Devin Automations orchestrator (FastAPI + SQLite + ops dashboard) locally in keyless mock mode, including webhook signing, timing expectations, and known failure modes.
---

# Testing Devin Automations

Service: FastAPI orchestrator that turns GitHub issues labeled `devin:remediate` into Devin
sessions, with a server-rendered ops dashboard. Everything can be exercised with **no Devin key,
no GitHub token and no network** via `MOCK_DEVIN=true`.

## Bring up a clean instance

```bash
cd <repo>
cp .env.example .env                          # MOCK_DEVIN=true is the default
docker compose down -v && sudo rm -rf data    # data/ is root-owned; sudo is required
env -u GITHUB_TOKEN -u DEVIN_API_KEY docker compose up --build -d
curl -sS localhost:8080/healthz               # {"ok":true,"mock":true,...}
```

- **Always strip `GITHUB_TOKEN` and `DEVIN_API_KEY` with `env -u`.** `docker-compose.yml` passes
  the host shell's values straight through; if a real token is present the app tries to comment on
  the (usually nonexistent) target repo and floods the logs with 404 warnings. With them unset,
  every GitHub action is logged as `[no GITHUB_TOKEN] would comment …`, which is also the proof
  that a test stayed keyless.
- **Keep `GITHUB_WEBHOOK_SECRET` exported** if you want to test the HMAC paths — the compose
  pass-through wins over the empty value in `.env`. Quick check that it took effect:
  an unsigned `POST /webhook` must return HTTP 401.
- Reset between runs by deleting `data/`; otherwise funnel counters (`received`, `ignored`,
  `duplicates`) accumulate across runs and your expected values will be off. Note that even a
  throwaway probe request increments `received`/`ignored`, so reset *after* any smoke-testing.
- `docker-compose.yml` only passes through `DEVIN_API_KEY`, `DEVIN_ORG_ID`, `GITHUB_TOKEN`,
  `GITHUB_WEBHOOK_SECRET`, `TARGET_REPO`, `MOCK_DEVIN`, `MAX_ACU_LIMIT`,
  `MAX_CONCURRENT_SESSIONS`. Anything else (notably `POLL_INTERVAL_SECONDS`) must be edited in
  `.env` — you cannot speed the poller up from the shell.
- Changing an env var: `env -u GITHUB_TOKEN MAX_CONCURRENT_SESSIONS=1 docker compose up -d`
  recreates the container while the `./data` bind mount (and therefore all history) persists —
  handy for proving persistence for free.

## Triggering webhooks

`./simulate.sh` replays `sample_payloads/issue_labeled.json` (issue #101) and signs it when
`GITHUB_WEBHOOK_SECRET` is exported. For custom payloads / edge cases, a signing helper:

```bash
BODY="$(cat payload.json)"
SIG="sha256=$(printf '%s' "$BODY" | openssl dgst -sha256 -hmac "$GITHUB_WEBHOOK_SECRET" | awk '{print $2}')"
curl -sS -X POST localhost:8080/webhook -H 'Content-Type: application/json' \
  -H 'X-GitHub-Event: issues' -H "X-GitHub-Delivery: test-$(date +%s%N)" \
  -H "X-Hub-Signature-256: $SIG" --data "$BODY"
```

Generate extra issues by copying the sample payload and changing `issue.number`,
`issue.title` and `issue.html_url`. Intake is idempotent **by issue number**, so re-using a
number returns `duplicate_ignored` instead of starting a new remediation.

Expected intake responses (useful as exact assertions):
`{"status":"queued","issue_number":N}` · `{"status":"duplicate_ignored",…}` ·
HTTP 401 `{"status":"invalid_signature"}` · `{"status":"ignored","reason":"not an issues event"}` ·
`… "reason":"action=unlabeled label=devin:remediate"` · `… "reason":"malformed json"` ·
`… "reason":"missing issue number"`.

## Timing expectations (mock mode)

The mock session opens a PR at 15s and finishes at 30s (`MockDevinClient.PR_AT` / `.DONE_AT`),
but the poller only samples every `POLL_INTERVAL_SECONDS` (default 20). So:

- PR link appears ~20-25s after the webhook, `COMPLETED` ~35-45s after.
- Completed rows show outcome `fixed` and ACUs `2.40`; est. cost = ACUs × `ACU_COST_USD`
  (default $2.00 → $4.80 for one remediation). Time-to-PR/session-duration numbers are
  poller-quantised (e.g. 24s / 44s), not exactly 15s/30s — don't assert on the raw mock constants.
- The dashboard refreshes itself every 10s via `<meta http-equiv="refresh">`; letting it
  auto-refresh (rather than pressing F5) is worth demonstrating in a recording.

## Things worth asserting on the dashboard

Empty state names both trigger paths (`devin:remediate` label and `./simulate.sh`).
Unavailable metrics render `n/a`, never a fake `0.00`. The heartbeat chip reads `poll Ns ago` and
is green while lag ≤ 3× poll interval. The audit trail should show exactly
`webhook_received → queued → session_started → pr_opened → completed` once per issue — verify
non-duplication directly in SQLite rather than by eyeballing:

```bash
sudo .venv/bin/python -c "import sqlite3;print(sqlite3.connect('data/automations.db').execute(
 'SELECT issue_number,kind,COUNT(*) FROM events GROUP BY 1,2 HAVING COUNT(*)>1').fetchall())"
```

## Known failure mode — restart while a session is RUNNING

`MockDevinClient` stores sessions in a process-local dict, so `docker compose restart` while a
row is `running` loses the session; the poller then logs
`poll failed for #N (…): unknown mock session …` on every cycle and **the row stays `RUNNING`
forever**. Because `sync_session` swallows all polling exceptions with no retry cap, no age-based
timeout and no transition to `failed` (and `requeue_stranded_launches()` only rescues rows stuck
in `launching`), the wedged row keeps occupying a concurrency slot and later issues stay `queued`
indefinitely. This may have been fixed since — if you see rows stuck in `RUNNING`, check
`/logs` for `poll failed` before assuming the poller died.

Restarting while a row is still `queued` recovers cleanly, so time restart-resilience tests
deliberately depending on which case you want to exercise.

## Baseline suite

`.venv/bin/python -m pytest -q` (7 tests, in-process, no network). Sanity only — it uses the same
mock path as the app, so it is not evidence that the deployed service works.

## Devin Secrets Needed

None. Everything above runs keyless. `GITHUB_WEBHOOK_SECRET` is only needed to exercise the HMAC
paths and any value works — it just has to match between the service and the signing helper.
