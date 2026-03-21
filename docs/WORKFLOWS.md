# Workflows

## Development setup

Create the virtual environment and install the package:

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e .
```

Run the test suite:

```bash
. .venv/bin/activate
python -m unittest discover -s tests -v
```

## Running the server

Start the app:

```bash
. .venv/bin/activate
expense-tracker serve --host 0.0.0.0 --port 8000
```

Restart the live server in a shared shell environment:

```bash
ss -ltnp | grep ':8000'
kill <PID>
. .venv/bin/activate
nohup expense-tracker serve --host 0.0.0.0 --port 8000 >/tmp/expense-tracker.log 2>&1 &
```

Check the live log:

```bash
tail -f /tmp/expense-tracker.log
```

## LXC production bootstrap

Inside the LXC guest, run:

```bash
sudo ./scripts/install_lxc.sh
```

That script installs the app into the repo-local virtualenv, prepares `/var/lib/expense-tracker`, writes `/etc/expense-tracker/expense-tracker.env`, generates `/etc/expense-tracker/auth.token` if needed, and installs a `systemd` service.

Afterward:

1. Edit `/etc/expense-tracker/expense-tracker.env`
2. Set `EXPENSE_TRACKER_ALLOWED_HOSTS` to the real hostnames you will proxy
3. Optionally place the OpenAI key at `/etc/expense-tracker/openai.key`
4. Restart the service

```bash
sudo systemctl restart expense-tracker
sudo systemctl status expense-tracker
```

## Production configuration and auth

Recommended production env file keys:

```bash
EXPENSE_TRACKER_AUTH_TOKEN_FILE=/etc/expense-tracker/auth.token
EXPENSE_TRACKER_OPENAI_API_KEY_FILE=/etc/expense-tracker/openai.key
EXPENSE_TRACKER_SECURE_COOKIES=true
EXPENSE_TRACKER_ALLOWED_HOSTS=tracker.example.com
```

Generate or rotate the auth token:

```bash
. .venv/bin/activate
expense-tracker generate-auth-token --write-file /etc/expense-tracker/auth.token
sudo systemctl restart expense-tracker
```

Browser users authenticate via `/auth/login`. API clients and automation clients should send:

```bash
Authorization: Bearer <token>
```

Health checks stay open at:

```bash
curl http://127.0.0.1:8000/healthz
```

## Database backup and restore

Create a timestamped backup:

```bash
mkdir -p backups
cp data/ledger.db "backups/ledger-$(date -u +%Y%m%dT%H%M%SZ).db"
```

Restore a backup while the server is stopped:

```bash
cp backups/ledger-YYYYMMDDTHHMMSSZ.db data/ledger.db
```

## Ledger workflows

### Import a legacy ledger CSV

```bash
. .venv/bin/activate
expense-tracker import-ledger-csv --csv-file /root/All-Accounts_2.csv
```

This replaces the current ledger state in SQLite.

### Seed demo data

```bash
. .venv/bin/activate
expense-tracker seed-demo-data
```

## Historical SMS workflows

### Analyze legacy SMS and transaction CSVs

```bash
. .venv/bin/activate
expense-tracker analyze-sms \
  --transactions-csv /root/All-Accounts_2.csv \
  --sms-csv /root/all_sms.csv
```

This writes analysis artifacts under `data/sms_pipeline/`.

### Import historical SMS into SQLite

```bash
. .venv/bin/activate
expense-tracker import-sms-history --sms-csv /root/All_Conversations_2026-03-20.csv
```

This stores raw SMS rows, extracted markers, match reasoning, and matched transaction links for future retrieval.

## Live SMS intake and review workflow

### Send a single SMS into the queue

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

### Inspect queue state

```bash
curl -H 'Authorization: Bearer YOUR_TOKEN' http://127.0.0.1:8000/api/reviews
```

### Inspect the exact LLM request for a review

Open in the browser:

- `/reviews/{review_id}/llm-request`

Or fetch the JSON payload:

```bash
curl -H 'Authorization: Bearer YOUR_TOKEN' http://127.0.0.1:8000/api/reviews/{review_id}/llm-request
```

The preview includes:

- the system prompt text
- the exact JSON user payload
- the typed output model name
- the equivalent JSON schema

### Review queue states

- `queued`: accepted by the API and waiting for drafting
- `processing`: background worker is building the draft
- `for_review`: ready for human review
- `filtered`: rejected as non-transactional
- `approved`: converted into a real ledger transaction

## Git checkpoint workflow

1. Create a local database backup.
2. Review the worktree and remove stale/generated artifacts only.
3. Update docs and rerun tests.
4. Commit with the required co-author trailer:

```text
Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>
```
