"""
Phase 1 — Exact, deterministic dataset statistics.

Scans the COMPLETE local dataset (including the 25,000+-row
financial_events.csv) via the loaders module and prints exact counts.
Nothing here is sampled, estimated, or truncated.

Run:
    python3 code/evaluation/data_stats.py
"""

from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import loaders  # noqa: E402


def _print_counter(title: str, counter: Counter, total: int) -> None:
    print(f"\n{title} (n={total}):")
    for key, count in sorted(counter.items(), key=lambda kv: (-kv[1], str(kv[0]))):
        pct = (count / total * 100) if total else 0.0
        print(f"  {key!r:<30} {count:>7}  ({pct:5.1f}%)")


def main() -> int:
    ds = loaders.load_dataset()

    print("=" * 78)
    print("RAW ROW COUNTS")
    print("=" * 78)
    print(f"requests.csv                : {len(ds.requests)}")
    print(f"sample_requests.csv         : {len(ds.sample_requests)}")
    print(f"financial_profiles.csv      : {len(ds.profiles)}")
    print(f"financial_events.csv        : {len(ds.events)}")
    print(f"request_payment_options.csv : {len(ds.payment_options)}")
    print(f"messages.csv                : {len(ds.messages)}")
    print(f"images.csv                  : {len(ds.images)}")
    print(f"exchange_rates.csv          : {len(ds.exchange_rates)}")

    # --- request_type distribution ---
    request_type_counter = Counter(r.request_type for r in ds.requests)
    _print_counter("REQUEST_TYPE DISTRIBUTION (requests.csv)", request_type_counter, len(ds.requests))

    # --- financial event status distribution ---
    status_counter = Counter(e.status for e in ds.events)
    _print_counter("FINANCIAL EVENT STATUS DISTRIBUTION", status_counter, len(ds.events))

    # --- event direction distribution ---
    direction_counter = Counter(e.direction for e in ds.events)
    _print_counter("EVENT DIRECTION DISTRIBUTION", direction_counter, len(ds.events))

    # --- event_type distribution ---
    event_type_counter = Counter(e.event_type for e in ds.events)
    _print_counter("EVENT_TYPE DISTRIBUTION", event_type_counter, len(ds.events))

    # --- flexibility distribution ---
    flexibility_counter = Counter(e.flexibility for e in ds.events)
    _print_counter("FLEXIBILITY DISTRIBUTION", flexibility_counter, len(ds.events))

    # --- blank amounts ---
    print("\n" + "=" * 78)
    print("BLANK FINANCIAL-EVENT AMOUNTS")
    print("=" * 78)
    blank_events = [e for e in ds.events if e.amount is None]
    print(f"Total events with blank amount           : {len(blank_events)}")
    with_image = [e for e in blank_events if ds.find_image_for_event(e.event_id) is not None]
    without_image = [e for e in blank_events if ds.find_image_for_event(e.event_id) is None]
    print(f"  blank amounts WITH a matching image    : {len(with_image)}")
    print(f"  blank amounts WITHOUT a matching image : {len(without_image)}")
    if with_image:
        print("  event_ids with a matching image:")
        for e in with_image:
            img = ds.find_image_for_event(e.event_id)
            print(f"    {e.event_id} -> {img.image_id} ({img.file_path})")
    if without_image:
        print("  event_ids WITHOUT a matching image (Phase 2 must decide fallback):")
        for e in without_image:
            print(f"    {e.event_id}")

    # --- linked events ---
    print("\n" + "=" * 78)
    print("LINKED-EVENT COUNTS")
    print("=" * 78)
    linked_children = [e for e in ds.events if e.linked_event_id is not None]
    print(f"Events that carry a non-blank linked_event_id (children) : {len(linked_children)}")
    print(f"Distinct linked_event_id values referenced (parents)     : {len(ds.events_by_linked_event_id)}")
    dangling_links = [
        e for e in linked_children if e.linked_event_id not in ds.events_by_id
    ]
    print(f"Children whose linked_event_id does NOT resolve to a known event_id: {len(dangling_links)}")
    if dangling_links:
        for e in dangling_links:
            print(f"    {e.event_id} -> missing parent {e.linked_event_id}")

    # --- messages by source_type ---
    source_type_counter = Counter(m.source_type for m in ds.messages)
    _print_counter("MESSAGES BY SOURCE_TYPE", source_type_counter, len(ds.messages))

    # --- message linkage shape ---
    print("\n" + "=" * 78)
    print("MESSAGE LINKAGE SHAPE")
    print("=" * 78)
    msg_with_request = sum(1 for m in ds.messages if m.request_id is not None)
    msg_with_related_event = sum(1 for m in ds.messages if m.related_event_id is not None)
    msg_with_neither = sum(
        1 for m in ds.messages if m.request_id is None and m.related_event_id is None
    )
    print(f"Messages with a request_id            : {msg_with_request}")
    print(f"Messages with a related_event_id      : {msg_with_related_event}")
    print(f"Messages with neither (user-level only): {msg_with_neither}")

    # --- image linkage counts ---
    print("\n" + "=" * 78)
    print("IMAGE LINKAGE COUNTS")
    print("=" * 78)
    print(f"Total image rows                                  : {len(ds.images)}")
    img_with_request = sum(1 for i in ds.images if i.request_id is not None)
    img_with_related_event = sum(1 for i in ds.images if i.related_event_id is not None)
    print(f"Images with a request_id                          : {img_with_request}")
    print(f"Images with a related_event_id                    : {img_with_related_event}")
    img_related_event_resolves = sum(
        1 for i in ds.images if i.related_event_id is not None and i.related_event_id in ds.events_by_id
    )
    print(f"Images whose related_event_id resolves to a real event_id: {img_related_event_resolves}")
    img_file_exists = sum(1 for i in ds.images if i.file_path.exists())
    img_file_missing = [i for i in ds.images if not i.file_path.exists()]
    print(f"Images whose PNG file actually exists on disk     : {img_file_exists} / {len(ds.images)}")
    if img_file_missing:
        print("  MISSING files:")
        for i in img_file_missing:
            print(f"    {i.image_id} -> {i.file_path}")

    # --- payment methods ---
    payment_method_counter = Counter(p.payment_method for p in ds.payment_options)
    _print_counter("PAYMENT METHODS (request_payment_options.csv)", payment_method_counter, len(ds.payment_options))

    # --- installment durations ---
    print("\n" + "=" * 78)
    print("INSTALLMENT DURATIONS (number_of_payments where payment_method == 'installments')")
    print("=" * 78)
    installment_opts = [p for p in ds.payment_options if p.payment_method == "installments"]
    duration_counter = Counter(p.number_of_payments for p in installment_opts)
    _print_counter("number_of_payments distribution", duration_counter, len(installment_opts))
    if installment_opts:
        durations = [p.number_of_payments for p in installment_opts]
        print(f"  min={min(durations)}  max={max(durations)}  mean={sum(durations)/len(durations):.2f}")
    freq_counter = Counter(p.payment_frequency_days for p in installment_opts)
    _print_counter("payment_frequency_days distribution (installments only)", freq_counter, len(installment_opts))

    # --- users' payment-method preferences ---
    print("\n" + "=" * 78)
    print("USERS' PAYMENT-METHOD PREFERENCES (financial_profiles.csv)")
    print("=" * 78)
    pref_combo_counter = Counter(p.payment_methods_user_will_consider for p in ds.profiles)
    _print_counter("Exact combination of payment_methods_user_will_consider", pref_combo_counter, len(ds.profiles))
    single_method_counter: Counter = Counter()
    for p in ds.profiles:
        for m in p.payment_methods_user_will_consider:
            single_method_counter[m] += 1
    _print_counter(
        "Individual payment methods accepted (profiles can accept >1)",
        single_method_counter,
        len(ds.profiles),
    )

    # --- max_installment_months distribution ---
    print("\n" + "=" * 78)
    print("MAX_INSTALLMENT_MONTHS DISTRIBUTION (financial_profiles.csv)")
    print("=" * 78)
    blank_max_months = sum(1 for p in ds.profiles if p.max_installment_months is None)
    print(f"Profiles with BLANK max_installment_months (won't consider installments): {blank_max_months}")
    month_counter = Counter(p.max_installment_months for p in ds.profiles if p.max_installment_months is not None)
    _print_counter("Non-blank max_installment_months values", month_counter, len(ds.profiles) - blank_max_months)

    # Cross-check: does "blank max_installment_months" line up with "installments"
    # missing from payment_methods_user_will_consider? (informational only,
    # no assumption is baked into the loader -- this is just a report.)
    mismatch = [
        p
        for p in ds.profiles
        if p.max_installment_months is None and "installments" in p.payment_methods_user_will_consider
    ]
    print(
        f"\nProfiles with BLANK max_installment_months but 'installments' still in "
        f"payment_methods_user_will_consider: {len(mismatch)}"
    )
    if mismatch:
        for p in mismatch[:10]:
            print(f"    {p.user_id}")
        if len(mismatch) > 10:
            print(f"    ... and {len(mismatch) - 10} more")

    # --- FX coverage ---
    print("\n" + "=" * 78)
    print("EXCHANGE-RATE COVERAGE (exchange_rates.csv)")
    print("=" * 78)
    pairs: dict = {}
    for r in ds.exchange_rates:
        pairs.setdefault((r.from_currency, r.to_currency), []).append(r.rate_date)
    currencies_seen = sorted({c for pair in pairs for c in pair})
    print(f"Distinct currencies appearing in exchange_rates.csv: {currencies_seen}")
    print(f"Distinct (from_currency -> to_currency) directions : {len(pairs)}")
    for (frm, to), dates in sorted(pairs.items()):
        dates_sorted = sorted(dates)
        print(
            f"  {frm} -> {to:<4} : {len(dates_sorted):>3} rows, "
            f"{dates_sorted[0]} .. {dates_sorted[-1]}"
        )

    # Which directions are NEVER present (only matters for currencies that
    # actually co-occur in the dataset -- reported, not invented/assumed).
    all_currencies_in_profiles = {p.home_currency for p in ds.profiles}
    all_currencies_in_events = {e.currency for e in ds.events}
    all_currencies = sorted(all_currencies_in_profiles | all_currencies_in_events)
    print(f"\nCurrencies seen in financial_profiles.csv : {sorted(all_currencies_in_profiles)}")
    print(f"Currencies seen in financial_events.csv   : {sorted(all_currencies_in_events)}")
    print("\nDirect-lookup coverage matrix (row present? Y/N) for every ordered pair")
    print("of currencies that actually occur in the dataset (self-pairs excluded):")
    header = "        " + " ".join(f"{c:>6}" for c in all_currencies)
    print(header)
    for frm in all_currencies:
        cells = []
        for to in all_currencies:
            if frm == to:
                cells.append(f"{'—':>6}")
            else:
                cells.append(f"{'Y' if (frm, to) in pairs else 'N':>6}")
        print(f"  {frm:<5} " + " ".join(cells))

    reverse_missing = [
        (frm, to)
        for (frm, to) in pairs
        if (to, frm) not in pairs
    ]
    print(
        f"\nDirections with NO reverse row present in the file "
        f"(i.e. inverting would be an invented rule, not data): {len(reverse_missing)}"
    )
    for frm, to in sorted(reverse_missing):
        print(f"    {frm} -> {to} exists, but {to} -> {frm} does NOT")

    print("\nDone. This script performs no financial decisioning -- it only reports facts.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
