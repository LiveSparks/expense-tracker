from __future__ import annotations

import argparse
import csv
import json
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Callable, Literal, Sequence
from uuid import uuid4

from pydantic import BaseModel, ConfigDict

from .models import Account, Category, LedgerData, Payee, Subcategory, Transaction

AMOUNT_PATTERNS = [
    re.compile(r"(?:paid|sent|spent|debited|credited|received|withdrawn|purchased?)\s+\b(?:rs\.?|inr)\s*([0-9,]+(?:\.\d{1,2})?)", re.IGNORECASE),
    re.compile(r"\b(?:rs\.?|inr)\s*([0-9,]+(?:\.\d{1,2})?)", re.IGNORECASE),
]
BALANCE_PATTERN = re.compile(r"(?:balance|bal(?:ance)?)\D+(?:rs\.?|inr)\s*([0-9,]+(?:\.\d{1,2})?)", re.IGNORECASE)
REFERENCE_PATTERN = re.compile(r"\b(?:ref|utr|txn(?:\s*id|#)?|txvr|trxn)\s*[:#-]?\s*([A-Za-z0-9-]{4,})", re.IGNORECASE)
ACCOUNT_HINT_PATTERNS = [
    re.compile(r"(?:a/c|account|acct)\s*[*xX#-]*\s*([0-9]{3,6})", re.IGNORECASE),
    re.compile(r"card\s*[*xX#-]*\s*([0-9]{3,6})", re.IGNORECASE),
]
MERCHANT_PATTERNS = [
    re.compile(r"\bto\s+([A-Za-z0-9&./' -]{3,}?)\s+on\b", re.IGNORECASE),
    re.compile(r"\bat\s+([A-Za-z0-9&./' -]{3,}?)\s+(?:on|txn|txvr|ref|using|for)\b", re.IGNORECASE),
    re.compile(r"\bfor\s+([A-Za-z0-9&./' -]{3,}?)\s+at\b", re.IGNORECASE),
]
TIMESTAMP_PATTERN = re.compile(r"(20\d{2}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}|\d{2}/\d{2}/\d{2})")
FINANCIAL_CONTACT_TOKENS = ("HDFCBK", "ICICI", "SBI", "PNB", "AXIS", "AIRTEL", "PAYTM", "AMAZON", "FASTAG")
OTP_PATTERN = re.compile(
    r"\b(?:otp|one[- ]time password|verification code|confirmation code|valid for \d+|do not share|don't share|expired in \d+|confirm(?:ation)? code)\b",
    re.IGNORECASE,
)
REMINDER_PATTERN = re.compile(
    r"\b(?:reminder|upcoming|due on|auto[- ]?debit|autopay|standing instruction|e-?mandate|will be debited|will be presented)\b",
    re.IGNORECASE,
)
STATEMENT_PATTERN = re.compile(
    r"\b(?:statement is sent|minimum due|total due|bill payment reminder|late fee|credit card statement)\b",
    re.IGNORECASE,
)
PROMO_OR_SERVICE_PATTERN = re.compile(
    r"\b(?:kyc|claim it now|subscription free|bonus points|expiring soon|welcome to|complaint has been registered|appointment .* submitted|safe network|recharge now|apple music|fraud and scam|verify your mobile)\b",
    re.IGNORECASE,
)
BOT_MEDIA_PATTERN = re.compile(r"^\[(?:application/|image/|video/|audio/)", re.IGNORECASE)
TRANSACTION_KEYWORDS = {
    "paid": "debit",
    "sent": "debit",
    "debited": "debit",
    "spent": "debit",
    "purchase": "debit",
    "purchased": "debit",
    "withdrawn": "debit",
    "toll": "toll",
    "fastag": "toll",
    "credited": "credit",
    "received": "credit",
    "refund": "credit",
    "salary": "credit",
    "upi": "transfer",
}
GENERIC_PAYEE_BUCKETS = {"shop", "online", "family", "unknown", "petrol"}


