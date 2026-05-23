from __future__ import annotations

import base64
import csv
import hashlib
import io
import os
import secrets
import shutil
import threading
import tempfile
from contextlib import contextmanager
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Iterator
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from fastapi import Body, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .auth import UserStore
from .backup_scheduler import BackupScheduler
from .cli import default_data_file
from .config import AppConfig, load_app_config
from .formatting import format_display_date, format_display_datetime, format_inr, format_list_date
from .migrations import apply_migrations
from .review_workflow import ReviewWorkflowStore
from .sms_history import SmsHistoryStore
from .tracker import ExpenseTracker

BASE_DIR = Path(__file__).resolve().parent
TEMPLATES = Jinja2Templates(directory=str(BASE_DIR / "templates"))
TEMPLATES.env.filters["currency"] = format_inr
TEMPLATES.env.filters["display_date"] = format_display_date
TEMPLATES.env.filters["display_datetime"] = format_display_datetime
TEMPLATES.env.filters["list_date"] = format_list_date

def _static_fingerprint(static_dir: Path) -> str:
    """Hash the mtimes of all static files so the version changes when any file changes."""
    h = hashlib.sha1()
    for entry in sorted(os.scandir(static_dir), key=lambda e: e.name):
        h.update(f"{entry.name}:{entry.stat().st_mtime_ns}".encode())
    return h.hexdigest()[:10]

STATIC_VER = _static_fingerprint(BASE_DIR / "static")
TEMPLATES.env.globals["static_ver"] = STATIC_VER

def category_options_from_tracker(tracker: ExpenseTracker) -> list[str]:
    metadata = tracker.metadata_snapshot()
    options: list[str] = []
    for category in metadata.categories:
        for subcategory in metadata.subcategories_by_category.get(category, []):
            options.append(f"{category} / {subcategory}")
    return options


def category_picker_options_from_tracker(tracker: ExpenseTracker) -> list[dict[str, object]]:
    metadata = tracker.metadata_snapshot()
    options: list[dict[str, object]] = []
    for category in metadata.categories:
        options.append({"label": category, "value": category, "kind": "category", "selectable": False})
        for subcategory in metadata.subcategories_by_category.get(category, []):
            options.append(
                {
                    "label": subcategory,
                    "value": f"{category} / {subcategory}",
                    "kind": "subcategory",
                    "group": category,
                    "selectable": True,
                }
            )
    return options


def picker_options(values: list[str]) -> list[dict[str, str]]:
    return [{"label": value, "value": value, "kind": "option"} for value in values]


def payee_picker_options_from_tracker(tracker: ExpenseTracker) -> list[dict[str, str]]:
    options: list[dict[str, str]] = []
    transfer_options: list[dict[str, str]] = []
    for payee in tracker.payee_summaries():
        option = {"label": str(payee["name"]), "value": str(payee["name"]), "kind": "option"}
        if payee.get("linked_account_name") is None:
            options.append(option)
        else:
            transfer_options.append({**option, "group": "Transfer to"})
    return options + transfer_options


def split_category_value(value: str) -> tuple[str, str | None]:
    if " / " not in value:
        return value.strip(), None
    category_name, subcategory_name = value.split(" / ", maxsplit=1)
    return category_name.strip(), subcategory_name.strip() or None


def require_transaction_subcategory(category_value: str) -> tuple[str, str]:
    category_name, subcategory_name = split_category_value(category_value)
    if not category_name or not subcategory_name:
        raise ValueError("Choose a subcategory. Category group labels are not valid transaction values.")
    return category_name, subcategory_name


def group_transactions(transactions) -> list[dict[str, object]]:
    grouped: dict[str, dict[str, object]] = {}
    for transaction in transactions:
        key = transaction.spent_on.isoformat()
        if key not in grouped:
            grouped[key] = {"date": key, "date_label": format_list_date(transaction.spent_on), "transactions": []}
        grouped[key]["transactions"].append(transaction)
    return list(grouped.values())


def add_error_query(url: str, message: str) -> str:
    split = urlsplit(url or "/transactions")
    query = [(key, value) for key, value in parse_qsl(split.query, keep_blank_values=True) if key != "error"]
    query.append(("error", message))
    return urlunsplit((split.scheme, split.netloc, split.path, urlencode(query), split.fragment))


def request_relative_url(request: Request) -> str:
    current_url = request.url.path
    if request.url.query:
        current_url = f"{current_url}?{request.url.query}"
    return current_url


def normalize_return_to(return_to: str | None, default: str) -> str:
    if not return_to:
        return default
    split = urlsplit(return_to)
    if split.scheme or split.netloc:
        return default
    path = split.path or default
    if not path.startswith("/"):
        return default
    return urlunsplit(("", "", path, split.query, split.fragment))


