from __future__ import annotations

import shutil
import tempfile
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from typing import Iterator
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from fastapi import Body, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .cli import default_data_file
from .formatting import format_inr
from .review_workflow import ReviewWorkflowStore
from .tracker import ExpenseTracker

BASE_DIR = Path(__file__).resolve().parent
TEMPLATES = Jinja2Templates(directory=str(BASE_DIR / "templates"))
TEMPLATES.env.filters["currency"] = format_inr


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
            grouped[key] = {"date": key, "transactions": []}
        grouped[key]["transactions"].append(transaction)
    return list(grouped.values())


def add_error_query(url: str, message: str) -> str:
    split = urlsplit(url or "/transactions")
    query = [(key, value) for key, value in parse_qsl(split.query, keep_blank_values=True) if key != "error"]
    query.append(("error", message))
    return urlunsplit((split.scheme, split.netloc, split.path, urlencode(query), split.fragment))


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
    error: str | None,
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
        "selected_spent_on": transaction.spent_on.isoformat() if transaction else date.today().isoformat(),
        "error": error,
    }


def review_form_context(
    current_tracker: ExpenseTracker,
    review,
    *,
    error: str | None,
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
    }


def navigation_context(request: Request) -> dict[str, str]:
    account = request.query_params.get("account")
    add_transaction_href = "/transactions/new"
    if request.url.path == "/transactions" and account:
        add_transaction_href = f"/transactions/new?{urlencode({'account': account})}"
    return {"add_transaction_href": add_transaction_href}


def create_app(data_file: Path | None = None) -> FastAPI:
    app = FastAPI(title="Expense Tracker")
    app.state.data_file = data_file or default_data_file()
    app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

    def tracker() -> ExpenseTracker:
        return ExpenseTracker(app.state.data_file)

    def review_store() -> ReviewWorkflowStore:
        return ReviewWorkflowStore(app.state.data_file)

    def transaction_context(request: Request, current_tracker: ExpenseTracker, error: str | None = None, **filters: str | None) -> dict[str, object]:
        metadata = current_tracker.metadata_snapshot()
        transactions = current_tracker.list_transactions(**filters)
        current_url = request.url.path
        if request.url.query:
            current_url = f"{current_url}?{request.url.query}"
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

    @app.get("/")
    def dashboard(request: Request):
        current_tracker = tracker()
        return TEMPLATES.TemplateResponse(
            request,
            "dashboard.html",
            {
                "accounts": current_tracker.account_summaries(),
                "balances": current_tracker.account_balances(),
                "pending_reviews": review_store().pending_count(),
                **navigation_context(request),
            },
        )

    @app.get("/reviews")
    def review_list(request: Request, error: str | None = None):
        return TEMPLATES.TemplateResponse(
            request,
            "reviews.html",
            {
                "reviews": review_store().list_reviews(),
                "error": error,
                **navigation_context(request),
            },
        )

    @app.get("/reviews/{review_id}")
    def review_detail(request: Request, review_id: str, error: str | None = None):
        current_review = review_store().get_review(review_id)
        if current_review is None:
            raise HTTPException(status_code=404, detail="Review item not found")
        return TEMPLATES.TemplateResponse(
            request,
            "review_detail.html",
            review_form_context(tracker(), current_review, error=error) | navigation_context(request),
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
    ):
        current_tracker = tracker()
        current_review_store = review_store()
        current_review = current_review_store.get_review(review_id)
        if current_review is None:
            raise HTTPException(status_code=404, detail="Review item not found")
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
                review_form_context(current_tracker, current_review, error=str(exc)) | navigation_context(request),
                status_code=400,
            )
        return RedirectResponse("/reviews", status_code=303)

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
    def new_transaction_form(request: Request, account: str | None = None):
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
                error=None,
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
        files: list[UploadFile] = File(default=[]),
    ):
        current_tracker = tracker()
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
                    error=str(exc),
                )
                | navigation_context(request),
                status_code=400,
            )
        return RedirectResponse(f"/transactions?{urlencode({'account': account_name})}", status_code=303)

    @app.get("/transactions/{transaction_id}/edit")
    def edit_transaction_form(request: Request, transaction_id: str):
        current_tracker = tracker()
        transaction = current_tracker.get_transaction(transaction_id)
        if transaction is None:
            raise HTTPException(status_code=404, detail="Transaction not found")
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
                error=None,
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
        files: list[UploadFile] = File(default=[]),
    ):
        current_tracker = tracker()
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
                    error=str(exc),
                )
                | navigation_context(request),
                status_code=400,
            )
        return RedirectResponse(f"/transactions?{urlencode({'account': account_name})}", status_code=303)

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
    def delete_transaction(transaction_id: str):
        tracker().delete_transaction(transaction_id)
        return RedirectResponse("/transactions", status_code=303)

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
        return {"reviews": [item.to_dict() for item in review_store().list_reviews(status=status)]}

    @app.post("/api/sms/intake")
    def api_sms_intake(payload: dict = Body(...)):
        content = str(payload.get("content", "")).strip()
        sender = str(payload.get("sender", "")).strip()
        received_at = str(payload.get("received_at", "")).strip()
        if not content or not sender or not received_at:
            raise HTTPException(status_code=400, detail="sender, content, and received_at are required.")
        review = review_store().create_review(
            sender=sender,
            phone=str(payload.get("phone", "")).strip(),
            content=content,
            received_at=received_at,
            latitude=float(payload["latitude"]) if payload.get("latitude") is not None else None,
            longitude=float(payload["longitude"]) if payload.get("longitude") is not None else None,
            source=str(payload.get("source", "sms_intake")),
        )
        return {"review": review.to_dict()}

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