class ReviewDraftTextFormat(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message_kind: Literal["transaction", "non_transactional"]
    account_name: str
    payee_name: str
    category_value: str
    amount: str
    spent_on: str
    notes: str
    non_transactional_reason: str
    review_status: Literal["for_review", "filtered"]
    confidence: float
    reasoning: list[str]


@dataclass(slots=True)
class LegacyTransaction:
    row_number: int
    account_name: str
    spent_on: date
    payee_name: str
    notes: str
    raw_category: str
    category_group: str
    category_name: str
    amount: Decimal
    cleared: str

    def to_dict(self) -> dict[str, object]:
        return {
            "row_number": self.row_number,
            "account_name": self.account_name,
            "spent_on": self.spent_on.isoformat(),
            "payee_name": self.payee_name,
            "notes": self.notes,
            "raw_category": self.raw_category,
            "category_group": self.category_group,
            "category_name": self.category_name,
            "amount": f"{self.amount:.2f}",
            "cleared": self.cleared,
        }


@dataclass(slots=True)
class SmsMessage:
    row_number: int
    received_at: datetime
    direction: str
    contact: str
    phone: str
    content: str
    message_type: str

    def to_dict(self) -> dict[str, object]:
        return {
            "row_number": self.row_number,
            "received_at": self.received_at.isoformat(),
            "direction": self.direction,
            "contact": self.contact,
            "phone": self.phone,
            "content": self.content,
            "message_type": self.message_type,
        }


@dataclass(slots=True)
class SmsMarkers:
    useful: bool
    amount: Decimal | None
    event_kind: str
    sender_hint: str
    account_hint: str | None
    merchant_hint: str | None
    reference_hint: str | None
    balance_hint: Decimal | None
    timestamp_hint: str | None
    matched_keywords: list[str] = field(default_factory=list)
    confidence: float = 0.0
    extraction_method: str = "regex"

    def to_dict(self) -> dict[str, object]:
        return {
            "useful": self.useful,
            "amount": f"{self.amount:.2f}" if self.amount is not None else None,
            "event_kind": self.event_kind,
            "sender_hint": self.sender_hint,
            "account_hint": self.account_hint,
            "merchant_hint": self.merchant_hint,
            "reference_hint": self.reference_hint,
            "balance_hint": f"{self.balance_hint:.2f}" if self.balance_hint is not None else None,
            "timestamp_hint": self.timestamp_hint,
            "matched_keywords": self.matched_keywords,
            "confidence": round(self.confidence, 3),
            "extraction_method": self.extraction_method,
        }


@dataclass(slots=True)
class SmsTransactionMatch:
    sms: SmsMessage
    markers: SmsMarkers
    transaction: LegacyTransaction | None
    score: float
    reasons: list[str]
    field_associations: dict[str, list[str]]

    def to_dict(self) -> dict[str, object]:
        return {
            "sms": self.sms.to_dict(),
            "markers": self.markers.to_dict(),
            "transaction": self.transaction.to_dict() if self.transaction is not None else None,
            "score": round(self.score, 3),
            "reasons": self.reasons,
            "field_associations": self.field_associations,
        }


@dataclass(slots=True)
class AnalysisArtifacts:
    transactions: list[LegacyTransaction]
    useful_sms: list[SmsMessage]
    matches: list[SmsTransactionMatch]
    prompt_pack: dict[str, object]
    mock_ledger_seed: dict[str, object]
    mock_sms_samples: list[dict[str, object]]


def normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value.replace("\u202f", " ").replace("\xa0", " ")).strip()


def parse_decimal(value: str) -> Decimal:
    cleaned = re.sub(r"[^0-9.+-]", "", value.replace(",", ""))
    if not cleaned:
        raise ValueError(f"Could not parse decimal from {value!r}")
    return Decimal(cleaned)


def split_category(raw_category: str) -> tuple[str, str]:
    normalized = normalize_text(raw_category)
    if not normalized:
        return "Uncategorized", "General"
    if ":" in normalized:
        group, name = normalized.split(":", maxsplit=1)
        return normalize_text(group) or "Uncategorized", normalize_text(name) or "General"
    return normalized, "General"


def parse_sms_datetime(raw_date: str, raw_time: str) -> datetime:
    normalized_date = normalize_text(raw_date)
    normalized_time = normalize_text(raw_time)
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", normalized_date):
        return datetime.strptime(f"{normalized_date} {normalized_time}", "%Y-%m-%d %H:%M:%S")
    normalized = normalize_text(f"{normalized_date} {normalized_time}").lower().replace("a.m.", "am").replace("p.m.", "pm")
    normalized = normalized.replace("am", " am").replace("pm", " pm")
    normalized = normalize_text(normalized)
    return datetime.strptime(normalized, "%d/%m/%Y %I:%M:%S %p")


