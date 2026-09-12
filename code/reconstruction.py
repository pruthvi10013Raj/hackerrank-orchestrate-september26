"""
Phase 2 -- Deterministic financial-state reconstruction.

Turns raw loaders.Dataset rows into the per-user state.FinancialState
objects defined in state.py. Every decision made here is a
classification / resolution decision about what the data already says --
never a forecast, a simulation, or a payment recommendation. Those are
Phase 3+ (90-day simulator) and Phase 4+ (payment-plan ranking).

Design rules carried over from Phase 1 and made explicit here:
  - A blank amount is NEVER defaulted to 0. It is only ever resolved via
    (a) a linked image (OCR, best-effort) or (b) unambiguous message
    evidence naming the same event. If neither resolves it, the event's
    `amount` stays None and `amount_source == "unresolved"` -- Phase 3
    must decide how to treat it (e.g. exclude from simulation), not this
    module.
  - No LLM calls anywhere in this file.
  - Currency conversion is NOT performed here. FxGap only *reports*
    whether exchange_rates.csv could support a conversion; it never
    invents a chained or inverted rate (that would duplicate/violate the
    Phase 1 `get_exchange_rate` contract).
  - "cancelled" and "failed" events never happened financially.
    "settled" and "scheduled" count. A "pending" event is direction-aware,
    per the challenge contract: a pending DEBIT is RESERVED (counted),
    e.g. a disputed/possible-duplicate card charge with no reversal
    posted yet; a pending CREDIT is excluded until it settles, confirmed
    by direct message evidence in this dataset (refund/payout/prize
    messages explicitly saying money "has not reached your account
    yet"). An "unrealized" investment valuation (direction == "non_cash")
    is always excluded, regardless of status -- it is a paper value, not
    cash. These are classification defaults; Phase 3 owns whether to ever
    override one for a specific simulation scenario.
"""

from __future__ import annotations

import re
from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import loaders
import message_evidence as message_evidence_mod
from state import (
    FinancialState,
    FxGap,
    ImageExtractionResult,
    MessageEvidence,
    NormalizedEvent,
    RecurringSeries,
)

# --------------------------------------------------------------------------
# Classification constants
# --------------------------------------------------------------------------

CLASS_RECURRING_EXPENSE = "recurring_expense"
CLASS_RECURRING_INCOME = "recurring_income"
CLASS_CONFIRMED_FUTURE_INCOME = "confirmed_future_income"
CLASS_ONE_TIME = "one_time"
CLASS_NON_CASH_FLOW = "non_cash_flow"  # cancelled / failed / pending (default)

_STATUS_COUNTS_BY_DEFAULT = {
    "settled": True,
    "scheduled": True,
    "pending": False,  # overridden for direction == "debit"; see _classify_status
    "cancelled": False,
    "failed": False,
    "unrealized": False,  # investment valuation snapshots; never cash (see below)
}

_STATUS_REASON = {
    "settled": "settled events already moved money.",
    "scheduled": "scheduled events are confirmed to occur (e.g. a next-payroll salary) "
    "and are treated as a confirmed future cash flow.",
    "pending_credit": "pending credits are not yet confirmed to have reached the account "
    "(dataset messages describe pending refunds/payouts/prizes as not-yet-received); "
    "excluded from cash flow until they settle, per the challenge contract "
    "('Do not count pending credits ... until they settle').",
    "pending_debit": "pending debits are RESERVED even though not yet settled, per the "
    "challenge contract ('Reserve pending debits'). This includes a disputed/possible-"
    "duplicate charge under investigation with no reversal posted yet: the financially "
    "safer interpretation is to keep it reserved until an explicit cancellation/reversal "
    "is confirmed.",
    "pending_non_cash": "pending non-cash events never move cash regardless of status; excluded.",
    "cancelled": "cancelled events never completed; excluded from cash flow.",
    "failed": "failed events did not move money; excluded from cash flow.",
    "unrealized": "unrealized investment valuation snapshots (event_type == "
    "'investment_valuation', direction == 'non_cash') report a paper value only -- no "
    "units were sold and no cash was generated; never counted as available cash "
    "(explicit challenge rule: 'do not treat unrealized investment value as available cash').",
}

