"""
Phase 1 entry point.

For this phase, main.py only exercises the deterministic data-access
layer (loaders.py) -- it loads the full dataset, builds indices, and
prints a short structural summary. It intentionally does NOT produce
any financial decisions or write output.csv; that is Phase 2+.

Run:
    python3 code/main.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import loaders  # noqa: E402


def main() -> int:
    ds = loaders.load_dataset()

    print("Phase 1 data layer loaded successfully.\n")

    print("Row counts:")
    print(f"  requests.csv               : {len(ds.requests)}")
    print(f"  sample_requests.csv        : {len(ds.sample_requests)}")
    print(f"  financial_profiles.csv     : {len(ds.profiles)}")
    print(f"  financial_events.csv       : {len(ds.events)}")
    print(f"  request_payment_options.csv: {len(ds.payment_options)}")
    print(f"  messages.csv               : {len(ds.messages)}")
    print(f"  images.csv                 : {len(ds.images)}")
    print(f"  exchange_rates.csv         : {len(ds.exchange_rates)}")

    print("\nIndex sanity spot-checks:")
    print(f"  distinct request_ids indexed (requests_by_id)        : {len(ds.requests_by_id)}")
    print(f"  distinct request_ids indexed (sample_requests_by_id) : {len(ds.sample_requests_by_id)}")
    print(f"  distinct user_ids indexed (profiles_by_user)         : {len(ds.profiles_by_user)}")
    print(f"  distinct event_ids indexed (events_by_id)            : {len(ds.events_by_id)}")
    print(f"  users with at least one financial event               : {len(ds.events_by_user)}")
    print(f"  requests with at least one payment option             : {len(ds.payment_options_by_request)}")
    print(f"  requests with at least one message                    : {len(ds.messages_by_request)}")
    print(f"  requests with at least one image                      : {len(ds.images_by_request)}")

    blank_amount_events = [e for e in ds.events if e.amount is None]
    print(f"\nEvents with blank amount (must resolve via image, never via 0): {len(blank_amount_events)}")
    resolved = sum(1 for e in blank_amount_events if ds.find_image_for_event(e.event_id) is not None)
    print(f"  of which have a matching image reference                     : {resolved}")
    print(f"  of which have NO matching image reference (Phase 2 gap)      : {len(blank_amount_events) - resolved}")

    print(
        "\nFor full dataset statistics run: python3 code/evaluation/data_stats.py"
    )
    print("For data-layer tests run       : python3 code/evaluation/test_data_layer.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
