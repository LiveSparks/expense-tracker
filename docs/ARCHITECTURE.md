# Architecture

## Overview

The application is a web-first personal finance tracker built as a layered Python app:

- `expense_tracker/config.py` centralizes runtime configuration and secret loading
- `expense_tracker/tracker.py` owns ledger business rules
- `expense_tracker/storage.py` persists the ledger into SQLite
- `expense_tracker/web.py` exposes server-rendered pages and JSON APIs through FastAPI
- `expense_tracker/review_workflow.py` manages SMS review items and LLM-assisted drafting
- `expense_tracker/sms_history.py` stores historical SMS evidence linked to transactions
- `expense_tracker/sms_pipeline.py` handles regex extraction, legacy analysis, and structured output schema/model definitions

## Runtime layers

### Domain layer

`ExpenseTracker` is the main domain service. It handles:

- creating, editing, deleting, and filtering transactions
- transfer mirroring between owned accounts
- metadata CRUD for accounts, payees, categories, and subcategories
- attachment registration
- bulk ledger actions

The domain layer is the source of truth for ledger behavior. The web layer and CLI both call into it rather than bypassing business rules.

### Persistence layer

The app uses SQLite with the default database at `data/ledger.db`.

Key persisted concerns include:

- ledger metadata and transactions
- file attachment references
- SMS review queue items
- historical SMS rows, extracted markers, and transaction match links

`expense_tracker/sqlite_utils.py` centralizes connection setup, while `expense_tracker/storage.py` handles full ledger writes and migration behavior.

### Web layer

`expense_tracker/web.py` mounts:

- dashboard and ledger pages
- token-backed login/logout pages
- review queue and review detail pages
- metadata management pages
- JSON APIs for transactions, reviews, and SMS intake

The UI is server-rendered with Jinja templates and small JavaScript enhancements in `expense_tracker/static/app.js`.

### Configuration and auth layer

`expense_tracker/config.py` resolves production settings from environment variables and optional secret-file paths.

Current production-facing config covers:

- auth token loading for web/API protection
- OpenAI API key loading
- secure-cookie behavior
- allowed host validation

If `EXPENSE_TRACKER_AUTH_TOKEN` or `EXPENSE_TRACKER_AUTH_TOKEN_FILE` is configured:

- browser users authenticate through `/auth/login`
- the UI uses an `HttpOnly` session cookie that stores the bearer token for the current browser session
- API clients can use `Authorization: Bearer ...`
- `/healthz` remains unauthenticated for health checks

### SMS review layer

The SMS workflow has two persisted stores:

- `sms_messages` in `sms_history.py` for durable historical evidence and transaction links
- `sms_reviews` in `review_workflow.py` for queued, filtered, processing, ready, and approved review items

The intake flow is:

1. `/api/sms/intake` stores a review item immediately.
2. Regex-first filtering decides whether the message is obviously non-transactional.
3. Queued items are processed in the background.
4. The review workflow gathers extracted markers, similar history, and metadata lists.
5. OpenAI or heuristic fallback produces a draft.
6. The reviewer approves, edits, or deletes the draft.

### LLM prompting layer

The review prompt lives at `expense_tracker/prompts/review_draft_prompt.txt`.

The structured output contract is defined by `ReviewDraftTextFormat` in `expense_tracker/sms_pipeline.py`.

Current request shape:

- system prompt: plain-text instructions from the prompt file
- user payload: JSON containing the SMS, extracted markers, similar history, allowed accounts/payees/categories, and transfer rules
- response parsing: OpenAI Python SDK `responses.parse(..., text_format=ReviewDraftTextFormat)`

Equivalent JSON schema can still be generated through `build_structured_output_schema()` for docs, analysis artifacts, and prompt inspection.

## Important data flows

### Authentication

1. A request hits `expense_tracker/web.py`.
2. Auth middleware skips static assets, `/auth/login`, `/auth/logout`, and `/healthz`.
3. For protected routes, the app checks either:
   - `Authorization: Bearer <token>`
   - the auth cookie set by the login form
4. If the token is missing or invalid:
   - browser requests redirect to `/auth/login`
   - API requests receive `401 Unauthorized`

### Transaction creation

1. Web form or API submits account, payee, category, amount, date, and notes.
2. `ExpenseTracker` validates and normalizes the request.
3. Transfers create both source and destination ledger entries.
4. The storage layer writes the updated ledger state into SQLite.

### Historical SMS retrieval

1. Incoming SMS markers are extracted from regex-first parsing.
2. `SmsHistoryStore.similar_examples(...)` retrieves prior transaction-linked SMS evidence.
3. Transfer histories are normalized to a single logical source-side example.
4. Similar examples are fed into the draft generation path and shown in the review UI.

### Review approval

1. A review draft is displayed with markers, reasoning, and similar history.
2. Approval converts the review into a real transaction through `ExpenseTracker`.
3. The review item is marked approved.
4. Approved review evidence can be persisted into historical SMS storage for future retrieval.

## Frontend behavior

The client-side JavaScript is intentionally light. It currently handles:

- custom pickers
- category creation dialog
- transaction selection mode and bulk actions
- inline rename flows on management pages
- confirmation prompts
- list scroll restoration for transaction and review flows

## Operational notes

- The live server is normally started through `expense-tracker serve`.
- For LXC production, the recommended shape is a local `systemd` service bound to loopback, with TLS and reverse proxying handled externally.
- Prompt preview for a review is available at `/reviews/{review_id}/llm-request` and `/api/reviews/{review_id}/llm-request`.
- Database backups are local filesystem copies and are intentionally kept outside source control.