def iter_sms_csv_rows(csv_path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with csv_path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.reader(handle)
        header: list[str] | None = None
        rows: list[dict[str, str]] = []
        for raw_row in reader:
            if not raw_row or not any(cell.strip() for cell in raw_row):
                continue
            if header is None:
                if raw_row[:7] == ["Date", "Time", "Direction", "Contact", "Phone", "Content", "Type"]:
                    header = raw_row[:7]
                continue
            padded_row = raw_row + [""] * max(0, len(header) - len(raw_row))
            rows.append({key: value for key, value in zip(header, padded_row)})
        if header is None:
            raise ValueError(f"Could not find an SMS CSV header row in {csv_path}.")
        return header, rows


def load_legacy_transactions(csv_path: Path) -> list[LegacyTransaction]:
    with csv_path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        transactions: list[LegacyTransaction] = []
        for row_number, row in enumerate(reader, start=1):
            category_group, category_name = split_category(row.get("Category", ""))
            transactions.append(
                LegacyTransaction(
                    row_number=row_number,
                    account_name=normalize_text(row.get("Account", "Unknown")) or "Unknown",
                    spent_on=date.fromisoformat(normalize_text(row.get("Date", "1970-01-01")) or "1970-01-01"),
                    payee_name=normalize_text(row.get("Payee", "Unknown")) or "Unknown",
                    notes=normalize_text(row.get("Notes", "")),
                    raw_category=normalize_text(row.get("Category", "")),
                    category_group=category_group,
                    category_name=category_name,
                    amount=parse_decimal(row.get("Amount", "0")),
                    cleared=normalize_text(row.get("Cleared", "")),
                )
            )
    return transactions


def load_sms_messages(csv_path: Path) -> list[SmsMessage]:
    _, rows = iter_sms_csv_rows(csv_path)
    messages: list[SmsMessage] = []
    for row_number, row in enumerate(rows, start=1):
        messages.append(
            SmsMessage(
                row_number=row_number,
                received_at=parse_sms_datetime(row.get("Date", "01/01/1970"), row.get("Time", "12:00:00 am")),
                direction=normalize_text(row.get("Direction", "")),
                contact=normalize_text(row.get("Contact", "")),
                phone=normalize_text(row.get("Phone", "")),
                content=normalize_text(row.get("Content", "")),
                message_type=normalize_text(row.get("Type", "SMS")) or "SMS",
            )
        )
    return messages


def detect_event_kind(content: str) -> tuple[str, list[str]]:
    lowered = content.lower()
    found = [keyword for keyword in TRANSACTION_KEYWORDS if keyword in lowered]
    if not found:
        return "unknown", []
    kinds = [TRANSACTION_KEYWORDS[keyword] for keyword in found]
    if "toll" in kinds:
        return "toll", found
    if "credit" in kinds and "debit" not in kinds:
        return "credit", found
    if "debit" in kinds and "credit" not in kinds:
        return "debit", found
    if "transfer" in kinds:
        return "transfer", found
    return kinds[0], found


def looks_like_financial_sms(message: SmsMessage) -> bool:
    lowered = message.content.lower()
    has_amount = any(pattern.search(message.content) for pattern in AMOUNT_PATTERNS)
    has_financial_contact = any(token.lower() in message.contact.lower() for token in FINANCIAL_CONTACT_TOKENS)
    event_kind, found_keywords = detect_event_kind(message.content)
    return bool(has_amount and (has_financial_contact or found_keywords or event_kind != "unknown"))


def find_first_match(patterns: Sequence[re.Pattern[str]], content: str) -> str | None:
    for pattern in patterns:
        match = pattern.search(content)
        if match:
            return normalize_text(match.group(1))
    return None


def clean_merchant_hint(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = normalize_text(value)
    cleaned = re.sub(r"\b(?:txn|txvr|ref)\b.*$", "", cleaned, flags=re.IGNORECASE).strip(" .,-")
    return cleaned or None


def is_pending_transaction_reminder(content: str) -> bool:
    return bool(REMINDER_PATTERN.search(content))


def is_otp_message(content: str) -> bool:
    return bool(OTP_PATTERN.search(content))


def is_statement_or_due_message(content: str) -> bool:
    return bool(STATEMENT_PATTERN.search(content))


def is_promo_or_service_message(content: str) -> bool:
    return bool(PROMO_OR_SERVICE_PATTERN.search(content))


def is_bot_media_message(content: str) -> bool:
    return bool(BOT_MEDIA_PATTERN.search(content.strip()))


def is_personal_contact(contact: str) -> bool:
    normalized = normalize_text(contact)
    if not normalized:
        return False
    if any(token.lower() in normalized.lower() for token in FINANCIAL_CONTACT_TOKENS):
        return False
    if "@" in normalized:
        return False
    if re.fullmatch(r"[A-Z]{2}-[A-Z0-9-]+", normalized):
        return False
    if normalized.isdigit():
        return False
    return True


def regex_filter_reasons(message: SmsMessage, markers: SmsMarkers) -> list[str]:
    reasons: list[str] = []
    if is_bot_media_message(message.content):
        reasons.append("Bot/media payload was filtered before drafting.")
    if is_otp_message(message.content):
        reasons.append("OTP or verification message was filtered before drafting.")
    if is_personal_contact(message.contact):
        reasons.append("Message from a personal contact was filtered before drafting.")
    if is_pending_transaction_reminder(message.content):
        reasons.append("Reminder or upcoming auto-debit message was filtered before drafting.")
    if is_statement_or_due_message(message.content):
        reasons.append("Statement or bill-due message was filtered before drafting.")
    if is_promo_or_service_message(message.content):
        reasons.append("Promo, service, or KYC-style message was filtered before drafting.")
    if not markers.useful:
        reasons.append("Regex extraction did not classify this message as a useful financial SMS.")
    return reasons


def extract_markers_regex(message: SmsMessage) -> SmsMarkers:
    amount: Decimal | None = None
    for pattern in AMOUNT_PATTERNS:
        match = pattern.search(message.content)
        if match:
            amount = parse_decimal(match.group(1))
            break

    balance_hint = None
    balance_match = BALANCE_PATTERN.search(message.content)
    if balance_match:
        balance_hint = parse_decimal(balance_match.group(1))

    account_hint = find_first_match(ACCOUNT_HINT_PATTERNS, message.content)
    merchant_hint = clean_merchant_hint(find_first_match(MERCHANT_PATTERNS, message.content))
    reference_hint = find_first_match([REFERENCE_PATTERN], message.content)
    timestamp_match = TIMESTAMP_PATTERN.search(message.content)
    timestamp_hint = timestamp_match.group(1) if timestamp_match else None
    event_kind, matched_keywords = detect_event_kind(message.content)
    useful = looks_like_financial_sms(message)

    confidence = 0.15
    if useful:
        confidence += 0.2
    if amount is not None:
        confidence += 0.25
    if merchant_hint:
        confidence += 0.15
    if account_hint:
        confidence += 0.1
    if reference_hint:
        confidence += 0.1
    if event_kind != "unknown":
        confidence += 0.1
    if balance_hint is not None:
        confidence += 0.05

    return SmsMarkers(
        useful=useful,
        amount=amount,
        event_kind=event_kind,
        sender_hint=message.contact,
        account_hint=account_hint,
        merchant_hint=merchant_hint,
        reference_hint=reference_hint,
        balance_hint=balance_hint,
        timestamp_hint=timestamp_hint,
        matched_keywords=matched_keywords,
        confidence=min(confidence, Decimal("0.99")) if isinstance(confidence, Decimal) else min(confidence, 0.99),
        extraction_method="regex",
    )


def extract_sms_markers(
    message: SmsMessage,
    llm_fallback: Callable[[SmsMessage, SmsMarkers], SmsMarkers | None] | None = None,
) -> SmsMarkers:
    markers = extract_markers_regex(message)
    if llm_fallback is None or markers.confidence >= 0.65:
        return markers
    fallback_markers = llm_fallback(message, markers)
    if fallback_markers is None:
        return markers
    return fallback_markers


def tokenize(value: str) -> set[str]:
    return {token for token in re.findall(r"[a-z0-9]+", value.lower()) if len(token) >= 3}


def score_sms_transaction_match(markers: SmsMarkers, message: SmsMessage, transaction: LegacyTransaction) -> tuple[float, list[str]]:
    score = 0.0
    reasons: list[str] = []

    if markers.amount is not None:
        sms_amount = abs(markers.amount)
        txn_amount = abs(transaction.amount)
        if sms_amount == txn_amount:
            score += 6.0
            reasons.append(f"Exact amount match: {sms_amount:.2f}.")
        elif abs(sms_amount - txn_amount) <= Decimal("5.00"):
            score += 1.5
            reasons.append(f"Near amount match within Rs.5: {sms_amount:.2f} vs {txn_amount:.2f}.")

    day_gap = abs((transaction.spent_on - message.received_at.date()).days)
    if day_gap == 0:
        score += 4.0
        reasons.append("Same-day transaction date.")
    elif day_gap == 1:
        score += 3.0
        reasons.append("One-day date gap.")
    elif day_gap <= 3:
        score += 1.0
        reasons.append(f"Date gap within {day_gap} days.")

    if markers.account_hint and markers.account_hint in transaction.account_name:
        score += 2.5
        reasons.append(f"Account suffix {markers.account_hint} appears in {transaction.account_name}.")

    sender_tokens = tokenize(markers.sender_hint)
    account_tokens = tokenize(transaction.account_name)
    if sender_tokens & account_tokens:
        score += 1.0
        reasons.append("Sender/bank token overlaps the transaction account.")

    merchant_tokens = tokenize(markers.merchant_hint or "")
    payee_tokens = tokenize(transaction.payee_name)
    if merchant_tokens and merchant_tokens & payee_tokens:
        score += 2.5
        reasons.append("Merchant hint overlaps the transaction payee.")

    if markers.event_kind == "toll" and ("fasttag" in transaction.payee_name.lower() or "bill" in transaction.raw_category.lower()):
        score += 1.5
        reasons.append("Toll/FASTag signal aligns with the transaction bucket.")

    if markers.event_kind == "credit" and transaction.amount > 0:
        score += 1.5
        reasons.append("Credit-style SMS aligns with a positive transaction.")
    if markers.event_kind in {"debit", "toll", "transfer"} and transaction.amount < 0:
        score += 1.5
        reasons.append("Debit-style SMS aligns with a negative transaction.")

    if markers.reference_hint and transaction.notes and markers.reference_hint.lower() in transaction.notes.lower():
        score += 1.0
        reasons.append("Reference hint appears in transaction notes.")

    return score, reasons


def build_field_associations(markers: SmsMarkers, transaction: LegacyTransaction) -> dict[str, list[str]]:
    associations: dict[str, list[str]] = {
        "account": [],
        "payee": [],
        "category": [],
        "notes": [],
    }

    if markers.account_hint:
        associations["account"].append(
            f"SMS account hint {markers.account_hint!r} points toward account {transaction.account_name!r}, but the stored field is a normalized ledger account name."
        )
    else:
        associations["account"].append(
            f"Sender {markers.sender_hint!r} suggests the account family for {transaction.account_name!r} even without an explicit account suffix."
        )

    if markers.merchant_hint:
        associations["payee"].append(
            f"Merchant hint {markers.merchant_hint!r} correlates with payee {transaction.payee_name!r}, but the payee may be a curated bucket rather than a literal SMS merchant string."
        )
    elif transaction.payee_name.lower() in GENERIC_PAYEE_BUCKETS:
        associations["payee"].append(
            f"The matched payee {transaction.payee_name!r} is a normalized bucket, so raw SMS merchant text likely maps partly to notes instead of the payee field."
        )

    associations["category"].append(
        f"This SMS pattern maps to category {transaction.category_group!r} / {transaction.category_name!r} through historical precedent, not by exact string equality in the message body."
    )

    note_fragments = [fragment for fragment in [markers.merchant_hint, markers.reference_hint, markers.timestamp_hint] if fragment]
    if note_fragments:
        associations["notes"].append(
            f"Reference/merchant/timestamp fragments {note_fragments!r} are useful note material even when the canonical payee/category are normalized differently."
        )
    elif transaction.notes:
        associations["notes"].append(
            f"Historical note {transaction.notes!r} captures details that are only implied by the SMS markers."
        )

    return associations


def build_minimal_note_bullets(markers: SmsMarkers, transaction: LegacyTransaction) -> str:
    bullets: list[str] = []
    if markers.reference_hint:
        bullets.append(f"- Ref: {markers.reference_hint}")
    if transaction.notes:
        bullets.append(f"- Note: {transaction.notes}")
    return "\n".join(bullets)


def build_structured_output_schema() -> dict[str, object]:
    schema = ReviewDraftTextFormat.model_json_schema()
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "expense_tracker_review_draft",
            "strict": True,
            "schema": schema,
        },
    }


