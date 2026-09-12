"""
Phase 2 — data model for reconstructed per-user financial state.

Pure data structures only. No reconstruction logic lives here (see
reconstruction.py) and no financial-decision / 90-day-simulation logic
lives here (that is Phase 3+).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Dict, List, Optional, Tuple


@dataclass(frozen=True)
class ImageExtractionResult:
    """Outcome of trying to resolve a blank financial-event amount."""

    status: str
    # "structured"        -- amount was not blank; nothing to extract
    # "extracted"         -- OCR found exactly one unambiguous amount
    # "ocr_unavailable"   -- no local OCR engine importable in this env
    # "no_image_reference"-- amount blank but no images.csv row for it
    # "ambiguous"         -- OCR ran but text was unusable/multi-valued
    # "extraction_failed" -- image file missing or unreadable
    amount: Optional[Decimal]
    method: Optional[str]  # e.g. "tesseract-ocr", or None
    source_image_id: Optional[str]
    raw_text_excerpt: Optional[str]  # first ~200 chars of OCR text, for audit only
    detail: str


@dataclass(frozen=True)
class NormalizedEvent:
    """Mirrors financial_events.csv 1:1 (no source field discarded) plus
    resolution provenance and a deterministic classification."""

    event_id: str
    user_id: str
    event_type: str
    description: str
    category: str
    direction: str
    currency: str
    event_date: date
    settlement_date: Optional[date]
    status: str
    linked_event_id: Optional[str]
    flexibility: str
    minimum_allowed_amount: Optional[Decimal]

    amount: Optional[Decimal]  # resolved amount (structured or image); None if unresolved
    amount_source: str  # "structured" | "image" | "unresolved"
    source_image_id: Optional[str]

    classification: str  # see reconstruction.CLASS_* constants
    counts_as_cash_flow: bool
    classification_reason: str

    superseded_by_message: bool
    message_note: Optional[str]

    excluded_as_duplicate: bool
    duplicate_reason: Optional[str]


@dataclass(frozen=True)
class RecurringSeries:
    user_id: str
    category: str
    direction: str  # "debit" | "credit"
    event_ids: Tuple[str, ...]
    typical_amount: Decimal
    interval_days: int
    flexibility: str
    minimum_allowed_amount: Optional[Decimal]
    is_protected: bool
    is_reducible: bool
    is_stoppable: bool
    last_event_date: date


@dataclass(frozen=True)
class MessageEvidence:
    message_id: str
    user_id: str
    request_id: Optional[str]
    related_event_id: Optional[str]
    signal_types: Tuple[str, ...]
    extracted_amounts: Tuple[Tuple[str, Decimal], ...]
    extracted_dates: Tuple[date, ...]
    applied: bool
    application_note: str


@dataclass(frozen=True)
class FxGap:
    """Reports whether a foreign-currency event's conversion need is
    supported by exchange_rates.csv. Informational only in Phase 2 --
    nothing is converted yet based on this."""

    event_id: str
    user_id: str
    event_currency: str
    home_currency: str
    rate_date: Optional[date]
    direct_rate_available: bool
    single_hop_chain_available: bool
    chain_path: Optional[Tuple[str, str, str]]


@dataclass
class FinancialState:
    """Reusable, per-user, reconstructed (not-yet-simulated) financial
    picture. Phase 3 consumes this; no recommendation/simulation logic
    lives here."""

    user_id: str
    home_currency: str
    current_available_balance: Decimal
    minimum_balance_to_keep: Decimal
    expense_categories_to_protect: Tuple[str, ...]
    expense_categories_user_is_willing_to_reduce: Tuple[str, ...]
    expense_categories_user_is_willing_to_stop: Tuple[str, ...]
    payment_methods_user_will_consider: Tuple[str, ...]
    max_installment_months: Optional[int]

    normalized_events: List[NormalizedEvent] = field(default_factory=list)
    recurring_expenses: List[RecurringSeries] = field(default_factory=list)
    recurring_income: List[RecurringSeries] = field(default_factory=list)
    confirmed_future_income_events: List[NormalizedEvent] = field(default_factory=list)
    one_time_events: List[NormalizedEvent] = field(default_factory=list)
    message_evidence: List[MessageEvidence] = field(default_factory=list)