# A handful of category/direction bundles that recur monthly for most
# users (rent, utilities, subscriptions, groceries, ...). We don't
# hardcode category *names* -- we detect recurrence structurally instead
# (same user + category + direction + flexibility, evenly spaced dates),
# so this works for any category the dataset happens to contain.
_MIN_OCCURRENCES_FOR_RECURRING = 3
_INTERVAL_TOLERANCE_DAYS = 5


# --------------------------------------------------------------------------
# Image-based blank-amount resolution
# --------------------------------------------------------------------------


def _try_ocr(file_path: Path) -> Tuple[Optional[str], str]:
    """Best-effort OCR of a receipt/screenshot image. Returns
    (raw_text_or_None, detail_message). Never raises.

    No LLM: uses a local OCR engine (pytesseract+Pillow) if importable.
    If the environment has no OCR engine, this deterministically reports
    "ocr_unavailable" rather than guessing -- guessing an amount would
    violate the "never invent a blank amount" rule just as badly as
    defaulting to 0.
    """
    if not file_path.exists():
        return None, f"image file not found on disk: {file_path}"
    try:
        import pytesseract  # type: ignore
        from PIL import Image as PILImage  # type: ignore
    except ImportError:
        return None, "no local OCR engine importable (pytesseract/Pillow not installed)"
    try:
        img = PILImage.open(file_path)
        text = pytesseract.image_to_string(img)
        return text, "ocr executed"
    except Exception as exc:  # pragma: no cover - defensive, environment-dependent
        return None, f"OCR raised {type(exc).__name__}: {exc}"


_AMOUNT_RE = re.compile(r"(?<![\d.])\d[\d,]*(?:\.\d+)?(?![\d.])")


def resolve_blank_amount_via_image(
    ds: loaders.Dataset, event: loaders.FinancialEvent
) -> ImageExtractionResult:
    if event.amount is not None:
        return ImageExtractionResult(
            status="structured",
            amount=event.amount,
            method=None,
            source_image_id=None,
            raw_text_excerpt=None,
            detail="amount was not blank in financial_events.csv",
        )

    image = ds.find_image_for_event(event.event_id)
    if image is None:
        return ImageExtractionResult(
            status="no_image_reference",
            amount=None,
            method=None,
            source_image_id=None,
            raw_text_excerpt=None,
            detail=f"amount is blank for {event.event_id} and images.csv has no row "
            f"with related_event_id == {event.event_id!r}",
        )

    text, detail = _try_ocr(image.file_path)
    if text is None:
        status = "extraction_failed" if "not found" in detail or "raised" in detail else "ocr_unavailable"
        return ImageExtractionResult(
            status=status,
            amount=None,
            method=None,
            source_image_id=image.image_id,
            raw_text_excerpt=None,
            detail=detail,
        )

    candidates = _AMOUNT_RE.findall(text)
    numeric_candidates = []
    for c in candidates:
        cleaned = c.replace(",", "")
        try:
            numeric_candidates.append(Decimal(cleaned))
        except Exception:
            continue
    # de-duplicate while preserving order
    seen = set()
    unique_candidates = []
    for n in numeric_candidates:
        if n not in seen:
            seen.add(n)
            unique_candidates.append(n)

    if len(unique_candidates) == 1:
        return ImageExtractionResult(
            status="extracted",
            amount=unique_candidates[0],
            method="tesseract-ocr",
            source_image_id=image.image_id,
            raw_text_excerpt=text[:200],
            detail="exactly one numeric candidate found in OCR text",
        )
    return ImageExtractionResult(
        status="ambiguous",
        amount=None,
        method="tesseract-ocr" if text else None,
        source_image_id=image.image_id,
        raw_text_excerpt=text[:200] if text else None,
        detail=f"OCR produced {len(unique_candidates)} distinct numeric candidates "
        f"(expected exactly 1); refusing to guess which one is the amount",
    )


# --------------------------------------------------------------------------
# Event normalization / classification
# --------------------------------------------------------------------------