def match_sms_to_transactions(
    messages: Sequence[SmsMessage],
    transactions: Sequence[LegacyTransaction],
    *,
    threshold: float = 7.0,
    llm_fallback: Callable[[SmsMessage, SmsMarkers], SmsMarkers | None] | None = None,
) -> list[SmsTransactionMatch]:
    matches: list[SmsTransactionMatch] = []
    useful_messages = [message for message in messages if looks_like_financial_sms(message)]
    for message in useful_messages:
        markers = extract_sms_markers(message, llm_fallback=llm_fallback)
        if markers.event_kind == "toll":
            matches.append(
                SmsTransactionMatch(
                    sms=message,
                    markers=markers,
                    transaction=None,
                    score=0.0,
                    reasons=["Toll/FASTag SMS rows are excluded from historical matching because the legacy ledger did not record them individually."],
                    field_associations={},
                )
            )
            continue
        ranked: list[tuple[LegacyTransaction, float, list[str]]] = []
        for transaction in transactions:
            if markers.amount is None:
                continue
            if abs(abs(transaction.amount) - abs(markers.amount)) > Decimal("5.00"):
                continue
            score, reasons = score_sms_transaction_match(markers, message, transaction)
            day_gap = abs((transaction.spent_on - message.received_at.date()).days)
            if score > 0 and day_gap <= 5:
                ranked.append((transaction, score, reasons))
        ranked.sort(key=lambda item: (item[1], item[0].spent_on), reverse=True)
        if ranked and ranked[0][1] >= threshold:
            transaction, score, reasons = ranked[0]
            matches.append(
                SmsTransactionMatch(
                    sms=message,
                    markers=markers,
                    transaction=transaction,
                    score=score,
                    reasons=reasons,
                    field_associations=build_field_associations(markers, transaction),
                )
            )
        else:
            matches.append(
                SmsTransactionMatch(
                    sms=message,
                    markers=markers,
                    transaction=None,
                    score=ranked[0][1] if ranked else 0.0,
                    reasons=ranked[0][2] if ranked else ["No transaction exceeded the matching threshold."],
                    field_associations={},
                )
            )
    return matches


