"""
Phase 1 — Lightweight tests for the deterministic data-access layer.

No third-party test framework: plain functions + assertions, so this has
zero extra dependencies. Every check runs; failures are collected and
reported together instead of stopping at the first one.

Run:
    python3 code/evaluation/test_data_layer.py

Exit code is 0 iff every check passed.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import loaders  # noqa: E402


class CheckFailure(AssertionError):
    pass


def run_checks(ds: loaders.Dataset) -> list:
    """Returns a list of (name, ok, detail) tuples. Never raises."""
    results = []

    def check(name: str, fn):
        try:
            detail = fn()
            results.append((name, True, detail if detail else "ok"))
        except AssertionError as exc:
            results.append((name, False, str(exc)))
        except Exception as exc:  # unexpected error is still a failure to report
            results.append((name, False, f"UNEXPECTED ERROR: {type(exc).__name__}: {exc}"))

    # --- volume checks -----------------------------------------------

    def _eval_requests_count():
        assert len(ds.requests) == 250, (
            f"expected 250 evaluation requests in requests.csv, found {len(ds.requests)}"
        )
        return f"{len(ds.requests)} rows"

    check("250 evaluation requests load from requests.csv", _eval_requests_count)

    def _profiles_count():
        assert len(ds.profiles) == 275, (
            f"expected 275 profiles in financial_profiles.csv, found {len(ds.profiles)}"
        )
        return f"{len(ds.profiles)} rows"

    check("275 profiles load from financial_profiles.csv", _profiles_count)

    # --- uniqueness checks ---------------------------------------------

    def _request_ids_unique():
        ids = [r.request_id for r in ds.requests]
        dupes = {rid for rid in ids if ids.count(rid) > 1}
        assert not dupes, f"duplicate request_id values: {dupes}"
        return f"{len(set(ids))} unique ids"

    check("request_id values in requests.csv are unique", _request_ids_unique)

    def _profile_user_ids_unique():
        ids = [p.user_id for p in ds.profiles]
        dupes = {uid for uid in ids if ids.count(uid) > 1}
        assert not dupes, f"duplicate user_id values: {dupes}"
        return f"{len(set(ids))} unique ids"

    check("user_id values in financial_profiles.csv are unique", _profile_user_ids_unique)

    def _event_ids_unique():
        ids = [e.event_id for e in ds.events]
        seen = set()
        dupes = set()
        for eid in ids:
            if eid in seen:
                dupes.add(eid)
            seen.add(eid)
        assert not dupes, f"duplicate event_id values (showing up to 10): {list(dupes)[:10]}"
        return f"{len(seen)} unique ids across {len(ids)} rows"

    check("event_id values in financial_events.csv are unique", _event_ids_unique)

    # --- referential integrity -----------------------------------------

    def _requests_resolve_to_profile():
        missing = [r.request_id for r in ds.requests if r.user_id not in ds.profiles_by_user]
        assert not missing, f"requests whose user_id has no profile (showing up to 10): {missing[:10]}"
        return f"all {len(ds.requests)} requests resolve to a profile"

    check("every request.user_id resolves to a financial profile", _requests_resolve_to_profile)

    def _payment_options_resolve_by_request():
        all_ids = ds.all_request_ids()
        missing = sorted({p.request_id for p in ds.payment_options if p.request_id not in all_ids})
        assert not missing, (
            f"payment options referencing unknown request_id "
            f"(checked against requests.csv + sample_requests.csv union, showing up to 10): {missing[:10]}"
        )
        return f"all {len(ds.payment_options)} payment options resolve to a known request_id"

    check(
        "every payment option's request_id resolves (requests.csv ∪ sample_requests.csv)",
        _payment_options_resolve_by_request,
    )

    def _events_resolve_to_profile():
        missing = sorted({e.user_id for e in ds.events if e.user_id not in ds.profiles_by_user})
        assert not missing, f"events referencing unknown user_id (showing up to 10): {missing[:10]}"
        return f"all {len(ds.events)} events resolve to a known user_id"

    check("every financial event's user_id resolves to a financial profile", _events_resolve_to_profile)

    def _messages_resolve_by_request():
        all_ids = ds.all_request_ids()
        with_request = [m for m in ds.messages if m.request_id is not None]
        missing = sorted({m.request_id for m in with_request if m.request_id not in all_ids})
        assert not missing, f"messages referencing unknown request_id: {missing[:10]}"
        return f"all {len(with_request)} request-linked messages resolve"

    check(
        "every message's non-blank request_id resolves (requests.csv ∪ sample_requests.csv)",
        _messages_resolve_by_request,
    )

    # --- image checks ----------------------------------------------------

    def _images_point_to_existing_files():
        missing = [(i.image_id, str(i.file_path)) for i in ds.images if not i.file_path.exists()]
        assert not missing, f"image rows whose PNG file is missing on disk: {missing}"
        return f"all {len(ds.images)} image references point to existing files"

    check("every images.csv row points to an existing PNG under dataset/media/images/", _images_point_to_existing_files)

    def _images_related_event_resolves():
        with_related = [i for i in ds.images if i.related_event_id is not None]
        missing = [i.image_id for i in with_related if i.related_event_id not in ds.events_by_id]
        assert not missing, f"images whose related_event_id has no matching event: {missing}"
        return f"all {len(with_related)} image->event links resolve"

    check("every image's non-blank related_event_id resolves to a known event_id", _images_related_event_resolves)

    # --- blank-value integrity (the core Phase 1 rule) --------------------

    def _blank_amounts_stay_none():
        # Directly re-parse a controlled case: optional_decimal on an empty
        # string must be None, never Decimal('0').
        result = loaders.optional_decimal("", field_name="amount", context="unit-test")
        assert result is None, f"expected None for blank amount, got {result!r}"
        result_ws = loaders.optional_decimal("   ", field_name="amount", context="unit-test")
        assert result_ws is None, f"expected None for whitespace-only amount, got {result_ws!r}"
        # And confirm it's not just the unit test: check the loaded events
        # object actually contains at least one such None (proves the CSV
        # column really does have blanks in this dataset, and they survived
        # as None all the way through the loader).
        blank_events = [e for e in ds.events if e.amount is None]
        assert blank_events, "expected at least one financial event with a blank amount in the dataset"
        return f"optional_decimal('') -> None; {len(blank_events)} real blank-amount events preserved as None"

    check("blank financial-event amounts remain None (never coerced to 0)", _blank_amounts_stay_none)

    def _no_numeric_field_silently_zeroed():
        # Same idea for every other optional numeric/int field the loader
        # touches: blank in -> None out, not 0.
        assert loaders.optional_int("", field_name="x", context="t") is None
        assert loaders.optional_int("   ", field_name="x", context="t") is None
        assert loaders.optional_decimal("", field_name="x", context="t") is None
        # And a REAL zero must still parse as zero, not be confused with blank.
        from decimal import Decimal

        assert loaders.optional_decimal("0", field_name="x", context="t") == Decimal("0")
        assert loaders.optional_int("0", field_name="x", context="t") == 0
        return "blank -> None, but literal '0' -> 0 (not conflated)"

    check(
        "no optional numeric parser silently converts a blank value to zero "
        "(and a literal '0' still parses as zero)",
        _no_numeric_field_silently_zeroed,
    )

    def _max_installment_months_blank_stays_none():
        blanks = [p for p in ds.profiles if p.max_installment_months is None]
        assert blanks, "expected at least one profile with blank max_installment_months"
        return f"{len(blanks)} profiles with max_installment_months == None (not 0)"

    check("blank max_installment_months remains None (never 0)", _max_installment_months_blank_stays_none)

    # --- exchange rate helper sanity --------------------------------------

    def _fx_same_currency_identity():
        from decimal import Decimal

        rate = loaders.get_exchange_rate(ds, ds.exchange_rates[0].rate_date, "USD", "USD")
        assert rate == Decimal("1"), f"expected identity rate 1 for same currency, got {rate}"
        return "USD->USD == 1"

    check("exchange-rate lookup returns identity (1) for same-currency pairs", _fx_same_currency_identity)

    def _fx_no_invented_reverse():
        # Pick a known direct row and confirm the *reverse* direction on the
        # same date is NOT invented (must be None unless a real reverse row
        # exists in the file).
        sample = ds.exchange_rates[0]
        forward = loaders.get_exchange_rate(ds, sample.rate_date, sample.from_currency, sample.to_currency)
        assert forward == sample.rate, "direct lookup did not return the exact stored rate"
        reverse_key = (sample.rate_date, sample.to_currency, sample.from_currency)
        reverse_row_exists = reverse_key in ds.exchange_rate_index
        reverse = loaders.get_exchange_rate(ds, sample.rate_date, sample.to_currency, sample.from_currency)
        if reverse_row_exists:
            assert reverse is not None
        else:
            assert reverse is None, (
                "get_exchange_rate invented a reverse rate that is not present in exchange_rates.csv"
            )
        return f"checked {sample.from_currency}->{sample.to_currency} on {sample.rate_date}; no invented reverse"

    check("exchange-rate lookup never invents an un-inverted reverse rate", _fx_no_invented_reverse)

    return results


def main() -> int:
    ds = loaders.load_dataset()
    results = run_checks(ds)

    print("=" * 78)
    print("PHASE 1 DATA-LAYER TEST RESULTS")
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
