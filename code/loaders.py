"""
Phase 1 — Deterministic data-access layer for "Buy or Wait?".

This module owns:
  - typed CSV loading for every participant-facing dataset file
  - explicit, non-lossy parsing of dates / numbers / booleans / pipe-lists
  - reusable cross-file indices (by request_id, user_id, event_id, ...)
  - a direct (non-inventing) exchange-rate lookup helper

Hard rule enforced throughout this file:
    A blank/missing value is NEVER silently coerced to 0, "", False, or
    an invented default. Blank required fields raise DataIntegrityError.
    Blank optional fields become None (or an empty tuple for pipe-lists).

No third-party dependencies. No LLM calls. No financial decision logic.
That belongs to later phases.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Dict, List, Optional, Tuple


# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------


class DataIntegrityError(ValueError):
    """Raised when a required field is missing/blank or fails to parse.

    We fail loudly instead of guessing, so a malformed row can never turn
    into a silently-wrong financial fact (e.g. blank amount -> 0).
    """


# --------------------------------------------------------------------------
# Low-level field parsing helpers
# --------------------------------------------------------------------------


def _blank_to_none(raw: Optional[str]) -> Optional[str]:
    """Return None for None/empty/whitespace-only strings, else the
    stripped string. This is the single choke point that decides what
    counts as "blank" across the whole loader."""
    if raw is None:
        return None
    stripped = raw.strip()
    return stripped if stripped != "" else None


def require_str(raw: Optional[str], *, field_name: str, context: str) -> str:
    value = _blank_to_none(raw)
    if value is None:
        raise DataIntegrityError(f"Required text field '{field_name}' is blank ({context})")
    return value


def optional_str(raw: Optional[str]) -> Optional[str]:
    return _blank_to_none(raw)


def require_decimal(raw: Optional[str], *, field_name: str, context: str) -> Decimal:
    value = _blank_to_none(raw)
    if value is None:
        raise DataIntegrityError(f"Required numeric field '{field_name}' is blank ({context})")
    try:
        return Decimal(value)
    except InvalidOperation as exc:
        raise DataIntegrityError(
            f"Field '{field_name}' has non-numeric value '{raw}' ({context})"
        ) from exc


def optional_decimal(raw: Optional[str], *, field_name: str, context: str) -> Optional[Decimal]:
    """Parses a numeric field that is allowed to be blank.

    Returns None on blank -- NEVER Decimal('0'). This is the function
    used for `financial_events.amount`, which is the field the challenge
    explicitly forbids treating as zero when blank.
    """
    value = _blank_to_none(raw)
    if value is None:
        return None
    try:
        return Decimal(value)
    except InvalidOperation as exc:
        raise DataIntegrityError(
            f"Field '{field_name}' has non-numeric value '{raw}' ({context})"
        ) from exc


def optional_int(raw: Optional[str], *, field_name: str, context: str) -> Optional[int]:
    value = _blank_to_none(raw)
    if value is None:
        return None
    try:
        return int(value)
    except ValueError as exc:
        raise DataIntegrityError(
            f"Field '{field_name}' has non-integer value '{raw}' ({context})"
        ) from exc


def require_bool(raw: Optional[str], *, field_name: str, context: str) -> bool:
    value = _blank_to_none(raw)
    if value is None:
        raise DataIntegrityError(f"Required boolean field '{field_name}' is blank ({context})")
    lowered = value.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    raise DataIntegrityError(
        f"Field '{field_name}' has unrecognized boolean value '{raw}' ({context})"
    )


def require_date(raw: Optional[str], *, field_name: str, context: str) -> date:
    value = _blank_to_none(raw)
    if value is None:
        raise DataIntegrityError(f"Required date field '{field_name}' is blank ({context})")
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        raise DataIntegrityError(
            f"Field '{field_name}' has unparseable date '{raw}' ({context})"
        ) from exc


def optional_date(raw: Optional[str], *, field_name: str, context: str) -> Optional[date]:
    value = _blank_to_none(raw)
    if value is None:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        raise DataIntegrityError(
            f"Field '{field_name}' has unparseable date '{raw}' ({context})"
        ) from exc


def require_datetime_utc(raw: Optional[str], *, field_name: str, context: str) -> datetime:
    value = _blank_to_none(raw)
    if value is None:
        raise DataIntegrityError(f"Required datetime field '{field_name}' is blank ({context})")
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError as exc:
        raise DataIntegrityError(
            f"Field '{field_name}' has unparseable datetime '{raw}' ({context})"
        ) from exc


def pipe_list(raw: Optional[str]) -> Tuple[str, ...]:
    """Parses a '|'-separated field. Blank -> empty tuple (not a list
    containing one blank string)."""
    value = _blank_to_none(raw)
    if value is None:
        return ()
    return tuple(item.strip() for item in value.split("|") if item.strip() != "")


def _read_rows(path: Path):
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            yield row


# --------------------------------------------------------------------------
# Record types
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Request:
    request_id: str
    user_id: str
    request_date: date
    request_type: str
    requested_amount: Decimal
    desired_completion_date: date
    allows_partial_payment: bool
    request_text: str


@dataclass(frozen=True)
class SampleRequest(Request):
    """A request from sample_requests.csv: same input columns plus the
    solved output columns. Used only for calibration, never as ground
    truth to hardcode against."""

    amount_safe_to_pay: Decimal
    affordability_status: str
    recommended_payment_method: str
    payment_plan: str
    earliest_date_for_full_payment: Optional[date]
    spending_changes_needed: str
    decision_explanation: str


@dataclass(frozen=True)
class FinancialProfile:
    user_id: str
    home_currency: str
    current_available_balance: Decimal
    minimum_balance_to_keep: Decimal
    financial_priorities: Tuple[str, ...]
    expense_categories_to_protect: Tuple[str, ...]
    expense_categories_user_is_willing_to_reduce: Tuple[str, ...]
    expense_categories_user_is_willing_to_stop: Tuple[str, ...]
    payment_methods_user_will_consider: Tuple[str, ...]
    max_installment_months: Optional[int]


@dataclass(frozen=True)
class FinancialEvent:
    event_id: str
    user_id: str
    event_type: str
    description: str
    category: str
    direction: str
    amount: Optional[Decimal]  # None means blank in source -- must be
    # resolved via a linked image before it can be used financially.
    currency: str
    event_date: date
    settlement_date: Optional[date]
    status: str
    linked_event_id: Optional[str]
    flexibility: str
    minimum_allowed_amount: Optional[Decimal]


@dataclass(frozen=True)
class PaymentOption:
    payment_option_id: str
    request_id: str
    payment_method: str
    payment_amount: Decimal
    number_of_payments: int
    first_payment_date: Optional[date]
    payment_frequency_days: Optional[int]
    financing_fee: Decimal
    total_payable_amount: Decimal


@dataclass(frozen=True)
class Message:
    message_id: str
    user_id: str
    request_id: Optional[str]
    related_event_id: Optional[str]
    sent_at: datetime
    source_type: str
    message_text: str


@dataclass(frozen=True)
class Image:
    image_id: str
    user_id: str
    request_id: Optional[str]
    related_event_id: Optional[str]
    file_path: Path


@dataclass(frozen=True)
class ExchangeRate:
    rate_date: date
    from_currency: str
    to_currency: str
    rate: Decimal


# --------------------------------------------------------------------------
# Per-file loaders
# --------------------------------------------------------------------------


def load_requests(dataset_dir: Path) -> List[Request]:
    out: List[Request] = []
    for row in _read_rows(dataset_dir / "requests.csv"):
        ctx = f"requests.csv/{row.get('request_id')}"
        out.append(
            Request(
                request_id=require_str(row.get("request_id"), field_name="request_id", context=ctx),
                user_id=require_str(row.get("user_id"), field_name="user_id", context=ctx),
                request_date=require_date(row.get("request_date"), field_name="request_date", context=ctx),
                request_type=require_str(row.get("request_type"), field_name="request_type", context=ctx),
                requested_amount=require_decimal(
                    row.get("requested_amount"), field_name="requested_amount", context=ctx
                ),
                desired_completion_date=require_date(
                    row.get("desired_completion_date"), field_name="desired_completion_date", context=ctx
                ),
                allows_partial_payment=require_bool(
                    row.get("allows_partial_payment"), field_name="allows_partial_payment", context=ctx
                ),
                request_text=require_str(row.get("request_text"), field_name="request_text", context=ctx),
            )
        )
    return out


def load_sample_requests(dataset_dir: Path) -> List[SampleRequest]:
    out: List[SampleRequest] = []
    for row in _read_rows(dataset_dir / "sample_requests.csv"):
        ctx = f"sample_requests.csv/{row.get('request_id')}"
        out.append(
            SampleRequest(
                request_id=require_str(row.get("request_id"), field_name="request_id", context=ctx),
                user_id=require_str(row.get("user_id"), field_name="user_id", context=ctx),
                request_date=require_date(row.get("request_date"), field_name="request_date", context=ctx),
                request_type=require_str(row.get("request_type"), field_name="request_type", context=ctx),
                requested_amount=require_decimal(
                    row.get("requested_amount"), field_name="requested_amount", context=ctx
                ),
                desired_completion_date=require_date(
                    row.get("desired_completion_date"), field_name="desired_completion_date", context=ctx
                ),
                allows_partial_payment=require_bool(
                    row.get("allows_partial_payment"), field_name="allows_partial_payment", context=ctx
                ),
                request_text=require_str(row.get("request_text"), field_name="request_text", context=ctx),
                amount_safe_to_pay=require_decimal(
                    row.get("amount_safe_to_pay"), field_name="amount_safe_to_pay", context=ctx
                ),
                affordability_status=require_str(
                    row.get("affordability_status"), field_name="affordability_status", context=ctx
                ),
                recommended_payment_method=require_str(
                    row.get("recommended_payment_method"),
                    field_name="recommended_payment_method",
                    context=ctx,
                ),
                payment_plan=require_str(row.get("payment_plan"), field_name="payment_plan", context=ctx),
                earliest_date_for_full_payment=optional_date(
                    row.get("earliest_date_for_full_payment"),
                    field_name="earliest_date_for_full_payment",
                    context=ctx,
                ),
                spending_changes_needed=require_str(
                    row.get("spending_changes_needed"),
                    field_name="spending_changes_needed",
                    context=ctx,
                ),
                decision_explanation=require_str(
                    row.get("decision_explanation"), field_name="decision_explanation", context=ctx
                ),
            )
        )
    return out


def load_profiles(dataset_dir: Path) -> List[FinancialProfile]:
    out: List[FinancialProfile] = []
    for row in _read_rows(dataset_dir / "financial_profiles.csv"):
        ctx = f"financial_profiles.csv/{row.get('user_id')}"
        out.append(
            FinancialProfile(
                user_id=require_str(row.get("user_id"), field_name="user_id", context=ctx),
                home_currency=require_str(row.get("home_currency"), field_name="home_currency", context=ctx),
                current_available_balance=require_decimal(
                    row.get("current_available_balance"),
                    field_name="current_available_balance",
                    context=ctx,
                ),
                minimum_balance_to_keep=require_decimal(
                    row.get("minimum_balance_to_keep"), field_name="minimum_balance_to_keep", context=ctx
                ),
                financial_priorities=pipe_list(row.get("financial_priorities")),
                expense_categories_to_protect=pipe_list(row.get("expense_categories_to_protect")),
                expense_categories_user_is_willing_to_reduce=pipe_list(
                    row.get("expense_categories_user_is_willing_to_reduce")
                ),
                expense_categories_user_is_willing_to_stop=pipe_list(
                    row.get("expense_categories_user_is_willing_to_stop")
                ),
                payment_methods_user_will_consider=pipe_list(
                    row.get("payment_methods_user_will_consider")
                ),
                # Blank means "will not consider installments" per the
                # dataset contract -- must stay None, never 0.
                max_installment_months=optional_int(
                    row.get("max_installment_months"), field_name="max_installment_months", context=ctx
                ),
            )
        )
    return out


def load_events(dataset_dir: Path) -> List[FinancialEvent]:
    out: List[FinancialEvent] = []
    for row in _read_rows(dataset_dir / "financial_events.csv"):
        ctx = f"financial_events.csv/{row.get('event_id')}"
        out.append(
            FinancialEvent(
                event_id=require_str(row.get("event_id"), field_name="event_id", context=ctx),
                user_id=require_str(row.get("user_id"), field_name="user_id", context=ctx),
                event_type=require_str(row.get("event_type"), field_name="event_type", context=ctx),
                description=require_str(row.get("description"), field_name="description", context=ctx),
                category=require_str(row.get("category"), field_name="category", context=ctx),
                direction=require_str(row.get("direction"), field_name="direction", context=ctx),
                # NEVER default to Decimal('0') here -- a blank amount
                # must be resolved from a linked image later.
                amount=optional_decimal(row.get("amount"), field_name="amount", context=ctx),
                currency=require_str(row.get("currency"), field_name="currency", context=ctx),
                event_date=require_date(row.get("event_date"), field_name="event_date", context=ctx),
                settlement_date=optional_date(
                    row.get("settlement_date"), field_name="settlement_date", context=ctx
                ),
                status=require_str(row.get("status"), field_name="status", context=ctx),
                linked_event_id=optional_str(row.get("linked_event_id")),
                flexibility=require_str(row.get("flexibility"), field_name="flexibility", context=ctx),
                minimum_allowed_amount=optional_decimal(
                    row.get("minimum_allowed_amount"), field_name="minimum_allowed_amount", context=ctx
                ),
            )
        )
    return out


def load_payment_options(dataset_dir: Path) -> List[PaymentOption]:
    out: List[PaymentOption] = []
    for row in _read_rows(dataset_dir / "request_payment_options.csv"):
        ctx = f"request_payment_options.csv/{row.get('payment_option_id')}"
        out.append(
            PaymentOption(
                payment_option_id=require_str(
                    row.get("payment_option_id"), field_name="payment_option_id", context=ctx
                ),
                request_id=require_str(row.get("request_id"), field_name="request_id", context=ctx),
                payment_method=require_str(row.get("payment_method"), field_name="payment_method", context=ctx),
                payment_amount=require_decimal(
                    row.get("payment_amount"), field_name="payment_amount", context=ctx
                ),
                number_of_payments=optional_int(
                    row.get("number_of_payments"), field_name="number_of_payments", context=ctx
                )
                or 0,
                first_payment_date=optional_date(
                    row.get("first_payment_date"), field_name="first_payment_date", context=ctx
                ),
                payment_frequency_days=optional_int(
                    row.get("payment_frequency_days"), field_name="payment_frequency_days", context=ctx
                ),
                financing_fee=require_decimal(
                    row.get("financing_fee"), field_name="financing_fee", context=ctx
                ),
                total_payable_amount=require_decimal(
                    row.get("total_payable_amount"), field_name="total_payable_amount", context=ctx
                ),
            )
        )
    return out


def load_messages(dataset_dir: Path) -> List[Message]:
    out: List[Message] = []
    for row in _read_rows(dataset_dir / "messages.csv"):
        ctx = f"messages.csv/{row.get('message_id')}"
        out.append(
            Message(
                message_id=require_str(row.get("message_id"), field_name="message_id", context=ctx),
                user_id=require_str(row.get("user_id"), field_name="user_id", context=ctx),
                request_id=optional_str(row.get("request_id")),
                related_event_id=optional_str(row.get("related_event_id")),
                sent_at=require_datetime_utc(row.get("sent_at"), field_name="sent_at", context=ctx),
                source_type=require_str(row.get("source_type"), field_name="source_type", context=ctx),
                message_text=require_str(row.get("message_text"), field_name="message_text", context=ctx),
            )
        )
    return out


def load_images(dataset_dir: Path) -> List[Image]:
    out: List[Image] = []
    images_dir = dataset_dir / "media" / "images"
    for row in _read_rows(dataset_dir / "images.csv"):
        ctx = f"images.csv/{row.get('image_id')}"
        image_id = require_str(row.get("image_id"), field_name="image_id", context=ctx)
        out.append(
            Image(
                image_id=image_id,
                user_id=require_str(row.get("user_id"), field_name="user_id", context=ctx),
                request_id=optional_str(row.get("request_id")),
                related_event_id=optional_str(row.get("related_event_id")),
                file_path=images_dir / f"{image_id}.png",
            )
        )
    return out


def load_exchange_rates(dataset_dir: Path) -> List[ExchangeRate]:
    out: List[ExchangeRate] = []
    for row in _read_rows(dataset_dir / "exchange_rates.csv"):
        ctx = f"exchange_rates.csv/{row.get('rate_date')}:{row.get('from_currency')}->{row.get('to_currency')}"
        out.append(
            ExchangeRate(
                rate_date=require_date(row.get("rate_date"), field_name="rate_date", context=ctx),
                from_currency=require_str(row.get("from_currency"), field_name="from_currency", context=ctx),
                to_currency=require_str(row.get("to_currency"), field_name="to_currency", context=ctx),
                rate=require_decimal(row.get("rate"), field_name="rate", context=ctx),
            )
        )
    return out


# --------------------------------------------------------------------------
# Dataset container + indices
# --------------------------------------------------------------------------


@dataclass
class Dataset:
    requests: List[Request]
    sample_requests: List[SampleRequest]
    profiles: List[FinancialProfile]
    events: List[FinancialEvent]
    payment_options: List[PaymentOption]
    messages: List[Message]
    images: List[Image]
    exchange_rates: List[ExchangeRate]

    requests_by_id: Dict[str, Request] = field(default_factory=dict)
    sample_requests_by_id: Dict[str, SampleRequest] = field(default_factory=dict)
    profiles_by_user: Dict[str, FinancialProfile] = field(default_factory=dict)
    events_by_id: Dict[str, FinancialEvent] = field(default_factory=dict)
    events_by_user: Dict[str, List[FinancialEvent]] = field(default_factory=dict)
    events_by_linked_event_id: Dict[str, List[FinancialEvent]] = field(default_factory=dict)
    payment_options_by_request: Dict[str, List[PaymentOption]] = field(default_factory=dict)
    messages_by_request: Dict[str, List[Message]] = field(default_factory=dict)
    messages_by_user: Dict[str, List[Message]] = field(default_factory=dict)
    messages_by_related_event: Dict[str, List[Message]] = field(default_factory=dict)
    images_by_request: Dict[str, List[Image]] = field(default_factory=dict)
    images_by_related_event: Dict[str, List[Image]] = field(default_factory=dict)
    exchange_rate_index: Dict[Tuple[date, str, str], Decimal] = field(default_factory=dict)

    # ---- referential helpers built on top of the indices ----

    def all_request_ids(self):
        """Union of evaluation request IDs and solved sample request IDs.
        request_payment_options.csv / messages.csv / images.csv reference
        both spaces (request_01..request_25 are samples, request_26..
        request_275 are the evaluation set)."""
        return set(self.requests_by_id) | set(self.sample_requests_by_id)

    def find_image_for_event(self, event_id: str) -> Optional[Image]:
        candidates = self.images_by_related_event.get(event_id, [])
        return candidates[0] if candidates else None


def _index_by(items, key_fn) -> Dict:
    out: Dict = {}
    for item in items:
        out[key_fn(item)] = item
    return out


def _group_by(items, key_fn) -> Dict[str, List]:
    out: Dict[str, List] = {}
    for item in items:
        key = key_fn(item)
        if key is None:
            continue
        out.setdefault(key, []).append(item)
    return out


def build_indices(ds: Dataset) -> None:
    ds.requests_by_id = _index_by(ds.requests, lambda r: r.request_id)
    ds.sample_requests_by_id = _index_by(ds.sample_requests, lambda r: r.request_id)
    ds.profiles_by_user = _index_by(ds.profiles, lambda p: p.user_id)
    ds.events_by_id = _index_by(ds.events, lambda e: e.event_id)
    ds.events_by_user = _group_by(ds.events, lambda e: e.user_id)
    ds.events_by_linked_event_id = _group_by(ds.events, lambda e: e.linked_event_id)
    ds.payment_options_by_request = _group_by(ds.payment_options, lambda p: p.request_id)
    ds.messages_by_request = _group_by(ds.messages, lambda m: m.request_id)
    ds.messages_by_user = _group_by(ds.messages, lambda m: m.user_id)
    ds.messages_by_related_event = _group_by(ds.messages, lambda m: m.related_event_id)
    ds.images_by_request = _group_by(ds.images, lambda i: i.request_id)
    ds.images_by_related_event = _group_by(ds.images, lambda i: i.related_event_id)
    ds.exchange_rate_index = {
        (r.rate_date, r.from_currency, r.to_currency): r.rate for r in ds.exchange_rates
    }


def default_dataset_dir() -> Path:
    """Resolve dataset/ relative to this file's repo root, never a
    hardcoded absolute path, so this works after clone/rename/checkout."""
    return Path(__file__).resolve().parent.parent / "dataset"


def load_dataset(dataset_dir: Optional[Path] = None) -> Dataset:
    dataset_dir = dataset_dir or default_dataset_dir()
    ds = Dataset(
        requests=load_requests(dataset_dir),
        sample_requests=load_sample_requests(dataset_dir),
        profiles=load_profiles(dataset_dir),
        events=load_events(dataset_dir),
        payment_options=load_payment_options(dataset_dir),
        messages=load_messages(dataset_dir),
        images=load_images(dataset_dir),
        exchange_rates=load_exchange_rates(dataset_dir),
    )
    build_indices(ds)
    return ds


# --------------------------------------------------------------------------
# Exchange-rate lookup (direct only -- no chaining, no inversion, no
# invention; see evaluation/data_stats.py for the coverage report that
# tells Phase 2 whether direct lookup is actually sufficient)
# --------------------------------------------------------------------------


def get_exchange_rate(
    ds: Dataset, rate_date: date, from_currency: str, to_currency: str
) -> Optional[Decimal]:
    """Returns the fixed rate for exactly this date + direction, or None
    if no such row exists in exchange_rates.csv.

    Same-currency conversion always returns Decimal('1') -- this is not
    an invented rate, it is a mathematical identity.

    This function deliberately does NOT:
      - invert a reverse-direction row (e.g. use ZAR->EUR from an
        EUR->ZAR row)
      - chain through an intermediate currency (e.g. ZAR->USD->IDR)
      - fall back to the nearest date

    Any of those would be an invented conversion rule not present in the
    data. If direct lookup is insufficient for some request, that is a
    gap to resolve explicitly in Phase 2, not silently paper over here.
    """
    if from_currency == to_currency:
        return Decimal("1")
    return ds.exchange_rate_index.get((rate_date, from_currency, to_currency))
