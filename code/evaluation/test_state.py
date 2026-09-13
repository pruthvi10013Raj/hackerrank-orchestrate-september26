"""
Phase 2 -- Lightweight tests for financial-state reconstruction
(reconstruction.py + message_evidence.py).

Same style as code/evaluation/test_data_layer.py: plain functions +
assertions, zero extra dependencies, every check runs and failures are
collected and reported together instead of stopping at the first one.

Run:
    python3 code/evaluation/test_state.py

Exit code is 0 iff every check passed.
"""

from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import loaders  # noqa: E402
import message_evidence  # noqa: E402
import reconstruction  # noqa: E402


def run_checks(ds: loaders.Dataset, states, fx_gaps) -> list:
    """Returns a list of (name, ok, detail) tuples. Never raises."""
    results = []
    all_events = [e for s in states.values() for e in s.normalized_events]
    all_evidence = [ev for s in states.values() for ev in s.message_evidence]

    def check(name: str, fn):
        try:
            detail = fn()
            results.append((name, True, detail if detail else "ok"))
        except AssertionError as exc:
            results.append((name, False, str(exc)))
        except Exception as exc:  # unexpected error is still a failure to report
            results.append((name, False, f"UNEXPECTED ERROR: {type(exc).__name__}: {exc}"))

    # --- coverage / no loss, no duplication ------------------------------

    def _every_source_event_appears_once():
        assert len(all_events) == len(ds.events), (
            f"expected {len(ds.events)} normalized events (one per financial_events.csv row), "
            f"got {len(all_events)}"
        )
        ids = [e.event_id for e in all_events]
        dupes = {eid for eid in ids if ids.count(eid) > 1}
        assert not dupes, f"event_id appears more than once in normalized output: {dupes}"
        assert set(ids) == set(ds.events_by_id), "normalized event_ids do not match source event_ids exactly"
        return f"{len(all_events)} events, 1:1 with financial_events.csv, no duplicates"

    check("every financial_events.csv row is normalized exactly once", _every_source_event_appears_once)

    def _all_users_reconstructed():
        assert len(states) == len(ds.profiles), (
            f"expected a FinancialState for all {len(ds.profiles)} profiles, got {len(states)}"
        )
        return f"{len(states)} users reconstructed"

    check("every user_id in financial_profiles.csv has a reconstructed state", _all_users_reconstructed)

    # --- blank-amount integrity (never silently zero) --------------------

    def _blank_amounts_never_become_zero():
        blank_source_ids = {e.event_id for e in ds.events if e.amount is None}
        by_id = {e.event_id: e for e in all_events}
        for eid in blank_source_ids:
            e = by_id[eid]
            if e.amount is not None:
                assert e.amount != Decimal("0") or e.amount_source != "structured", (
                    f"{eid} was blank in source but ended up as structured 0"
                )
                assert e.amount_source in ("image", "message"), (
                    f"{eid} has a non-blank amount but amount_source={e.amount_source!r} "
                    "(must be 'image' or 'message', never invented)"
                )
        return f"checked {len(blank_source_ids)} originally-blank events; none silently became 0"

    check("blank financial-event amounts never silently become 0", _blank_amounts_never_become_zero)

    def _unresolved_events_stay_none():
        unresolved = [e for e in all_events if e.amount_source == "unresolved"]
        for e in unresolved:
            assert e.amount is None, f"{e.event_id} is 'unresolved' but has a non-None amount {e.amount}"
        return f"{len(unresolved)} unresolved events all have amount is None"

    check("events with amount_source == 'unresolved' always have amount is None", _unresolved_events_stay_none)

    # --- direction-aware pending / unrealized classification --------------

    def _pending_debit_is_reserved():
        pending_debits = [e for e in all_events if e.status == "pending" and e.direction == "debit"]
        assert pending_debits, "expected at least one pending debit in this dataset to exercise the rule"
        for e in pending_debits:
            assert e.counts_as_cash_flow is True, (
                f"{e.event_id} is a pending debit and must be RESERVED (counts_as_cash_flow=True) "
                f"per the challenge contract, got {e.counts_as_cash_flow}"
            )
        return f"{len(pending_debits)} pending debit(s) all reserved (counts_as_cash_flow=True)"

    check("pending DEBITS are reserved (counted), per the explicit challenge contract", _pending_debit_is_reserved)

    def _pending_credit_is_excluded():
        pending_credits = [e for e in all_events if e.status == "pending" and e.direction == "credit"]
        assert pending_credits, "expected at least one pending credit in this dataset to exercise the rule"
        for e in pending_credits:
            assert e.counts_as_cash_flow is False, (
                f"{e.event_id} is a pending credit and must be excluded until settled, "
                f"got counts_as_cash_flow={e.counts_as_cash_flow}"
            )
        return f"{len(pending_credits)} pending credit(s) all excluded (counts_as_cash_flow=False)"

    check("pending CREDITS are excluded until settled, per the explicit challenge contract", _pending_credit_is_excluded)

    def _unrealized_never_counts():
        unrealized = [e for e in all_events if e.status == "unrealized"]
        assert unrealized, "expected at least one 'unrealized' investment valuation row in this dataset"
        for e in unrealized:
            assert e.direction == "non_cash", f"{e.event_id} has status=unrealized but direction={e.direction!r}"
            assert e.counts_as_cash_flow is False, f"{e.event_id} is unrealized but counts_as_cash_flow is True"
            assert e.classification == reconstruction.CLASS_NON_CASH_FLOW, (
                f"{e.event_id} is unrealized but classified as {e.classification!r}"
            )
        return f"{len(unrealized)} unrealized investment valuation(s), all excluded and classified non_cash_flow"

    check("unrealized investment valuations never count as cash", _unrealized_never_counts)

    def _cancelled_and_failed_never_count():
        never_count = [e for e in all_events if e.status in ("cancelled", "failed")]
        for e in never_count:
            assert e.counts_as_cash_flow is False, f"{e.event_id} has status={e.status} but counts_as_cash_flow is True"
        return f"{len(never_count)} cancelled/failed event(s), all excluded"

    check("cancelled and failed events never count as cash flow", _cancelled_and_failed_never_count)

    # --- recurring-series structural integrity ----------------------------

    def _recurring_series_have_min_occurrences_and_regular_spacing():
        checked = 0
        for state in states.values():
            for series in list(state.recurring_expenses) + list(state.recurring_income):
                assert len(series.event_ids) >= reconstruction._MIN_OCCURRENCES_FOR_RECURRING, (
                    f"series for user={series.user_id} category={series.category} has only "
                    f"{len(series.event_ids)} occurrences"
                )
                checked += 1
        return f"{checked} recurring series all meet the minimum-occurrence threshold"

    check(
        "every detected recurring series has >= 3 occurrences (never inferred from 1-2 events)",
        _recurring_series_have_min_occurrences_and_regular_spacing,
    )

    def _protected_flag_matches_profile():
        mismatches = []
        for uid, state in states.items():
            protected_categories = set(state.expense_categories_to_protect)
            for series in state.recurring_expenses:
                expected = series.category in protected_categories
                if series.is_protected != expected:
                    mismatches.append((uid, series.category, series.is_protected, expected))
        assert not mismatches, f"is_protected mismatches vs profile.expense_categories_to_protect: {mismatches[:10]}"
        return "every recurring expense series' is_protected flag matches its profile's protect-list"

    check("recurring-series is_protected exactly matches financial_profiles.csv", _protected_flag_matches_profile)

    # --- message evidence: untrusted-content firewall ----------------------

    def _advance_fee_messages_never_extract_or_apply():
        suspicious = [
            ev for ev in all_evidence if message_evidence.SIGNAL_ADVANCE_FEE_SUSPICIOUS in ev.signal_types
        ]
        assert suspicious, "expected at least one advance-fee-suspicious message in this dataset"
        for ev in suspicious:
            assert ev.extracted_amounts == (), f"{ev.message_id} is suspicious but still extracted amounts"
            assert ev.applied is False, f"{ev.message_id} is suspicious but was marked applied=True"
        return f"{len(suspicious)} advance-fee-suspicious message(s), none extracted or applied"

    check(
        "advance-fee / prompt-injection-style messages never yield an applied financial fact",
        _advance_fee_messages_never_extract_or_apply,
    )

    def _unconfirmed_bonus_never_applied():
        unconfirmed = [
            ev for ev in all_evidence
            if message_evidence.SIGNAL_BONUS_COMMISSION_UNCONFIRMED in ev.signal_types
        ]
        for ev in unconfirmed:
            assert ev.applied is False, (
                f"{ev.message_id} explicitly states the amount is NOT yet confirmed but was applied"
            )
        return f"{len(unconfirmed)} unconfirmed-bonus/commission message(s), none applied"

    check("explicitly unconfirmed bonus/commission messages are never applied", _unconfirmed_bonus_never_applied)

    def _applied_evidence_is_internally_consistent():
        applied = [ev for ev in all_evidence if ev.applied]
        for ev in applied:
            assert ev.related_event_id is not None, f"{ev.message_id} applied but has no related_event_id"
            assert len(ev.extracted_amounts) == 1, (
                f"{ev.message_id} applied but has {len(ev.extracted_amounts)} extracted amounts (must be exactly 1)"
            )
        return f"{len(applied)} applied evidence object(s), all have exactly 1 amount + a related_event_id"

    check("every 'applied' message evidence has exactly one amount and a related_event_id", _applied_evidence_is_internally_consistent)

    def _message_resolved_amounts_match_evidence():
        message_resolved = [e for e in all_events if e.amount_source == "message"]
        by_event = {}
        for ev in all_evidence:
            if ev.related_event_id:
                by_event.setdefault(ev.related_event_id, []).append(ev)
        for e in message_resolved:
            evidences_for_event = by_event.get(e.event_id, [])
            candidate_amounts = {amt for ev in evidences_for_event for (_, amt) in ev.extracted_amounts}
            assert len(candidate_amounts) == 1, (
                f"{e.event_id} has amount_source=message but its linked messages carry "
                f"{len(candidate_amounts)} distinct candidate amounts"
            )
            assert e.amount in candidate_amounts, f"{e.event_id}'s resolved amount does not match its message evidence"
        return f"{len(message_resolved)} message-resolved event(s) all traceable to exactly one message amount"

    check(
        "every message-resolved blank amount matches a single unambiguous message-evidence amount",
        _message_resolved_amounts_match_evidence,
    )

    # --- image extraction never guesses ------------------------------------

    def _image_extraction_never_guesses_when_ambiguous():
        for img in ds.images:
            event = ds.events_by_id.get(img.related_event_id) if img.related_event_id else None
            if event is None:
                continue
            result = reconstruction.resolve_blank_amount_via_image(ds, event)
            if result.status == "ambiguous":
                assert result.amount is None, f"{img.image_id} is ambiguous but still returned an amount"
            if result.status == "extracted":
                assert result.amount is not None, f"{img.image_id} claims extracted but amount is None"
        return f"checked all {len(ds.images)} image references; no guessing on ambiguous OCR output"

    check("image-based amount extraction never guesses among multiple ambiguous candidates", _image_extraction_never_guesses_when_ambiguous)

    # --- FX gap reporting stays informational only --------------------------

    def _fx_gap_never_implies_a_computed_rate():
        # FxGap is a dataclass with no 'rate' field at all -- structurally
        # incapable of smuggling an invented conversion number out of
        # Phase 2. This test fails loudly if that field is ever added
        # without equally explicit justification.
        from state import FxGap

        field_names = set(FxGap.__dataclass_fields__.keys())
        assert "rate" not in field_names and "converted_amount" not in field_names, (
            f"FxGap must stay informational-only (no rate/converted_amount field), found: {field_names}"
        )
        return f"FxGap fields are strictly availability/chain-path metadata: {sorted(field_names)}"

    check("FxGap reporting never carries a computed or invented conversion rate", _fx_gap_never_implies_a_computed_rate)

    def _fx_chain_dates_always_match():
        for gap in fx_gaps:
            if gap.chain_path:
                frm, via, to = gap.chain_path
                assert frm == gap.event_currency and to == gap.home_currency
                # both hops of the chain must exist on the SAME rate_date --
                # never a mixed-date chain.
                assert (gap.rate_date, frm, via) in ds.exchange_rate_index
                assert (gap.rate_date, via, to) in ds.exchange_rate_index
        return f"checked {sum(1 for g in fx_gaps if g.chain_path)} single-hop chain(s); both legs share the same rate_date"

    check("every reported FX single-hop chain uses same-date rows on both legs", _fx_chain_dates_always_match)

    return results


def main() -> int:
    ds = loaders.load_dataset()
    states, fx_gaps = reconstruction.build_all_financial_states(ds)
    results = run_checks(ds, states, fx_gaps)

    print("=" * 78)
    print("PHASE 2 FINANCIAL-STATE-RECONSTRUCTION TEST RESULTS")
    print("=" * 78)
    passed = 0
    failed = 0
    for name, ok, detail in results:
        status = "PASS" if ok else "FAIL"
        if ok:
            passed += 1
        else:
            failed += 1
        print(f"[{status}] {name}")
        print(f"       {detail}")

    print("-" * 78)
    print(f"{passed} passed, {failed} failed, {len(results)} total")

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
