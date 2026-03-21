# Expense Tracker

Mobile-first personal finance tracker with:

- SQLite-backed ledger storage
- FastAPI web UI and JSON API
- transfer-aware transactions
- receipt/file attachments
- SMS intake and review queue
- OpenAI-backed draft generation with typed structured output
- prompt preview tooling for inspecting the exact LLM request

## Requirements

- Python 3.10+
- a virtual environment with project dependencies installed

## Install

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e .
```

Generate a bearer token for protected clients:

```bash
. .venv/bin/activate
expense-tracker generate-auth-token --write-file /tmp/expense-tracker.auth
```

## Simple LXC production install

For a single LXC guest with an external reverse proxy, use:

```bash
sudo ./scripts/install_lxc.sh
```

The script:

- creates `.venv` and installs the app
- prepares `/var/lib/expense-tracker`
- prepares `/etc/expense-tracker`
- generates an auth token file if one does not exist
- installs a `systemd` unit for the app

After the script runs, update `/etc/expense-tracker/expense-tracker.env` with the real hostnames for `EXPENSE_TRACKER_ALLOWED_HOSTS`.

## Run the app

```bash
. .venv/bin/activate
expense-tracker serve --host 0.0.0.0 --port 8000
```

The default database lives at `data/ledger.db`.

If an older `data/ledger.json` exists, it is migrated automatically on first load and backed up to `data/ledger.json.bak`.

## Production configuration

The app now uses environment-first configuration for production secrets and runtime settings.

Supported variables:

- `EXPENSE_TRACKER_AUTH_TOKEN`
- `EXPENSE_TRACKER_AUTH_TOKEN_FILE`
- `EXPENSE_TRACKER_OPENAI_API_KEY`
- `EXPENSE_TRACKER_OPENAI_API_KEY_FILE`
- `EXPENSE_TRACKER_SECURE_COOKIES`
- `EXPENSE_TRACKER_ALLOWED_HOSTS`
- `EXPENSE_TRACKER_COOKIE_NAME`

For production, prefer the `*_FILE` variants with root-owned `0600` files under `/etc/expense-tracker/`.

## Authentication

If an auth token is configured, both the web UI and the JSON API are protected.

- browser users log in through `/auth/login`; the app sets an `HttpOnly` session cookie that lasts for the browser session
- API clients and Tasker can send `Authorization: Bearer <token>`
- `/healthz` stays unauthenticated for local health checks and proxy probing

## Documentation

- `docs/ARCHITECTURE.md` explains the module layout, database shape, request flow, auth/config model, SMS pipeline, and OpenAI integration.
- `docs/WORKFLOWS.md` documents the common operator and development workflows, including backup/restore, token rotation, LXC install, and service restart steps.

## Database backup and restore

Create a timestamped local backup:

```bash
mkdir -p backups
cp data/ledger.db "backups/ledger-$(date -u +%Y%m%dT%H%M%SZ).db"
```

Restore a backup while the server is stopped:

```bash
cp backups/ledger-YYYYMMDDTHHMMSSZ.db data/ledger.db
```

The `backups/` directory is gitignored and intended for local safety copies only.

## Seed demo data

```bash
. .venv/bin/activate
expense-tracker seed-demo-data
```

This imports `data/sms_pipeline/mock_ledger_seed.json` into the active SQLite database.

## Import a real ledger CSV

```bash
. .venv/bin/activate
expense-tracker import-ledger-csv --csv-file /root/All-Accounts_2.csv
```

This clears the current ledger, attachments, and queued SMS review items before importing the CSV directly into SQLite.

## Import historical SMS into SQLite

```bash
. .venv/bin/activate
expense-tracker import-sms-history --sms-csv /root/All_Conversations_2026-03-20.csv
```

This stores raw SMS rows, extracted markers, match reasons, and matched transaction links for future retrieval.

## Run tests

```bash
. .venv/bin/activate
python -m unittest discover -s tests -v
```

## Core CLI commands

Add a transaction:

```bash
expense-tracker add \
  --account "Cash" \
  --payee "Amazon" \
  --category "General" \
  --subcategory "Delivery" \
  --amount "-499.00" \
  --date "2026-03-19" \
  --notes "Order"
```

Run the SMS analysis pipeline:

```bash
expense-tracker analyze-sms \
  --transactions-csv /root/All-Accounts_2.csv \
  --sms-csv /root/all_sms.csv
```

List transactions with search:

```bash
expense-tracker list --search "amazon"
```

## Web and API overview

Main pages:

- `/` dashboard
- `/transactions` ledger list
- `/transactions/new` add transaction
- `/reviews` SMS review queue
- `/reviews/{review_id}/llm-request` inspect the exact LLM request for a review
- `/manage` metadata management

Key API endpoints:

- `GET /api/transactions`
- `POST /api/transactions`
- `GET /api/reviews`
- `GET /api/reviews/{review_id}/llm-request`
- `POST /api/sms/intake`

Example SMS intake request:

```bash
curl -X POST http://127.0.0.1:8000/api/sms/intake \
  -H 'Authorization: Bearer YOUR_TOKEN' \
  -H 'Content-Type: application/json' \
  -d '{
    "sender": "JM-HDFCBK-S",
    "phone": "JM-HDFCBK-S",
    "content": "Sent Rs.88.00 From HDFC Bank A/C *2054 To Amazon On 19/03/26 Ref 12345",
    "received_at": "2026-03-19T15:50:00Z"
  }'
```

## OpenAI draft generation

The review workflow uses `gpt-5-mini`.

For production, configure the API key with `EXPENSE_TRACKER_OPENAI_API_KEY` or `EXPENSE_TRACKER_OPENAI_API_KEY_FILE`.

The legacy `/root/openai.key` fallback still works for local compatibility, but it is no longer the recommended production path.

The current app path uses the OpenAI Python SDK typed parse flow:

- system prompt loaded from `expense_tracker/prompts/review_draft_prompt.txt`
- user payload containing the SMS, extracted markers, similar history, allowed metadata, and transfer rules
- typed structured output parsed through `responses.parse(..., text_format=ReviewDraftTextFormat)`

If the OpenAI call fails, the app records an explicit heuristic fallback draft instead of silently dropping the request.

## Live operations

Restart the server:

```bash
ss -ltnp | grep ':8000'
kill <PID>
. .venv/bin/activate
nohup expense-tracker serve --host 0.0.0.0 --port 8000 >/tmp/expense-tracker.log 2>&1 &
```

Check health:

```bash
curl http://127.0.0.1:8000/healthz
```

Inspect the current live review queue:

```bash
curl http://127.0.0.1:8000/api/reviews
```

## Repository notes

- Runtime data stays under `data/` and is gitignored.
- Local database backups live under `backups/` and are gitignored.
- The project is web-first; the older TUI has been removed.
- Review drafts are stored in SQLite alongside the main ledger data.
