"""Polls the email watch directory (SPEC.md §10 Phase 6).

Run via `make watch-inbox`. Polling rather than inotify/FSEvents on purpose:
it's a few lines, behaves identically on every platform, and this whole
directory is a stand-in for an inbound mail webhook anyway — the interval
stops mattering the moment that's real.

Pass --once to process whatever is currently there and exit (what tests and
the demo use).
"""
import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.db import SessionLocal  # noqa: E402
from app.ingest.email_stub import scan_inbox  # noqa: E402

POLL_SECONDS = 5


def _report(results) -> None:
    for result in results:
        if result.status == "ingested":
            print(f"  {result.source_name}: {len(result.invoice_ids)} invoice(s) for tenant {result.tenant_id}")
        else:
            print(f"  {result.source_name}: QUARANTINED — {result.reason}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true", help="scan once and exit instead of polling")
    args = parser.parse_args()

    db = SessionLocal()
    try:
        if args.once:
            results = scan_inbox(db)
            print(f"Scanned inbox: {len(results)} email(s).")
            _report(results)
            return 0

        print(f"Watching inbox every {POLL_SECONDS}s. Ctrl-C to stop.")
        while True:
            results = scan_inbox(db)
            if results:
                print(f"Scanned inbox: {len(results)} email(s).")
                _report(results)
            time.sleep(POLL_SECONDS)
    except KeyboardInterrupt:
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
