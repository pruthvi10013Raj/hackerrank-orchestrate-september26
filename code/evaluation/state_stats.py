"""
Phase 2 -- Exact, deterministic financial-state-reconstruction statistics.

Runs reconstruction.build_all_financial_states() over every profile in
the COMPLETE local dataset (all 275 users / 25,342 financial events) and
prints exact diagnostics. Nothing here is sampled, estimated, or
truncated, and nothing here makes a payment recommendation -- this is a
reporting script only, mirroring code/evaluation/data_stats.py from
Phase 1 but one layer up the pipeline.

Run:
    python3 code/evaluation/state_stats.py
"""

from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import loaders  # noqa: E402
import reconstruction  # noqa: E402


def _print_counter(title: str, counter: Counter, total: int) -> None:
    print(f"\n{title} (n={total}):")
    for key, count in sorted(counter.items(), key=lambda kv: (-kv[1], str(kv[0]))):
        pct = (count / total * 100) if total else 0.0
        print(f"  {key!r:<45} {count:>7}  ({pct:5.1f}%)")


def main() -> int:
    ds = loaders.load_dataset()
    states, fx_gaps = reconstruction.build_all_financial_states(ds)

    all_events = [e for state in states.values() for e in state.normalized_events]
    total_events = len(all_events)

    print("=" * 78)
    print("PHASE 2 -- FINANCIAL STATE RECONSTRUCTION STATISTICS")
    print("=" * 78)
    print(f"Users with a reconstructed financial state : {len(states)}")
    print(f"Total normalized events across all users    : {total_events}")
    print(f"(source financial_events.csv row count      : {len(ds.events)})")
    if total_events != len(ds.events):
        print("  *** MISMATCH -- every source event should appear exactly once ***")

    # ---------------------------------------------------------------
    # 1. Classification counts
    # ---------------------------------------------------------------
    classification_counter = Counter(e.classification for e in all_events)
    _print_counter("CLASSIFICATION COUNTS", classification_counter, total_events)

    counts_as_cash_flow_counter = Counter(e.counts_as_cash_flow for e in all_events)
    _print_counter("counts_as_cash_flow (True/False)", counts_as_cash_flow_counter, total_events)

    # cross-tab: status x direction x counts_as_cash_flow, to make the
    # direction-aware pending/unrealized rule auditable at a glance
    print("\nSTATUS x DIRECTION -> counts_as_cash_flow (exact rows actually observed):")
    crosstab: Counter = Counter()
    for e in all_events:
        crosstab[(e.status, e.direction, e.counts_as_cash_flow)] += 1
    for (status, direction, counts), n in sorted(crosstab.items(), key=lambda kv: (-kv[1], kv[0])):
        print(f"  status={status:<10} direction={direction:<8} -> counts_as_cash_flow={counts!s:<5}  n={n}")

    # ---------------------------------------------------------------
    # 2. Recurring series counts
    # ---------------------------------------------------------------
    print("\n" + "=" * 78)
    print("RECURRING SERIES")
    print("=" * 78)
    total_recurring_expense_series = sum(len(s.recurring_expenses) for s in states.values())
    total_recurring_income_series = sum(len(s.recurring_income) for s in states.values())
    print(f"Recurring EXPENSE series detected (debit)  : {total_recurring_expense_series}")
    print(f"Recurring INCOME series detected (credit)  : {total_recurring_income_series}")

    users_with_no_recurring_expense = sum(1 for s in states.values() if not s.recurring_expenses)
    print(f"Users with ZERO recurring expense series    : {users_with_no_recurring_expense} / {len(states)}")

    protected_series = sum(
        1 for s in states.values() for r in s.recurring_expenses if r.is_protected
    )
    reducible_series = sum(
        1 for s in states.values() for r in s.recurring_expenses if r.is_reducible
    )
    stoppable_series = sum(
        1 for s in states.values() for r in s.recurring_expenses if r.is_stoppable
    )
    print(f"  of which protected   : {protected_series}")
    print(f"  of which reducible   : {reducible_series}")
    print(f"  of which stoppable   : {stoppable_series}")

    flexibility_counter: Counter = Counter()
    for s in states.values():
        for r in s.recurring_expenses:
            flexibility_counter[r.flexibility] += 1
    _print_counter(
        "Recurring expense series by flexibility value",
        flexibility_counter,
        total_recurring_expense_series,
    )

    interval_counter: Counter = Counter()
    for s in states.values():
        for r in (list(s.recurring_expenses) + list(s.recurring_income)):
            interval_counter[r.interval_days] += 1
    _print_counter(
        "Detected recurring interval_days (all series)",
        interval_counter,
        total_recurring_expense_series + total_recurring_income_series,
    )

    # ---------------------------------------------------------------
    # 3. Confirmed future income
    # ---------------------------------------------------------------
    print("\n" + "=" * 78)
    print("CONFIRMED FUTURE INCOME")
    print("=" * 78)
    total_confirmed_future_income = sum(len(s.confirmed_future_income_events) for s in states.values())
    print(f"Events classified confirmed_future_income  : {total_confirmed_future_income}")
    users_with_confirmed_future_income = sum(
        1 for s in states.values() if s.confirmed_future_income_events
    )
    print(f"Users with at least one confirmed future income event: {users_with_confirmed_future_income} / {len(states)}")
    ccy_counter: Counter = Counter()
    for s in states.values():
        for e in s.confirmed_future_income_events:
            ccy_counter[e.currency] += 1
    _print_counter("Confirmed future income by event currency", ccy_counter, total_confirmed_future_income)

    # ---------------------------------------------------------------
    # 4. Blank amount resolution
    # ---------------------------------------------------------------
    print("\n" + "=" * 78)
    print("BLANK AMOUNT RESOLUTION")
    print("=" * 78)
    source_counter = Counter(e.amount_source for e in all_events)
    _print_counter("amount_source distribution (ALL events)", source_counter, total_events)

    blank_events = [e for e in all_events if e.amount is None]
    total_blank_in_source = sum(1 for e in ds.events if e.amount is None)
    print(f"\nSource rows with blank amount in financial_events.csv : {total_blank_in_source}")
    print(f"Normalized events still unresolved (amount is None)   : {len(blank_events)}")
    resolved_via_image = sum(1 for e in all_events if e.amount_source == "image")
    resolved_via_message = sum(1 for e in all_events if e.amount_source == "message")
    print(f"  resolved via image OCR    : {resolved_via_image}")
    print(f"  resolved via message      : {resolved_via_message}")
    print(f"  still unresolved          : {len(blank_events)}")
    if resolved_via_image + resolved_via_message + len(blank_events) != total_blank_in_source:
        print("  *** MISMATCH between blank-source count and resolution breakdown ***")
    if blank_events:
        print("  Unresolved event_ids (Phase 3 must decide a conservative fallback):")
        for e in blank_events:
            print(f"    {e.event_id} (user={e.user_id}, category={e.category}, status={e.status})")

    # ---------------------------------------------------------------
    # 5. Image extraction results
    # ---------------------------------------------------------------
    print("\n" + "=" * 78)
    print("IMAGE EXTRACTION RESULTS (all 16 rows in images.csv)")
    print("=" * 78)
    events_by_id = ds.events_by_id
    for img in ds.images:
        event = events_by_id.get(img.related_event_id) if img.related_event_id else None
        if event is None:
            print(f"  {img.image_id}: related_event_id={img.related_event_id!r} does not resolve to a known event")
            continue
        result = reconstruction.resolve_blank_amount_via_image(ds, event)
        print(
            f"  {img.image_id} -> {event.event_id} (user={event.user_id}, category={event.category}): "
            f"status={result.status}, amount={result.amount}, method={result.method}"
        )
        print(f"      detail: {result.detail}")

    # ---------------------------------------------------------------
    # 6. Message evidence / signals
    # ---------------------------------------------------------------
    print("\n" + "=" * 78)
    print("MESSAGE EVIDENCE / SIGNALS")
    print("=" * 78)
    all_evidence = [ev for s in states.values() for ev in s.message_evidence]
    print(f"Total message evidence objects (== len(ds.messages)) : {len(all_evidence)} / {len(ds.messages)}")

    signal_counter: Counter = Counter()
    for ev in all_evidence:
        for sig in ev.signal_types:
            signal_counter[sig] += 1
    _print_counter("Signal type frequency (a message may carry >1 signal)", signal_counter, len(all_evidence))

    applied_count = sum(1 for ev in all_evidence if ev.applied)
    print(f"\nMessage evidences actually APPLIED (resolved a blank amount): {applied_count}")
    for ev in all_evidence:
        if ev.applied:
            print(f"    {ev.message_id} -> related_event_id={ev.related_event_id}: {ev.application_note}")

    unclassified_count = sum(1 for ev in all_evidence if ev.signal_types == ("unclassified",))
    print(f"\nMessages that matched NO recognized template (unclassified): {unclassified_count}")

    with_event = sum(1 for ev in all_evidence if ev.related_event_id is not None)
    print(f"Messages with a related_event_id       : {with_event}")
    print(f"Messages without a related_event_id     : {len(all_evidence) - with_event}")

    suspicious = [ev for ev in all_evidence if "advance_fee_suspicious_pattern" in ev.signal_types]
    print(f"\nMessages matching the advance-fee-suspicious pattern (never actioned): {len(suspicious)}")
    for ev in suspicious:
        print(f"    {ev.message_id} (user={ev.user_id})")

    # ---------------------------------------------------------------
    # 7. Linked-event lifecycle patterns
    # ---------------------------------------------------------------
    print("\n" + "=" * 78)
    print("LINKED-EVENT LIFECYCLE PATTERNS")
    print("=" * 78)
    linked_children = [e for e in all_events if e.linked_event_id]
    print(f"Events with a non-blank linked_event_id : {len(linked_children)}")

    events_by_id_norm = {e.event_id: e for e in all_events}
    pair_pattern_counter: Counter = Counter()
    for child in linked_children:
        parent = events_by_id_norm.get(child.linked_event_id)
        if parent is None:
            pair_pattern_counter[("missing_parent", child.status, child.direction)] += 1
            continue
        pair_pattern_counter[
            (f"parent:{parent.status}/{parent.direction}", f"child:{child.status}/{child.direction}", child.category)
        ] += 1
    print("\nObserved (parent, child, category) lifecycle shapes:")
    for key, n in sorted(pair_pattern_counter.items(), key=lambda kv: -kv[1]):
        print(f"  {key} -> n={n}")

    # ---------------------------------------------------------------
    # 8. Duplicate / financial-effect exclusions
    # ---------------------------------------------------------------
    print("\n" + "=" * 78)
    print("DUPLICATE / FINANCIAL-EFFECT EXCLUSIONS")
    print("=" * 78)
    excluded_as_duplicate = sum(1 for e in all_events if e.excluded_as_duplicate)
    print(f"Events flagged excluded_as_duplicate : {excluded_as_duplicate}")
    print(
        "Note: this dataset's linked_event_id pairs (refund<-purchase, "
        "investment-valuation chains, possible-duplicate-charge<-original) are already "
        "resolved correctly by STATUS alone (pending/unrealized rows never count; settled "
        "rows are genuinely separate cash movements), so no row required a separate "
        "duplicate-exclusion flag in this dataset. This flag exists in the data model for "
        "Phase 3 to use if a future scenario needs it."
    )
    non_cash_flow_events = sum(1 for e in all_events if e.classification == reconstruction.CLASS_NON_CASH_FLOW)
    print(f"Events classified non_cash_flow (cancelled/failed/pending-credit/unrealized): {non_cash_flow_events}")

    # ---------------------------------------------------------------
    # 9. FX gaps
    # ---------------------------------------------------------------
    print("\n" + "=" * 78)
    print("FX GAP REPORT (informational only -- no conversion performed in Phase 2)")
    print("=" * 78)
    print(f"Events needing conversion (event.currency != user.home_currency): {len(fx_gaps)}")
    direct_available = sum(1 for g in fx_gaps if g.direct_rate_available)
    single_hop_only = sum(1 for g in fx_gaps if not g.direct_rate_available and g.single_hop_chain_available)
    no_path = sum(1 for g in fx_gaps if not g.direct_rate_available and not g.single_hop_chain_available)
    print(f"  direct rate available on settlement/event date : {direct_available}")
    print(f"  no direct rate, but single-hop chain available  : {single_hop_only}")
    print(f"  NO conversion path available at all              : {no_path}")
    if single_hop_only:
        print("\n  Single-hop chains actually used by the data (currency -> intermediate -> home):")
        chain_counter: Counter = Counter()
        for g in fx_gaps:
            if g.chain_path:
                chain_counter[g.chain_path] += 1
        for chain, n in sorted(chain_counter.items(), key=lambda kv: -kv[1]):
            print(f"    {chain[0]} -> {chain[1]} -> {chain[2]}  (n={n})")
    if no_path:
        print("\n  Currency pairs with NO available conversion path at all (Phase 3 gap):")
        gap_pair_counter: Counter = Counter()
        for g in fx_gaps:
            if not g.direct_rate_available and not g.single_hop_chain_available:
                gap_pair_counter[(g.event_currency, g.home_currency)] += 1
        for pair, n in sorted(gap_pair_counter.items(), key=lambda kv: -kv[1]):
            print(f"    {pair[0]} -> {pair[1]}  (n={n})")

    # ---------------------------------------------------------------
    # 10. Unresolved cases (summary)
    # ---------------------------------------------------------------
    print("\n" + "=" * 78)
    print("UNRESOLVED CASES SUMMARY")
    print("=" * 78)
    print(f"Blank-amount events still unresolved after image + message resolution : {len(blank_events)}")
    print(f"Events with NO fx conversion path available (event currency != home)  : {no_path}")
    print(
        "These are the two categories of genuine Phase-2 gaps that Phase 3 must "
        "explicitly decide how to treat (never silently default to 0 or an invented rate)."
    )

    print("\nDone. This script performs no financial decisioning -- it only reports facts.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
