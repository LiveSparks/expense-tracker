# Expense Tracker

Mobile-first personal expense tracker with:

- SQLite-backed ledger storage
- FastAPI web UI and JSON API
- transfer-aware transactions
- receipt/file attachments
- SMS intake and review queue
- OpenAI-backed draft generation with heuristic fallback

## Requirements

- Python 3.10+
- a virtual environment with project dependencies installed

## Install

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e .
```

## Run the app

Start the web server:

```bash
. .venv/bin/activate
expense-tracker serve --host 0.0.0.0 --port 8000
```

The default database lives at `data/ledger.db`.

If an older `data/ledger.json` exists, it is migrated automatically on first load and backed up to `data/ledger.json.bak`.

## Seed demo data

Import the generated mock ledger data:

```bash
. .venv/bin/activate
expense-tracker seed-demo-data
```

This imports `data/sms_pipeline/mock_ledger_seed.json` into the active SQLite database.

## Import a real ledger CSV

Replace the current app data with transactions from the legacy export:

```bash
. .venv/bin/activate
expense-tracker import-ledger-csv --csv-file /root/All-Accounts_2.csv
```

This clears the current ledger, attachments, and queued SMS review items before importing the CSV directly into SQLite.

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

## Web/API overview

Main pages:

- `/` dashboard
- `/transactions` ledger list
- `/transactions/new` add transaction
- `/reviews` SMS review queue
- `/manage` metadata management

Key API endpoints:

- `GET /api/transactions`
- `POST /api/transactions`
- `PUT /api/transactions/{transaction_id}`
- `DELETE /api/transactions/{transaction_id}`
- `GET /api/reviews`
- `POST /api/sms/intake`

Example SMS intake request:

```bash
curl -X POST http://127.0.0.1:8000/api/sms/intake \
  -H 'Content-Type: application/json' \
  -d '{
    "sender": "JM-HDFCBK-S",
    "phone": "JM-HDFCBK-S",
    "content": "Sent Rs.88.00 From HDFC Bank A/C *2054 To Amazon On 19/03/26 Ref 12345",
    "received_at": "2026-03-19T15:50:00Z"
  }'
```

## OpenAI draft generation

The SMS intake workflow checks `/root/openai.key` for an API key and uses `gpt-5-mini` with structured output for draft generation.

If the OpenAI call fails, the app records an explicit heuristic fallback draft instead of silently dropping the request.

## Repository notes

- Runtime data stays under `data/` and is gitignored.
- The project is web-first; the older TUI has been removed.
- Review drafts are stored in SQLite alongside the main ledger data.