def retrieve_similar_matches(
    target: SmsTransactionMatch,
    history: Sequence[SmsTransactionMatch],
    *,
    limit: int = 3,
) -> list[SmsTransactionMatch]:
    ranked: list[tuple[float, SmsTransactionMatch]] = []
    for candidate in history:
        if candidate.sms.row_number == target.sms.row_number or candidate.transaction is None:
            continue
        similarity = 0.0
        if target.markers.amount is not None and candidate.markers.amount == target.markers.amount:
            similarity += 3.0
        if target.markers.event_kind == candidate.markers.event_kind and target.markers.event_kind != "unknown":
            similarity += 1.5
        if target.markers.sender_hint == candidate.markers.sender_hint:
            similarity += 1.5
        if tokenize(target.markers.merchant_hint or "") & tokenize(candidate.markers.merchant_hint or ""):
            similarity += 2.0
        if target.transaction and candidate.transaction and target.transaction.category_group == candidate.transaction.category_group:
            similarity += 1.0
        if similarity > 0:
            ranked.append((similarity, candidate))
    ranked.sort(key=lambda item: (item[0], item[1].score), reverse=True)
    return [candidate for _, candidate in ranked[:limit]]


def available_metadata_from_transactions(transactions: Sequence[LegacyTransaction]) -> dict[str, list[str]]:
    accounts = sorted({transaction.account_name for transaction in transactions}, key=str.casefold)
    payees = sorted({transaction.payee_name for transaction in transactions}, key=str.casefold)
    categories = sorted({f"{transaction.category_group} / {transaction.category_name}" for transaction in transactions}, key=str.casefold)
    return {"accounts": accounts, "payees": payees, "categories": categories}