def parse_checkbox(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def encode_cookie_value(value: str) -> str:
    encoded = base64.urlsafe_b64encode(value.encode("utf-8")).decode("ascii")
    return f"b64:{encoded}"


def decode_cookie_value(value: str | None) -> str | None:
    raw_value = (value or "").strip()
    if not raw_value:
        return None
    if not raw_value.startswith("b64:"):
        return raw_value
    try:
        return base64.urlsafe_b64decode(raw_value[4:].encode("ascii")).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None


def normalize_csv_header(value: str) -> str:
    return " ".join((value or "").strip().lower().replace("_", " ").split())


def pick_csv_column(fieldnames: list[str], candidates: set[str]) -> str | None:
    for fieldname in fieldnames:
        if normalize_csv_header(fieldname) in candidates:
            return fieldname
    return None


def parse_reconcile_date(raw_value: str) -> date:
    value = raw_value.strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%m/%d/%Y", "%d/%m/%y", "%d-%m-%y"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    return date.fromisoformat(value)


def parse_reconcile_amount(raw_value: str) -> Decimal:
    normalized = raw_value.strip().replace(",", "").replace("₹", "")
    if normalized.startswith("(") and normalized.endswith(")"):
        normalized = f"-{normalized[1:-1]}"
    return Decimal(normalized)


def reconcile_statement_csv(current_tracker: ExpenseTracker, csv_bytes: bytes) -> dict[str, list[dict[str, object]]]:
    text = csv_bytes.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    fieldnames = list(reader.fieldnames or [])
    if not fieldnames:
        raise ValueError("CSV file must include a header row.")
    date_column = pick_csv_column(fieldnames, {"date", "transaction date", "posted date", "value date"})
    amount_column = pick_csv_column(fieldnames, {"amount", "transaction amount"})
    description_column = pick_csv_column(fieldnames, {"description", "narration", "particulars", "memo", "details"})
    if date_column is None or amount_column is None:
        raise ValueError("CSV file must include Date and Amount columns.")

    indexed_transactions: dict[tuple[str, str], list] = {}
    for transaction in current_tracker.list_transactions():
        key = (transaction.spent_on.isoformat(), f"{transaction.amount.quantize(Decimal('0.01')):.2f}")
        indexed_transactions.setdefault(key, []).append(transaction)

    matched: list[dict[str, object]] = []
    ambiguous: list[dict[str, object]] = []
    unmatched: list[dict[str, object]] = []
    for row in reader:
        raw_date = str(row.get(date_column, "") or "").strip()
        raw_amount = str(row.get(amount_column, "") or "").strip()
        if not raw_date or not raw_amount:
            continue
        try:
            parsed_date = parse_reconcile_date(raw_date)
            parsed_amount = parse_reconcile_amount(raw_amount).quantize(Decimal("0.01"))
        except (InvalidOperation, ValueError):
            continue
        csv_row = {
            "date": parsed_date.isoformat(),
            "amount": f"{parsed_amount:.2f}",
            "description": str(row.get(description_column, "") or "").strip() if description_column else "",
        }
        candidates = indexed_transactions.get((csv_row["date"], csv_row["amount"]), [])
        if len(candidates) == 1:
            matched.append({"csv": csv_row, "transaction": serialize_transaction(candidates[0])})
        elif len(candidates) > 1:
            ambiguous.append({"csv": csv_row, "matches": [serialize_transaction(item) for item in candidates]})
        else:
            unmatched.append(csv_row)
    return {"matched": matched, "ambiguous": ambiguous, "unmatched": unmatched}


@contextmanager
def saved_uploads(files: list[UploadFile]) -> Iterator[list[str]]:
    temp_paths: list[str] = []
    try:
        for upload in files:
            if not upload.filename:
                continue
            suffix = Path(upload.filename).suffix
            with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as handle:
                shutil.copyfileobj(upload.file, handle)
                temp_paths.append(handle.name)
        yield temp_paths
    finally:
        for path in temp_paths:
            temp_file = Path(path)
            if temp_file.exists():
                temp_file.unlink()
        for upload in files:
            upload.file.close()


def serialize_transaction(record) -> dict[str, object]:
    return {
        "id": record.id,
        "entry_type": record.entry_type,
        "account_name": record.account_name,
        "payee_name": record.payee_name,
        "category_name": record.category_name,
        "subcategory_name": record.subcategory_name,
        "amount": f"{record.amount:.2f}",
        "notes": record.notes,
        "spent_on": record.spent_on.isoformat(),
        "linked_transaction_id": record.linked_transaction_id,
        "transfer_group_id": record.transfer_group_id,
        "verified": bool(record.verified),
        "attachments": [
            {"id": item.id, "name": item.original_name, "path": item.stored_path}
            for item in record.attachments
        ],
    }


def transaction_form_context(
    current_tracker: ExpenseTracker,
    *,
    mode: str,
    transaction,
    selected_account: str,
    selected_payee: str,
    selected_category: str,
    selected_amount: str,
    error: str | None,
    return_to: str | None = None,
    related_sms_messages: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    metadata = current_tracker.metadata_snapshot()
    payee_picker_options = payee_picker_options_from_tracker(current_tracker)
    non_transfer_payees = [
        payee["name"]
        for payee in current_tracker.payee_summaries()
        if payee.get("linked_account_name") is None
    ]
    return {
        "mode": mode,
        "transaction": transaction,
        "accounts": metadata.accounts,
        "payees": metadata.payees,
        "category_options": category_options_from_tracker(current_tracker),
        "account_picker_options": picker_options(metadata.accounts),
        "payee_picker_options": payee_picker_options,
        "category_picker_options": category_picker_options_from_tracker(current_tracker),
        "category_group_options": picker_options(metadata.categories),
        "bulk_payee_options": picker_options(non_transfer_payees),
        "selected_account": selected_account,
        "selected_payee": selected_payee,
        "selected_category": selected_category,
        "selected_amount": selected_amount,
        "selected_spent_on": transaction.spent_on.isoformat() if transaction else date.today().isoformat(),
        "error": error,
        "return_to": return_to or (f"/transactions?{urlencode({'account': selected_account})}" if selected_account else "/transactions"),
        "related_sms_messages": related_sms_messages or [],
    }


def review_form_context(
    current_tracker: ExpenseTracker,
    review,
    *,
    error: str | None,
    return_to: str,
    current_url: str,
) -> dict[str, object]:
    metadata = current_tracker.metadata_snapshot()
    payee_picker_options = payee_picker_options_from_tracker(current_tracker)
    return {
        "review": review,
        "accounts": metadata.accounts,
        "payees": metadata.payees,
        "category_options": category_options_from_tracker(current_tracker),
        "account_picker_options": picker_options(metadata.accounts),
        "payee_picker_options": payee_picker_options,
        "category_picker_options": category_picker_options_from_tracker(current_tracker),
        "selected_account": review.draft_account_name,
        "selected_payee": review.draft_payee_name,
        "selected_category": review.draft_category_value,
        "selected_amount": review.draft_amount,
        "selected_spent_on": review.draft_spent_on,
        "selected_notes": review.draft_notes,
        "error": error,
        "return_to": return_to,
        "current_url": current_url,
    }


def navigation_context(request: Request) -> dict[str, str]:
    account = request.query_params.get("account")
    add_transaction_href = "/transactions/new"
    if request.url.path == "/transactions" and account:
        add_transaction_href = f"/transactions/new?{urlencode({'account': account})}"
    return {"add_transaction_href": add_transaction_href}


def create_app(
    data_file: Path | None = None,
    *,
    process_reviews_inline: bool = False,
    config: AppConfig | None = None,
) -> FastAPI:
    app = FastAPI(title="Expense Tracker")
    app.state.data_file = data_file or default_data_file()
    app.state.config = config or load_app_config()
    tracker_instance = ExpenseTracker(app.state.data_file)
    tracker_instance.storage.load()
    apply_migrations(app.state.data_file)
    app.state.user_store = UserStore(app.state.data_file)
    app.state.backup_scheduler = BackupScheduler(app.state.data_file)
    app.state.backup_scheduler.start()
    if app.state.config.allowed_hosts != ("*",):
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(app.state.config.allowed_hosts))
    app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
    review_worker_lock = threading.Lock()
    review_worker: dict[str, threading.Thread | None] = {"thread": None}

    @app.on_event("shutdown")
    def stop_background_workers() -> None:
        app.state.backup_scheduler.stop()

    def tracker() -> ExpenseTracker:
        return ExpenseTracker(app.state.data_file)

    def review_store() -> ReviewWorkflowStore:
        return ReviewWorkflowStore(app.state.data_file, config=app.state.config)

    def user_store() -> UserStore:
        return app.state.user_store

    def backup_scheduler() -> BackupScheduler:
        return app.state.backup_scheduler

    def auth_mode() -> str:
        return app.state.config.auth_mode

    def auth_token() -> str | None:
        return app.state.config.resolved_auth_token

    def request_bearer_token(request: Request) -> str | None:
        header = request.headers.get("authorization", "").strip()
        if header.lower().startswith("bearer "):
            return header[7:].strip() or None
        return None

    def request_cookie_raw(request: Request) -> str | None:
        cookie_value = request.cookies.get(app.state.config.cookie_name, "").strip()
        return cookie_value or None

    def request_cookie_token(request: Request) -> str | None:
        return decode_cookie_value(request_cookie_raw(request))

    def request_token(request: Request) -> str | None:
        return request_bearer_token(request) or request_cookie_token(request)

    def current_user_id(request: Request) -> str | None:
        if auth_mode() != "password":
            return None
        bearer_token = request_bearer_token(request)
        if bearer_token:
            if bearer_token.startswith("et_"):
                return user_store().validate_api_key(bearer_token)
            return user_store().validate_session(bearer_token)
        cookie_token = request_cookie_token(request)
        if cookie_token:
            return user_store().validate_session(cookie_token)
        return None

    def token_cookie_value() -> str | None:
        expected = auth_token()
        if not expected:
            return None
        return hashlib.sha256(expected.encode("utf-8")).hexdigest()

    def is_authorized(request: Request) -> bool:
        if auth_mode() == "password":
            return current_user_id(request) is not None
        expected = auth_token()
        if not expected:
            return True
        bearer_token = request_bearer_token(request)
        if bearer_token and secrets.compare_digest(bearer_token, expected):
            return True
        cookie_value = request_cookie_raw(request)
        expected_cookie = token_cookie_value()
        return bool(cookie_value and expected_cookie) and secrets.compare_digest(cookie_value, expected_cookie)

    def is_exempt_path(path: str) -> bool:
        return path.startswith("/static/") or path in {"/auth/login", "/auth/logout", "/auth/setup", "/healthz"}

    def set_session_cookie(response: RedirectResponse, session_token: str) -> None:
        response.set_cookie(
            app.state.config.cookie_name,
            encode_cookie_value(session_token),
            httponly=True,
            samesite="lax",
            secure=app.state.config.secure_cookies,
            max_age=app.state.config.cookie_max_age_seconds,
        )

    def set_static_auth_cookie(response: RedirectResponse) -> None:
        cookie_value = token_cookie_value()
        if cookie_value is None:
            return
        response.set_cookie(
            app.state.config.cookie_name,
            cookie_value,
            httponly=True,
            samesite="lax",
            secure=app.state.config.secure_cookies,
            max_age=app.state.config.cookie_max_age_seconds,
        )

    @app.middleware("http")
    async def require_authentication(request: Request, call_next):
        if not app.state.config.auth_enabled or is_exempt_path(request.url.path) or is_authorized(request):
            return await call_next(request)
        if request.url.path.startswith("/api/"):
            return JSONResponse(
                {"detail": "Unauthorized"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
        login_target = f"/auth/login?{urlencode({'next': request_relative_url(request)})}"
        return RedirectResponse(login_target, status_code=303)

    @app.middleware("http")
    async def set_security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        content_type = response.headers.get("content-type", "")
        if "text/html" in content_type:
            response.headers.setdefault("Cache-Control", "no-cache")
        return response

    def start_review_worker() -> None:
        if process_reviews_inline:
            review_store().process_pending_queue()
            return
        worker = review_worker.get("thread")
        if worker is not None and worker.is_alive():
            return

        def run_worker() -> None:
            with review_worker_lock:
                while review_store().process_pending_queue(max_items=1):
                    continue

        thread = threading.Thread(target=run_worker, name="sms-review-worker", daemon=True)
        review_worker["thread"] = thread
        thread.start()

    def transaction_context(request: Request, current_tracker: ExpenseTracker, error: str | None = None, **filters: str | None) -> dict[str, object]:
        metadata = current_tracker.metadata_snapshot()
        transactions = current_tracker.list_transactions(**filters)
        current_url = request_relative_url(request)
        non_transfer_payees = [
            payee["name"]
            for payee in current_tracker.payee_summaries()
            if payee.get("linked_account_name") is None
        ]
        return {
            "filters": filters,
            "transactions": transactions,
            "grouped_transactions": group_transactions(transactions),
            "total_amount": current_tracker.total_amount(transactions),
            "accounts": metadata.accounts,
            "payees": metadata.payees,
            "category_options": category_options_from_tracker(current_tracker),
            "filter_account_options": picker_options(metadata.accounts),
            "filter_payee_options": picker_options(metadata.payees),
            "filter_category_options": picker_options(metadata.categories),
            "bulk_move_account_options": picker_options(metadata.accounts),
            "bulk_payee_options": picker_options(non_transfer_payees),
            "error": error,
            "current_url": current_url,
            "has_active_filters": any(value for key, value in filters.items() if key not in {"account", "search"}) or request.query_params.get("show_filters") == "1",
        }

    @app.get("/healthz")
    def healthz():
        return {"status": "ok"}

    @app.get("/auth/login")
    def auth_login_form(request: Request, next: str | None = None, error: str | None = None):
        if not app.state.config.auth_enabled:
            return RedirectResponse("/", status_code=303)
        normalized_next = normalize_return_to(next, "/")
        if auth_mode() == "password" and user_store().user_count() == 0:
            return RedirectResponse("/auth/setup", status_code=303)
        if is_authorized(request):
            return RedirectResponse(normalized_next, status_code=303)
        return TEMPLATES.TemplateResponse(
            request,
            "login.html",
            {
                "next": normalized_next,
                "error": error,
                "auth_mode": auth_mode(),
            },
            status_code=401 if error else 200,
        )

    @app.post("/auth/login")
    def auth_login(
        request: Request,
        token: str = Form(""),
        username: str = Form(""),
        password: str = Form(""),
        next: str = Form("/"),
    ):
        if not app.state.config.auth_enabled:
            return RedirectResponse("/", status_code=303)
        normalized_next = normalize_return_to(next, "/")
        if auth_mode() == "password":
            if user_store().user_count() == 0:
                return RedirectResponse("/auth/setup", status_code=303)
            user_id = user_store().authenticate(username, password)
            if user_id is None:
                return TEMPLATES.TemplateResponse(
                    request,
                    "login.html",
                    {
                        "next": normalized_next,
                        "error": "Invalid username or password.",
                        "auth_mode": auth_mode(),
                    },
                    status_code=401,
                )
            response = RedirectResponse(normalized_next, status_code=303)
            set_session_cookie(response, user_store().create_session(user_id))
            return response
        expected = auth_token() or ""
        if not token or not secrets.compare_digest(token, expected):
            return TEMPLATES.TemplateResponse(
                request,
                "login.html",
                {
                    "next": normalized_next,
                    "error": "Invalid token.",
                    "auth_mode": auth_mode(),
                },
                status_code=401,
            )
        response = RedirectResponse(normalized_next, status_code=303)
        set_static_auth_cookie(response)
        return response

    @app.get("/auth/setup")
    def auth_setup_form(request: Request, error: str | None = None):
        if auth_mode() != "password":
            return RedirectResponse("/auth/login", status_code=303)
        if user_store().user_count() > 0:
            return RedirectResponse("/auth/login", status_code=303)
        return TEMPLATES.TemplateResponse(
            request,
            "setup.html",
            {"error": error},
            status_code=400 if error else 200,
        )

    @app.post("/auth/setup")
    def auth_setup(
        request: Request,
        username: str = Form(""),
        password: str = Form(""),
        confirm_password: str = Form(""),
    ):
        if auth_mode() != "password":
            return RedirectResponse("/auth/login", status_code=303)
        if user_store().user_count() > 0:
            return RedirectResponse("/auth/login", status_code=303)
        if not username.strip() or not password:
            error = "Username and password are required."
        elif password != confirm_password:
            error = "Passwords do not match."
        else:
            error = None
        if error:
            return TEMPLATES.TemplateResponse(request, "setup.html", {"error": error}, status_code=400)
        user_id = user_store().create_user(username, password)
        response = RedirectResponse("/", status_code=303)
        set_session_cookie(response, user_store().create_session(user_id))
        return response

    @app.post("/auth/logout")
    def auth_logout(request: Request):
        if auth_mode() == "password":
            user_store().delete_session(request_cookie_token(request) or "")
        response = RedirectResponse("/auth/login", status_code=303)
        response.delete_cookie(app.state.config.cookie_name)
        return response

    @app.get("/")
    def dashboard(request: Request):
        current_tracker = tracker()
        balances = current_tracker.account_balances()
        return TEMPLATES.TemplateResponse(
            request,
            "dashboard.html",
            {
                "accounts": current_tracker.account_summaries(),
                "balances": balances,
                "total_balance": sum(balances.values()),
                "pending_reviews": review_store().pending_count(),
                **navigation_context(request),
            },
        )

    @app.get("/reviews")
    def review_list(request: Request, error: str | None = None):
        current_review_store = review_store()
        return TEMPLATES.TemplateResponse(
            request,
            "reviews.html",
            {
                "reviews": current_review_store.list_reviews(),
                "queue_counts": current_review_store.queue_counts(),
                "error": error,
                "current_url": request_relative_url(request),
                **navigation_context(request),
            },
        )

    @app.get("/reviews/{review_id}")
    def review_detail(request: Request, review_id: str, error: str | None = None, return_to: str | None = None):
        current_review = review_store().get_review(review_id)
        if current_review is None:
            raise HTTPException(status_code=404, detail="Review item not found")
        current_url = request_relative_url(request)
        normalized_return_to = normalize_return_to(return_to, "/reviews")
        return TEMPLATES.TemplateResponse(
            request,
            "review_detail.html",
            review_form_context(
                tracker(),
                current_review,
                error=error,
                return_to=normalized_return_to,
                current_url=current_url,
            )
            | navigation_context(request),
        )

    @app.post("/reviews/{review_id}/approve")
    async def approve_review(
        request: Request,
        review_id: str,
        account_name: str = Form(...),
        payee_name: str = Form(...),
        category_value: str = Form(...),
        amount: str = Form(...),
        spent_on: str = Form(...),
        notes: str = Form(""),
        return_to: str = Form("/reviews"),
    ):
        current_tracker = tracker()
        current_review_store = review_store()
        current_review = current_review_store.get_review(review_id)
        if current_review is None:
            raise HTTPException(status_code=404, detail="Review item not found")
        normalized_return_to = normalize_return_to(return_to, "/reviews")
        try:
            current_review_store.approve_review(
                review_id,
                account_name=account_name,
                payee_name=payee_name,
                category_value=category_value,
                amount=amount,
                spent_on=spent_on,
                notes=notes,
            )
        except ValueError as exc:
            current_review.draft_account_name = account_name
            current_review.draft_payee_name = payee_name
            current_review.draft_category_value = category_value
            current_review.draft_amount = amount
            current_review.draft_spent_on = spent_on
            current_review.draft_notes = notes
            return TEMPLATES.TemplateResponse(
                request,
                "review_detail.html",
                review_form_context(
                    current_tracker,
                    current_review,
                    error=str(exc),
                    return_to=normalized_return_to,
                    current_url=request_relative_url(request),
                )
                | navigation_context(request),
                status_code=400,
            )
        return RedirectResponse(normalized_return_to, status_code=303)

    @app.post("/reviews/{review_id}/quick-approve")
    def quick_approve_review(review_id: str, return_to: str = Form("/reviews")):
        normalized_return_to = normalize_return_to(return_to, "/reviews")
        try:
            review_store().quick_approve_review(review_id)
        except ValueError as exc:
            return RedirectResponse(add_error_query(normalized_return_to, str(exc)), status_code=303)
        return RedirectResponse(normalized_return_to, status_code=303)

    @app.post("/reviews/{review_id}/delete")
    def delete_review(review_id: str, return_to: str = Form("/reviews")):
        current_review = review_store().get_review(review_id)
        if current_review is None:
            raise HTTPException(status_code=404, detail="Review item not found")
        review_store().delete_review(review_id)
        return RedirectResponse(normalize_return_to(return_to, "/reviews"), status_code=303)

    @app.get("/logs")
    def logs_page(
        request: Request,
        event_type: str | None = None,
        page: int = 1,
    ):
        per_page = 50
        offset = (max(page, 1) - 1) * per_page
        current_review_store = review_store()
        entries = current_review_store.list_log_entries(
            event_type=event_type or None,
            limit=per_page,
            offset=offset,
        )
        total = current_review_store.log_entry_count(event_type=event_type or None)
        total_pages = max(1, (total + per_page - 1) // per_page)
        event_types = ["intake", "filtered", "processed", "approved", "deleted", "error"]
        return TEMPLATES.TemplateResponse(
            request,
            "logs.html",
            {
                "entries": entries,
                "event_type": event_type or "",
                "event_types": event_types,
                "page": page,
                "total_pages": total_pages,
                "total": total,
                "current_url": request_relative_url(request),
                **navigation_context(request),
            },
        )

    @app.get("/reviews/{review_id}/llm-request")
    def review_llm_request(request: Request, review_id: str, return_to: str | None = None):
        current_review = review_store().get_review(review_id)
        if current_review is None:
            raise HTTPException(status_code=404, detail="Review item not found")
        prompt_preview = review_store().llm_request_preview(
            review_id,
            metadata=tracker().metadata_snapshot(),
        )
        return TEMPLATES.TemplateResponse(
            request,
            "review_llm_request.html",
            {
                "review": current_review,
                "prompt_preview": prompt_preview,
                "return_to": normalize_return_to(return_to, f"/reviews/{review_id}"),
                **navigation_context(request),
            },
        )

    @app.get("/transactions")
    def transaction_list(
        request: Request,
        account: str | None = None,
        category: str | None = None,
        payee: str | None = None,
        search: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        error: str | None = None,
    ):
        current_tracker = tracker()
        return TEMPLATES.TemplateResponse(
            request,
            "transactions.html",
            transaction_context(
                request,
                current_tracker,
                error=error,
                account=account,
                category=category,
                payee=payee,
                search=search,
                start_date=start_date,
                end_date=end_date,
            )
            | navigation_context(request),
        )

    @app.get("/transactions/new")
    def new_transaction_form(request: Request, account: str | None = None, return_to: str | None = None):
        current_tracker = tracker()
        return TEMPLATES.TemplateResponse(
            request,
            "transaction_form.html",
            transaction_form_context(
                current_tracker,
                mode="create",
                transaction=None,
                selected_account=account or "",
                selected_payee="",
                selected_category="",
                selected_amount="-",
                error=None,
                return_to=normalize_return_to(return_to, f"/transactions?{urlencode({'account': account})}" if account else "/transactions"),
            )
            | navigation_context(request),
        )

    @app.post("/transactions")
    async def create_transaction(
        request: Request,
        account_name: str = Form(...),
        payee_name: str = Form(...),
        category_value: str = Form(...),
        amount: str = Form(...),
        spent_on: str = Form(...),
        notes: str = Form(""),
        return_to: str = Form(""),
        files: list[UploadFile] = File(default=[]),
    ):
        current_tracker = tracker()
        normalized_return_to = normalize_return_to(
            return_to,
            f"/transactions?{urlencode({'account': account_name})}" if account_name else "/transactions",
        )
        try:
            category_name, subcategory_name = require_transaction_subcategory(category_value)
            with saved_uploads(files) as paths:
                current_tracker.add_transaction(
                    account_name=account_name,
                    payee_name=payee_name,
                    category_name=category_name,
                    subcategory_name=subcategory_name,
                    amount=amount,
                    spent_on=spent_on,
                    notes=notes,
                    attachment_paths=paths,
                )
        except ValueError as exc:
            return TEMPLATES.TemplateResponse(
                request,
                "transaction_form.html",
                transaction_form_context(
                    current_tracker,
                    mode="create",
                    transaction=None,
                    selected_account=account_name,
                    selected_payee=payee_name,
                    selected_category=category_value,
                    selected_amount=amount,
                    error=str(exc),
                    return_to=normalized_return_to,
                )
                | navigation_context(request),
                status_code=400,
            )
        return RedirectResponse(normalized_return_to, status_code=303)

    @app.get("/transactions/{transaction_id}/edit")
    def edit_transaction_form(request: Request, transaction_id: str, return_to: str | None = None):
        current_tracker = tracker()
        transaction = current_tracker.get_transaction(transaction_id)
        if transaction is None:
            raise HTTPException(status_code=404, detail="Transaction not found")
        normalized_return_to = normalize_return_to(return_to, f"/transactions?{urlencode({'account': transaction.account_name})}")
        related_sms_messages = SmsHistoryStore(app.state.data_file).transaction_messages(
            transaction=transaction,
            transactions=current_tracker.list_transactions(),
        )
        selected_category = transaction.category_name
        if transaction.subcategory_name:
            selected_category = f"{selected_category} / {transaction.subcategory_name}"
        return TEMPLATES.TemplateResponse(
            request,
            "transaction_form.html",
            transaction_form_context(
                current_tracker,
                mode="edit",
                transaction=transaction,
                selected_account=transaction.account_name,
                selected_payee=transaction.payee_name,
                selected_category=selected_category,
                selected_amount=f"{transaction.amount:.2f}",
                error=None,
                return_to=normalized_return_to,
                related_sms_messages=related_sms_messages,
            )
            | navigation_context(request),
        )

    @app.post("/transactions/{transaction_id}/edit")
    async def edit_transaction(
        request: Request,
        transaction_id: str,
        account_name: str = Form(...),
        payee_name: str = Form(...),
        category_value: str = Form(...),
        amount: str = Form(...),
        spent_on: str = Form(...),
        notes: str = Form(""),
        return_to: str = Form("/transactions"),
        files: list[UploadFile] = File(default=[]),
    ):
        current_tracker = tracker()
        normalized_return_to = normalize_return_to(return_to, "/transactions")
        try:
            category_name, subcategory_name = require_transaction_subcategory(category_value)
            with saved_uploads(files) as paths:
                current_tracker.update_transaction(
                    transaction_id,
                    account_name=account_name,
                    payee_name=payee_name,
                    category_name=category_name,
                    subcategory_name=subcategory_name,
                    amount=amount,
                    spent_on=spent_on,
                    notes=notes,
                    attachment_paths=paths,
                )
        except ValueError as exc:
            transaction = current_tracker.get_transaction(transaction_id)
            return TEMPLATES.TemplateResponse(
                request,
                "transaction_form.html",
                transaction_form_context(
                    current_tracker,
                    mode="edit",
                    transaction=transaction,
                    selected_account=account_name,
                    selected_payee=payee_name,
                    selected_category=category_value,
                    selected_amount=amount,
                    error=str(exc),
                    return_to=normalized_return_to,
                )
                | navigation_context(request),
                status_code=400,
            )
        return RedirectResponse(normalized_return_to, status_code=303)

    @app.post("/transactions/{transaction_id}/verify")
    async def verify_transaction(
        request: Request,
        transaction_id: str,
        return_to: str = Form("/transactions"),
    ):
        try:
            verified = tracker().toggle_verified(transaction_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        accepts_json = "application/json" in request.headers.get("accept", "")
        if accepts_json:
            return {"verified": verified}
        return RedirectResponse(normalize_return_to(return_to, "/transactions"), status_code=303)

    @app.post("/transactions/bulk")
    async def bulk_transactions(
        action: str = Form(...),
        transaction_ids: list[str] = Form(default=[]),
        target_account: str = Form(""),
        target_payee: str = Form(""),
        target_date: str = Form(""),
        return_to: str = Form("/transactions"),
    ):
        current_tracker = tracker()
        try:
            if not transaction_ids:
                raise ValueError("Select at least one transaction.")
            if action == "delete":
                current_tracker.bulk_delete_transactions(transaction_ids)
            elif action == "duplicate":
                current_tracker.bulk_duplicate_transactions(transaction_ids)
            elif action == "move_account":
                current_tracker.bulk_move_transactions(transaction_ids, target_account)
            elif action == "change_payee":
                current_tracker.bulk_change_payee(transaction_ids, target_payee)
            elif action == "change_date":
                current_tracker.bulk_change_transaction_date(transaction_ids, target_date)
            else:
                raise ValueError("Unknown bulk action.")
        except ValueError as exc:
            return RedirectResponse(add_error_query(return_to, str(exc)), status_code=303)
        return RedirectResponse(return_to or "/transactions", status_code=303)

    @app.post("/transactions/{transaction_id}/delete")
    def delete_transaction(transaction_id: str, return_to: str = Form("/transactions")):
        tracker().delete_transaction(transaction_id)
        return RedirectResponse(normalize_return_to(return_to, "/transactions"), status_code=303)

    @app.get("/attachments/{transaction_id}/{attachment_name}")
    def attachment_file(transaction_id: str, attachment_name: str):
        current_tracker = tracker()
        transaction = current_tracker.get_transaction(transaction_id)
        if transaction is None:
            raise HTTPException(status_code=404, detail="Transaction not found")
        attachment = next((item for item in transaction.attachments if item.original_name == attachment_name), None)
        if attachment is None:
            raise HTTPException(status_code=404, detail="Attachment not found")
        return FileResponse(attachment.stored_path, filename=attachment.original_name)

    @app.get("/manage")
    def manage_index(request: Request):
        return TEMPLATES.TemplateResponse(request, "manage_index.html", navigation_context(request))

    @app.get("/manage/security")
    def manage_security(request: Request, error: str | None = None):
        created_api_key = request.cookies.get(f"{app.state.config.cookie_name}_new_api_key")
        response = TEMPLATES.TemplateResponse(
            request,
            "manage_security.html",
            {
                "available": auth_mode() == "password",
                "api_keys": user_store().list_api_keys(current_user_id(request) or "") if auth_mode() == "password" else [],
                "created_api_key": created_api_key,
                "error": error,
                **navigation_context(request),
            },
        )
        if created_api_key:
            response.delete_cookie(f"{app.state.config.cookie_name}_new_api_key")
        return response

    @app.post("/manage/security/api-keys")
    def manage_security_create_api_key(request: Request, name: str = Form("")):
        if auth_mode() != "password":
            return RedirectResponse("/manage/security", status_code=303)
        user_id = current_user_id(request)
        if user_id is None:
            raise HTTPException(status_code=401, detail="Unauthorized")
        try:
            raw_key, _ = user_store().create_api_key(user_id, name)
        except ValueError as exc:
            return RedirectResponse(f"/manage/security?error={exc}", status_code=303)
        response = RedirectResponse("/manage/security", status_code=303)
        response.set_cookie(
            f"{app.state.config.cookie_name}_new_api_key",
            raw_key,
            httponly=True,
            samesite="lax",
            secure=app.state.config.secure_cookies,
            max_age=120,
        )
        return response

    @app.post("/manage/security/api-keys/{key_id}/delete")
    def manage_security_delete_api_key(request: Request, key_id: str):
        if auth_mode() != "password":
            return RedirectResponse("/manage/security", status_code=303)
        user_id = current_user_id(request)
        if user_id is None:
            raise HTTPException(status_code=401, detail="Unauthorized")
        user_store().delete_api_key(key_id, user_id)
        return RedirectResponse("/manage/security", status_code=303)

    @app.get("/manage/backups")
    def manage_backups(request: Request, error: str | None = None, message: str | None = None):
        return TEMPLATES.TemplateResponse(
            request,
            "manage_backups.html",
            {
                "settings": backup_scheduler().get_settings(),
                "runs": backup_scheduler().list_runs(),
                "error": error,
                "message": message,
                **navigation_context(request),
            },
        )

    @app.post("/manage/backups/settings")
    def manage_backups_settings(
        enabled: str | None = Form(None),
        interval_hours: int = Form(24),
        retention_count: int = Form(7),
        backup_dir: str = Form(""),
    ):
        backup_scheduler().save_settings(
            enabled=parse_checkbox(enabled),
            interval_hours=max(1, int(interval_hours)),
            retention_count=max(0, int(retention_count)),
            backup_dir=backup_dir.strip(),
        )
        return RedirectResponse("/manage/backups?message=Backup+settings+saved.", status_code=303)

    @app.post("/manage/backups/run")
    def manage_backups_run():
        result = backup_scheduler().run_backup()
        if result["status"] == "success":
            return RedirectResponse("/manage/backups?message=Backup+completed+successfully.", status_code=303)
        return RedirectResponse(f"/manage/backups?error={result['error']}", status_code=303)

    @app.get("/manage/reconcile")
    def manage_reconcile(request: Request, error: str | None = None, message: str | None = None):
        return TEMPLATES.TemplateResponse(
            request,
            "manage_reconcile.html",
            {
                "results": None,
                "error": error,
                "message": message,
                **navigation_context(request),
            },
        )

    @app.post("/manage/reconcile")
    async def manage_reconcile_post(
        request: Request,
        action: str = Form("upload"),
        transaction_ids: list[str] = Form(default=[]),
        file: UploadFile | None = File(default=None),
    ):
        current_tracker = tracker()
        if action == "verify_matched":
            verified_count = 0
            for transaction_id in transaction_ids:
                try:
                    current_tracker.set_verified(transaction_id, True)
                    verified_count += 1
                except ValueError:
                    continue
            return RedirectResponse(f"/manage/reconcile?message=Verified+{verified_count}+transactions.", status_code=303)
        if file is None or not file.filename:
            return RedirectResponse("/manage/reconcile?error=Choose+a+CSV+file.", status_code=303)
        try:
            results = reconcile_statement_csv(current_tracker, await file.read())
        except ValueError as exc:
            return TEMPLATES.TemplateResponse(
                request,
                "manage_reconcile.html",
                {"results": None, "error": str(exc), "message": None, **navigation_context(request)},
                status_code=400,
            )
        finally:
            if file is not None:
                file.file.close()
        return TEMPLATES.TemplateResponse(
            request,
            "manage_reconcile.html",
            {
                "results": results,
                "error": None,
                "message": None,
                **navigation_context(request),
            },
        )

    @app.get("/manage/data")
    def manage_data(request: Request, error: str | None = None, message: str | None = None):
        return TEMPLATES.TemplateResponse(
            request,
            "manage_data.html",
            {
                "error": error,
                "message": message,
                **navigation_context(request),
            },
        )

    @app.get("/manage/data/export")
    def manage_data_export():
        # Make sure the database exists before exporting it.
        tracker().storage.load()
        database_path: Path = app.state.data_file
        filename = f"expense-tracker-{date.today().isoformat()}.db"
        return FileResponse(database_path, media_type="application/octet-stream", filename=filename)

    @app.post("/manage/data/import")
    async def manage_data_import(database_file: UploadFile = File(...)):
        uploaded_path: Path | None = None
        try:
            if not database_file.filename:
                return RedirectResponse("/manage/data?error=Choose+a+database+file.", status_code=303)

            with tempfile.NamedTemporaryFile(delete=False, suffix=".db") as handle:
                shutil.copyfileobj(database_file.file, handle)
                uploaded_path = Path(handle.name)

            header = uploaded_path.read_bytes()[:16]
            if not header.startswith(b"SQLite format 3\x00"):
                return RedirectResponse("/manage/data?error=Uploaded+file+is+not+a+valid+SQLite+database.", status_code=303)

            database_path: Path = app.state.data_file
            database_path.parent.mkdir(parents=True, exist_ok=True)
            if database_path.exists():
                timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
                backup_path = database_path.with_name(f"{database_path.name}.bak-{timestamp}")
                shutil.copy2(database_path, backup_path)
            shutil.move(str(uploaded_path), str(database_path))
            uploaded_path = None
            return RedirectResponse("/manage/data?message=Database+imported+successfully.", status_code=303)
        except OSError:
            return RedirectResponse("/manage/data?error=Unable+to+import+database+file.", status_code=303)
        finally:
            if uploaded_path and uploaded_path.exists():
                uploaded_path.unlink()
            database_file.file.close()

    @app.get("/manage/accounts")
    def manage_accounts(request: Request, error: str | None = None):
        current_tracker = tracker()
        return TEMPLATES.TemplateResponse(
            request,
            "manage_accounts.html",
            {
                "accounts": current_tracker.account_summaries(),
                "account_options": picker_options(current_tracker.metadata_snapshot().accounts),
                "error": error,
                **navigation_context(request),
            },
        )

    @app.post("/manage/accounts")
    async def manage_accounts_post(
        action: str = Form(...),
        name: str = Form(""),
        current_name: str = Form(""),
        new_name: str = Form(""),
        replacement_name: str = Form(""),
        delete_strategy: str = Form(""),
    ):
        current_tracker = tracker()
        try:
            if action == "create":
                current_tracker.create_account(name)
            elif action == "rename":
                current_tracker.rename_account(current_name, new_name)
            elif action == "delete":
                current_tracker.delete_account(
                    current_name,
                    replacement_account_name=replacement_name or None,
                    delete_transactions=delete_strategy == "delete_transactions",
                )
        except ValueError as exc:
            return RedirectResponse(f"/manage/accounts?error={exc}", status_code=303)
        return RedirectResponse("/manage/accounts", status_code=303)

    @app.get("/manage/payees")
    def manage_payees(request: Request, error: str | None = None):
        current_tracker = tracker()
        return TEMPLATES.TemplateResponse(
            request,
            "manage_payees.html",
            {
                "payees": current_tracker.payee_summaries(),
                "payee_options": picker_options(
                    [payee["name"] for payee in current_tracker.payee_summaries() if payee.get("linked_account_name") is None]
                ),
                "error": error,
                **navigation_context(request),
            },
        )

    @app.post("/manage/payees")
    async def manage_payees_post(
        action: str = Form(...),
        name: str = Form(""),
        current_name: str = Form(""),
        new_name: str = Form(""),
        replacement_name: str = Form(""),
        delete_strategy: str = Form(""),
    ):
        current_tracker = tracker()
        try:
            if action == "create":
                current_tracker.create_payee(name)
            elif action == "rename":
                current_tracker.rename_payee(current_name, new_name)
            elif action == "delete":
                current_tracker.delete_payee(
                    current_name,
                    replacement_payee_name=replacement_name or None,
                    delete_transactions=delete_strategy == "delete_transactions",
                )
        except ValueError as exc:
            return RedirectResponse(f"/manage/payees?error={exc}", status_code=303)
        return RedirectResponse("/manage/payees", status_code=303)

    @app.get("/manage/categories")
    def manage_categories(request: Request, error: str | None = None):
        current_tracker = tracker()
        return TEMPLATES.TemplateResponse(
            request,
            "manage_categories.html",
            {
                "categories": current_tracker.category_summaries(),
                "category_value_options": picker_options(category_options_from_tracker(current_tracker)),
                "error": error,
                **navigation_context(request),
            },
        )

    @app.post("/manage/categories")
    async def manage_categories_post(
        action: str = Form(...),
        name: str = Form(""),
        current_name: str = Form(""),
        new_name: str = Form(""),
        category_name: str = Form(""),
        subcategory_name: str = Form(""),
        replacement_category_value: str = Form(""),
        delete_strategy: str = Form(""),
    ):
        current_tracker = tracker()
        try:
            if action == "create-category":
                current_tracker.create_category(name)
            elif action == "rename-category":
                current_tracker.rename_category(current_name, new_name)
            elif action == "delete-category":
                replacement_category_name, replacement_subcategory_name = split_category_value(replacement_category_value)
                current_tracker.delete_category(
                    current_name,
                    replacement_category_name=replacement_category_name or None,
                    replacement_subcategory_name=replacement_subcategory_name,
                    delete_transactions=delete_strategy == "delete_transactions",
                )
            elif action == "create-subcategory":
                current_tracker.create_subcategory(category_name, subcategory_name)
            elif action == "rename-subcategory":
                current_tracker.rename_subcategory(category_name, current_name, new_name)
            elif action == "delete-subcategory":
                replacement_category_name, replacement_subcategory_name = split_category_value(replacement_category_value)
                current_tracker.delete_subcategory(
                    category_name,
                    current_name,
                    replacement_category_name=replacement_category_name or None,
                    replacement_subcategory_name=replacement_subcategory_name,
                    delete_transactions=delete_strategy == "delete_transactions",
                )
        except ValueError as exc:
            return RedirectResponse(f"/manage/categories?error={exc}", status_code=303)
        return RedirectResponse("/manage/categories", status_code=303)

    @app.get("/api/accounts")
    def api_accounts():
        return {"accounts": tracker().account_summaries()}

    @app.post("/api/accounts")
    def api_create_account(payload: dict = Body(...)):
        tracker().create_account(str(payload["name"]))
        return {"status": "ok"}

    @app.patch("/api/accounts/{account_name}")
    def api_rename_account(account_name: str, payload: dict = Body(...)):
        tracker().rename_account(account_name, str(payload["name"]))
        return {"status": "ok"}

    @app.delete("/api/accounts/{account_name}")
    def api_delete_account(account_name: str):
        tracker().delete_account(account_name)
        return {"status": "ok"}

    @app.get("/api/payees")
    def api_payees():
        return {"payees": tracker().payee_summaries()}

    @app.get("/api/categories")
    def api_categories():
        return {"categories": tracker().category_summaries()}

    @app.get("/api/reviews")
    def api_reviews(status: str | None = "for_review"):
        current_review_store = review_store()
        return {
            "reviews": [item.to_dict() for item in current_review_store.list_reviews(status=status)],
            "queue": current_review_store.queue_counts(),
        }

    @app.get("/api/reviews/{review_id}/llm-request")
    def api_review_llm_request(review_id: str):
        current_review = review_store().get_review(review_id)
        if current_review is None:
            raise HTTPException(status_code=404, detail="Review item not found")
        return review_store().llm_request_preview(
            review_id,
            metadata=tracker().metadata_snapshot(),
        )

    @app.post("/api/sms/intake")
    def api_sms_intake(payload: dict = Body(...)):
        content = str(payload.get("content", "")).strip()
        sender = str(payload.get("sender", "")).strip()
        received_at = str(payload.get("received_at", "")).strip()
        if not content or not sender or not received_at:
            raise HTTPException(status_code=400, detail="sender, content, and received_at are required.")
        current_review_store = review_store()
        review = current_review_store.enqueue_review(
            sender=sender,
            phone=str(payload.get("phone", "")).strip(),
            content=content,
            received_at=received_at,
            latitude=float(payload["latitude"]) if payload.get("latitude") is not None else None,
            longitude=float(payload["longitude"]) if payload.get("longitude") is not None else None,
            source=str(payload.get("source", "sms_intake")),
        )
        start_review_worker()
        stored_review = current_review_store.get_review(review.id) or review
        return {"review": stored_review.to_dict(), "queue": current_review_store.queue_counts()}

    @app.post("/api/transactions/reconcile")
    async def api_reconcile_transactions(file: UploadFile = File(...)):
        if not file.filename:
            raise HTTPException(status_code=400, detail="Choose a CSV file.")
        try:
            return reconcile_statement_csv(tracker(), await file.read())
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        finally:
            file.file.close()

    @app.get("/api/transactions")
    def api_transactions(
        account: str | None = None,
        category: str | None = None,
        payee: str | None = None,
        search: str | None = None,
        date: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
    ):
        records = tracker().list_transactions(
            account=account,
            category=category,
            payee=payee,
            search=search,
            exact_date=date,
            start_date=start_date,
            end_date=end_date,
        )
        return {"transactions": [serialize_transaction(item) for item in records]}

    @app.post("/api/transactions")
    async def api_create_transaction(
        account_name: str = Form(...),
        payee_name: str = Form(...),
        category_value: str = Form(...),
        amount: str = Form(...),
        spent_on: str = Form(...),
        notes: str = Form(""),
        files: list[UploadFile] = File(default=[]),
    ):
        current_tracker = tracker()
        category_name, subcategory_name = require_transaction_subcategory(category_value)
        with saved_uploads(files) as paths:
            created = current_tracker.add_transaction(
                account_name=account_name,
                payee_name=payee_name,
                category_name=category_name,
                subcategory_name=subcategory_name,
                amount=amount,
                spent_on=spent_on,
                notes=notes,
                attachment_paths=paths,
            )
        return {"transactions": [serialize_transaction(item) for item in created]}

    @app.put("/api/transactions/{transaction_id}")
    async def api_update_transaction(
        transaction_id: str,
        account_name: str = Form(...),
        payee_name: str = Form(...),
        category_value: str = Form(...),
        amount: str = Form(...),
        spent_on: str = Form(...),
        notes: str = Form(""),
        files: list[UploadFile] = File(default=[]),
    ):
        current_tracker = tracker()
        category_name, subcategory_name = require_transaction_subcategory(category_value)
        with saved_uploads(files) as paths:
            updated = current_tracker.update_transaction(
                transaction_id,
                account_name=account_name,
                payee_name=payee_name,
                category_name=category_name,
                subcategory_name=subcategory_name,
                amount=amount,
                spent_on=spent_on,
                notes=notes,
                attachment_paths=paths,
            )
        return {"transactions": [serialize_transaction(item) for item in updated]}

    @app.delete("/api/transactions/{transaction_id}")
    def api_delete_transaction(transaction_id: str):
        return {"deleted": tracker().delete_transaction(transaction_id)}

    return app