def _classify_status(status: str, direction: str) -> Tuple[bool, str]:
    """Direction-aware classification. The challenge contract explicitly
    treats a pending DEBIT differently from a pending CREDIT: "Reserve
    pending debits. Do not count pending credits, bonuses, commissions,
    refunds, lottery proceeds, or investment gains until they settle."
    A plain status lookup can't express that asymmetry, so "pending" is
    special-cased here before falling back to the direction-independent
    table for every other status.
    """
    if direction == "non_cash":
        # investment valuation snapshots and similar non-cash markers never
        # move money, regardless of what status they carry.
        return False, _STATUS_REASON.get(
            status, f"non_cash direction; excluded regardless of status {status!r}"
        )
    if status == "pending":
        if direction == "debit":
            return True, _STATUS_REASON["pending_debit"]
        if direction == "credit":
            return False, _STATUS_REASON["pending_credit"]
        return False, _STATUS_REASON["pending_non_cash"]

    counts = _STATUS_COUNTS_BY_DEFAULT.get(status)
    reason = _STATUS_REASON.get(status)
    if counts is None:
        # Unknown status value not seen during Phase 0/1 auditing --
        # fail safe (exclude) rather than silently assume it counts.
        return False, f"unrecognized status {status!r}; excluded from cash flow by default (fail-safe)"
    return counts, reason


def normalize_event(
    ds: loaders.Dataset,
    event: loaders.FinancialEvent,
    message_evidence_by_event: Dict[str, List[MessageEvidence]],
) -> NormalizedEvent:
    image_result = resolve_blank_amount_via_image(ds, event)
    amount = image_result.amount
    amount_source = "structured" if event.amount is not None else (
        "image" if image_result.status == "extracted" else "unresolved"
    )
    source_image_id = image_result.source_image_id if amount_source == "image" else None

    # If still unresolved via image, see if unambiguous message evidence
    # tied to this exact event fills the gap. We only ever apply this
    # when there is exactly one candidate amount for this event across
    # its linked messages -- multiple/conflicting candidates are left
    # unresolved rather than guessed.
    message_note = None
    superseded_by_message = False
    if amount is None:
        evidences = message_evidence_by_event.get(event.event_id, [])
        candidate_amounts = {
            amt for ev in evidences for (_, amt) in ev.extracted_amounts
        }
        if len(candidate_amounts) == 1:
            amount = next(iter(candidate_amounts))
            amount_source = "message"
            message_note = "amount resolved from a single unambiguous message evidence amount"
        elif len(candidate_amounts) > 1:
            message_note = (
                f"{len(candidate_amounts)} conflicting amount candidates in linked messages; "
                "left unresolved rather than guessed"
            )

    counts_by_status, status_reason = _classify_status(event.status, event.direction)

    if event.linked_event_id:
        classification_reason = (
            f"{status_reason} (linked_event_id={event.linked_event_id} retained as provenance; "
            "no separate duplicate-exclusion rule needed because status alone already "
            "determines whether this row counts)"
        )
    else:
        classification_reason = status_reason

    classification = CLASS_ONE_TIME  # refined into recurring/confirmed-income by the caller

    return NormalizedEvent(
        event_id=event.event_id,
        user_id=event.user_id,
        event_type=event.event_type,
        description=event.description,
        category=event.category,
        direction=event.direction,
        currency=event.currency,
        event_date=event.event_date,
        settlement_date=event.settlement_date,
        status=event.status,
        linked_event_id=event.linked_event_id,
        flexibility=event.flexibility,
        minimum_allowed_amount=event.minimum_allowed_amount,
        amount=amount,
        amount_source=amount_source if amount is not None else "unresolved",
        source_image_id=source_image_id,
        classification=classification,
        counts_as_cash_flow=counts_by_status,
        classification_reason=classification_reason,
        superseded_by_message=superseded_by_message,
        message_note=message_note,
        excluded_as_duplicate=False,
        duplicate_reason=None,
    )


# --------------------------------------------------------------------------
# Recurring-series detection (structural, not category-name-based)
# --------------------------------------------------------------------------