def build_prompt_text(prompt_pack: dict[str, object]) -> str:
    lines = [f"Model: {prompt_pack['model']}", "", str(prompt_pack["system_prompt"]), "", "Worked examples:"]
    for index, example in enumerate(prompt_pack["examples"], start=1):
        lines.append(f"Example {index}")
        lines.append(json.dumps(example, indent=2))
    lines.append("")
    lines.append("Structured output schema")
    lines.append(json.dumps(prompt_pack["response_format"], indent=2))
    lines.append("")
    lines.append("Live prompt template")
    lines.append(json.dumps(prompt_pack["template_input"], indent=2))
    return "\n".join(lines)


def build_prompt_pack(
    matches: Sequence[SmsTransactionMatch],
    transactions: Sequence[LegacyTransaction],
    *,
    example_limit: int = 4,
) -> dict[str, object]:
    matched = [match for match in matches if match.transaction is not None]
    metadata = available_metadata_from_transactions(transactions)
    diverse_examples: list[SmsTransactionMatch] = []
    seen_event_kinds: set[str] = set()
    for match in sorted(matched, key=lambda item: item.score, reverse=True):
        if match.markers.event_kind in seen_event_kinds and len(diverse_examples) >= example_limit:
            continue
        diverse_examples.append(match)
        seen_event_kinds.add(match.markers.event_kind)
        if len(diverse_examples) >= example_limit:
            break

    examples = []
    for match in diverse_examples:
        similar = retrieve_similar_matches(match, matched, limit=3)
        transaction = match.transaction
        if transaction is None:
            continue
        examples.append(
            {
                "input": {
                    "new_sms": match.sms.to_dict(),
                    "extracted_markers": match.markers.to_dict(),
                    "similar_examples": [candidate.to_dict() for candidate in similar],
                    "available_accounts": metadata["accounts"],
                    "available_payees": metadata["payees"],
                    "available_categories": metadata["categories"],
                },
                "expected_output": {
                    "account_name": transaction.account_name,
                    "payee_name": transaction.payee_name,
                    "category_value": f"{transaction.category_group} / {transaction.category_name}",
                    "amount": f"{transaction.amount:.2f}",
                    "spent_on": transaction.spent_on.isoformat(),
                    "notes": build_minimal_note_bullets(match.markers, transaction),
                    "review_status": "for_review",
                    "confidence": round(min(0.99, max(0.5, match.markers.confidence)), 2),
                    "reasoning": match.reasons + [
                        item
                        for association in match.field_associations.values()
                        for item in association
                    ],
                },
            }
        )

    return {
        "model": "gpt-5-mini",
        "system_prompt": (
            "You are an assistant that drafts a single expense-tracker transaction for human review. "
            "Use the new SMS, extracted markers, similar historical SMS+transaction examples, and the allowed account/payee/category lists. "
            "Prefer exact existing metadata values when they fit. Use OpenAI structured output with the provided schema. "
            "Return account_name, payee_name, category_value, amount, spent_on, notes, review_status, confidence, and reasoning. "
            "Keep notes minimal and in bullet-list form. Only include high-value details such as references/transaction IDs and concise bank-provided transaction notes. "
            "Do not invent toll history from legacy examples because FASTag deductions were not recorded individually in the old ledger."
        ),
        "response_format": build_structured_output_schema(),
        "examples": examples,
        "template_input": {
            "new_sms": {
                "received_at": "2026-03-19T11:30:00",
                "contact": "JM-HDFCBK-S",
                "content": "Sent Rs.499.00 From HDFC Bank A/C *2054 To Amazon Seller Services On 19/03/26 Ref 552880661565 Note household order",
            },
            "extracted_markers": {
                "amount": "499.00",
                "event_kind": "debit",
                "sender_hint": "JM-HDFCBK-S",
                "account_hint": "2054",
                "merchant_hint": "Amazon Seller Services",
                "reference_hint": "552880661565",
            },
            "similar_examples": "<insert retrieved similar examples here>",
            "available_accounts": metadata["accounts"],
            "available_payees": metadata["payees"],
            "available_categories": metadata["categories"],
        },
    }


def build_mock_ledger_seed(matches: Sequence[SmsTransactionMatch], *, limit: int = 12) -> dict[str, object]:
    matched_transactions = [match.transaction for match in matches if match.transaction is not None][:limit]
    accounts: list[Account] = []
    payees: list[Payee] = []
    categories: list[Category] = []
    subcategories: list[Subcategory] = []
    account_ids: dict[str, str] = {}
    payee_ids: dict[str, str] = {}
    category_ids: dict[str, str] = {}
    subcategory_ids: dict[tuple[str, str], str] = {}
    ledger_transactions: list[Transaction] = []

    for transaction in matched_transactions:
        if transaction is None:
            continue
        if transaction.account_name not in account_ids:
            account_ids[transaction.account_name] = str(uuid4())
            accounts.append(Account(id=account_ids[transaction.account_name], name=transaction.account_name))
        if transaction.payee_name not in payee_ids:
            payee_ids[transaction.payee_name] = str(uuid4())
            payees.append(Payee(id=payee_ids[transaction.payee_name], name=transaction.payee_name))
        if transaction.category_group not in category_ids:
            category_ids[transaction.category_group] = str(uuid4())
            categories.append(Category(id=category_ids[transaction.category_group], name=transaction.category_group))
        sub_key = (transaction.category_group, transaction.category_name)
        if sub_key not in subcategory_ids:
            subcategory_ids[sub_key] = str(uuid4())
            subcategories.append(
                Subcategory(
                    id=subcategory_ids[sub_key],
                    name=transaction.category_name,
                    category_id=category_ids[transaction.category_group],
                )
            )
        ledger_transactions.append(
            Transaction(
                id=str(uuid4()),
                entry_type="income" if transaction.amount > 0 else "expense",
                account_id=account_ids[transaction.account_name],
                payee_id=payee_ids[transaction.payee_name],
                category_id=category_ids[transaction.category_group],
                subcategory_id=subcategory_ids[sub_key],
                amount=transaction.amount,
                notes=transaction.notes,
                spent_on=transaction.spent_on,
                created_at=datetime.combine(transaction.spent_on, time(hour=12, minute=0)),
            )
        )

    return LedgerData(
        accounts=accounts,
        payees=payees,
        categories=categories,
        subcategories=subcategories,
        transactions=ledger_transactions,
        attachments=[],
    ).to_dict()