def _detect_recurring_series(events: List[NormalizedEvent]) -> List[RecurringSeries]:
    """Groups a single user's events by (category, direction, flexibility)
    and keeps groups whose event_dates are roughly evenly spaced with
    >= _MIN_OCCURRENCES_FOR_RECURRING members. This is a structural
    detector -- it makes no assumption about which category names are
    "recurring" a priori.
    """
    groups: Dict[Tuple[str, str, str], List[NormalizedEvent]] = {}
    for e in events:
        if e.status not in ("settled", "scheduled"):
            continue
        key = (e.category, e.direction, e.flexibility)
        groups.setdefault(key, []).append(e)

    series_list: List[RecurringSeries] = []
    for (category, direction, flexibility), members in groups.items():
        if len(members) < _MIN_OCCURRENCES_FOR_RECURRING:
            continue
        members_sorted = sorted(members, key=lambda e: e.event_date)
        gaps = [
            (members_sorted[i + 1].event_date - members_sorted[i].event_date).days
            for i in range(len(members_sorted) - 1)
        ]
        if not gaps:
            continue
        median_gap = sorted(gaps)[len(gaps) // 2]
        if median_gap <= 0:
            continue
        regular = all(abs(g - median_gap) <= _INTERVAL_TOLERANCE_DAYS for g in gaps)
        if not regular:
            continue

        amounts = [e.amount for e in members_sorted if e.amount is not None]
        if not amounts:
            continue
        typical_amount = sorted(amounts)[len(amounts) // 2]

        min_allowed_values = {e.minimum_allowed_amount for e in members_sorted if e.minimum_allowed_amount is not None}
        min_allowed = next(iter(min_allowed_values)) if len(min_allowed_values) == 1 else None

        series_list.append(
            RecurringSeries(
                user_id=members_sorted[0].user_id,
                category=category,
                direction=direction,
                event_ids=tuple(e.event_id for e in members_sorted),
                typical_amount=typical_amount,
                interval_days=median_gap,
                flexibility=flexibility,
                minimum_allowed_amount=min_allowed,
                is_protected=False,  # filled in by build_financial_state (needs profile)
                is_reducible=flexibility in ("reducible", "reducible_or_stoppable"),
                is_stoppable=flexibility in ("stoppable", "reducible_or_stoppable"),
                last_event_date=members_sorted[-1].event_date,
            )
        )
    return series_list


# --------------------------------------------------------------------------
# FX gap reporting (informational only -- no conversion performed)
# --------------------------------------------------------------------------


def _fx_gap_for_event(
    ds: loaders.Dataset, event: loaders.FinancialEvent, home_currency: str
) -> Optional[FxGap]:
    if event.currency == home_currency:
        return None  # no conversion need at all
    rate_date = event.settlement_date or event.event_date
    direct = loaders.get_exchange_rate(ds, rate_date, event.currency, home_currency) is not None

    single_hop = False
    chain_path = None
    if not direct:
        for (rd, frm, to), _rate in ds.exchange_rate_index.items():
            if rd != rate_date or frm != event.currency:
                continue
            second = ds.exchange_rate_index.get((rate_date, to, home_currency))
            if second is not None:
                single_hop = True
                chain_path = (event.currency, to, home_currency)
                break

    return FxGap(
        event_id=event.event_id,
        user_id=event.user_id,
        event_currency=event.currency,
        home_currency=home_currency,
        rate_date=rate_date,
        direct_rate_available=direct,
        single_hop_chain_available=single_hop,
        chain_path=chain_path,
    )


# --------------------------------------------------------------------------
# Top-level per-user assembly
# --------------------------------------------------------------------------


def build_financial_state(ds: loaders.Dataset, user_id: str) -> Tuple[FinancialState, List[FxGap]]:
    profile = ds.profiles_by_user.get(user_id)
    if profile is None:
        raise loaders.DataIntegrityError(f"No financial_profiles.csv row for user_id={user_id!r}")

    raw_events = ds.events_by_user.get(user_id, [])
    raw_messages = ds.messages_by_user.get(user_id, [])

    evidences = [message_evidence_mod.parse_message(m) for m in raw_messages]
    message_evidence_by_event: Dict[str, List[MessageEvidence]] = {}
    for ev in evidences:
        if ev.related_event_id:
            message_evidence_by_event.setdefault(ev.related_event_id, []).append(ev)

    normalized = [normalize_event(ds, e, message_evidence_by_event) for e in raw_events]

    recurring_series = _detect_recurring_series(normalized)
    recurring_event_ids = {eid for series in recurring_series for eid in series.event_ids}

    recurring_expenses = [s for s in recurring_series if s.direction == "debit"]
    recurring_income = [s for s in recurring_series if s.direction == "credit"]

    # Re-tag classification + is_protected now that we know which events
    # are part of a recurring series and what the profile's protected
    # list says.
    protected = set(profile.expense_categories_to_protect)
    recurring_expenses = [replace(s, is_protected=(s.category in protected)) for s in recurring_expenses]

    final_normalized: List[NormalizedEvent] = []
    one_time_events: List[NormalizedEvent] = []
    confirmed_future_income: List[NormalizedEvent] = []

    for e in normalized:
        if e.event_id in recurring_event_ids:
            classification = CLASS_RECURRING_EXPENSE if e.direction == "debit" else CLASS_RECURRING_INCOME
        elif e.status == "scheduled" and e.direction == "credit":
            classification = CLASS_CONFIRMED_FUTURE_INCOME
        elif not e.counts_as_cash_flow:
            classification = CLASS_NON_CASH_FLOW
        else:
            classification = CLASS_ONE_TIME
        e2 = replace(e, classification=classification)
        final_normalized.append(e2)
        if classification == CLASS_CONFIRMED_FUTURE_INCOME:
            confirmed_future_income.append(e2)
        elif classification == CLASS_ONE_TIME:
            one_time_events.append(e2)

    fx_gaps = [
        gap
        for e in raw_events
        if (gap := _fx_gap_for_event(ds, e, profile.home_currency)) is not None
    ]

    # Reconcile each message's own (per-message-only) 'applied' guess
    # against what reconstruction actually used. A message evidence object
    # doesn't know about sibling messages for the same event_id -- if two
    # messages each named a plausible single amount for the same event but
    # they disagreed, normalize_event() correctly left the event
    # unresolved rather than guessing, so neither message should still
    # claim applied=True.
    actually_used_event_ids = {
        e.event_id for e in final_normalized if e.amount_source == "message"
    }
    reconciled_evidence: List[MessageEvidence] = []
    for ev in evidences:
        if ev.applied and ev.related_event_id not in actually_used_event_ids:
            reconciled_evidence.append(
                replace(
                    ev,
                    applied=False,
                    application_note=ev.application_note
                    + "; NOT applied: reconstruction found conflicting evidence for this event "
                    "from another message and left the amount unresolved rather than guess",
                )
            )
        else:
            reconciled_evidence.append(ev)
    evidences = reconciled_evidence

    state = FinancialState(
        user_id=profile.user_id,
        home_currency=profile.home_currency,
        current_available_balance=profile.current_available_balance,
        minimum_balance_to_keep=profile.minimum_balance_to_keep,
        expense_categories_to_protect=profile.expense_categories_to_protect,
        expense_categories_user_is_willing_to_reduce=profile.expense_categories_user_is_willing_to_reduce,
        expense_categories_user_is_willing_to_stop=profile.expense_categories_user_is_willing_to_stop,
        payment_methods_user_will_consider=profile.payment_methods_user_will_consider,
        max_installment_months=profile.max_installment_months,
        normalized_events=final_normalized,
        recurring_expenses=recurring_expenses,
        recurring_income=recurring_income,
        confirmed_future_income_events=confirmed_future_income,
        one_time_events=one_time_events,
        message_evidence=evidences,
    )
    return state, fx_gaps


def build_all_financial_states(ds: loaders.Dataset) -> Tuple[Dict[str, FinancialState], List[FxGap]]:
    states: Dict[str, FinancialState] = {}
    all_gaps: List[FxGap] = []
    for user_id in ds.profiles_by_user:
        state, gaps = build_financial_state(ds, user_id)
        states[user_id] = state
        all_gaps.extend(gaps)
    return states, all_gaps