def build_mock_sms_samples(matches: Sequence[SmsTransactionMatch], *, limit: int = 10) -> list[dict[str, object]]:
    samples: list[dict[str, object]] = []
    for match in matches[:limit]:
        sample = {
            "sms": match.sms.to_dict(),
            "markers": match.markers.to_dict(),
            "matched_transaction": match.transaction.to_dict() if match.transaction is not None else None,
            "match_reasons": match.reasons,
        }
        samples.append(sample)
    return samples


def write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def analyze_legacy_sms_data(
    transactions_csv: Path,
    sms_csv: Path,
    output_dir: Path,
) -> AnalysisArtifacts:
    transactions = load_legacy_transactions(transactions_csv)
    sms_messages = load_sms_messages(sms_csv)
    useful_sms = [message for message in sms_messages if looks_like_financial_sms(message)]
    matches = match_sms_to_transactions(sms_messages, transactions)
    matched_only = [match for match in matches if match.transaction is not None]
    prompt_pack = build_prompt_pack(matched_only, transactions)
    mock_ledger_seed = build_mock_ledger_seed(matched_only)
    mock_sms_samples = build_mock_sms_samples(matches)

    write_json(
        output_dir / "analysis_summary.json",
        {
            "transaction_count": len(transactions),
            "sms_count": len(sms_messages),
            "useful_sms_count": len(useful_sms),
            "matched_sms_count": len(matched_only),
            "top_senders": Counter(message.contact for message in useful_sms).most_common(12),
            "top_category_pairs": Counter(
                f"{transaction.category_group} / {transaction.category_name}" for transaction in transactions
            ).most_common(12),
        },
    )
    write_json(output_dir / "matched_examples.json", [match.to_dict() for match in matched_only[:100]])
    write_json(output_dir / "prompt_pack.json", prompt_pack)
    (output_dir / "prompt_pack.txt").write_text(build_prompt_text(prompt_pack), encoding="utf-8")
    write_json(output_dir / "mock_ledger_seed.json", mock_ledger_seed)
    write_json(output_dir / "mock_sms_samples.json", mock_sms_samples)

    return AnalysisArtifacts(
        transactions=transactions,
        useful_sms=useful_sms,
        matches=matches,
        prompt_pack=prompt_pack,
        mock_ledger_seed=mock_ledger_seed,
        mock_sms_samples=mock_sms_samples,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="expense-tracker analyze-sms",
        description="One-off legacy SMS analysis plus production prompt artifacts.",
    )
    parser.add_argument(
        "--transactions-csv",
        type=Path,
        default=Path("/root/All-Accounts_2.csv"),
        help="Path to the legacy labeled transactions CSV.",
    )
    parser.add_argument(
        "--sms-csv",
        type=Path,
        default=Path("/root/all_sms.csv"),
        help="Path to the legacy SMS CSV.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path(__file__).resolve().parent.parent / "data" / "sms_pipeline",
        help="Directory where analysis artifacts should be written.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    artifacts = analyze_legacy_sms_data(args.transactions_csv, args.sms_csv, args.output_dir)
    matched = len([match for match in artifacts.matches if match.transaction is not None])
    print(
        f"Analyzed {len(artifacts.transactions)} transactions and {len(artifacts.useful_sms)} useful SMS messages; "
        f"matched {matched}. Artifacts written to {args.output_dir}."
    )
    return 0
