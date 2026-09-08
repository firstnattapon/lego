"""Dry-run/resume/rollback one realized FIFO v2→v3 migration.

Dry-run is the default. ``--apply`` copies one bounded batch and persists its
checkpoint. ``--rollback --confirm`` is accepted only before schema v3 becomes
authoritative. It cancels the migration epoch and retains unlinked copied pages;
a later migration uses a new page generation, without deleting broker history.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("chain_key")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true")
    mode.add_argument("--rollback", action="store_true")
    parser.add_argument("--confirm", action="store_true")
    parser.add_argument("--max-pages", type=int, default=64)
    args = parser.parse_args()
    if args.rollback and not args.confirm:
        raise SystemExit("--rollback requires --confirm")

    from execution_service import _init_firebase
    from lego_state import (migrate_realized_open_legs,
                            rollback_realized_open_legs_migration)

    _init_firebase()
    if args.rollback:
        result = rollback_realized_open_legs_migration(args.chain_key)
        action = "ROLLBACK_UNLINKED_PAGES"
    else:
        result = migrate_realized_open_legs(
            args.chain_key, dry_run=not args.apply, max_pages=args.max_pages)
        action = "APPLY_BATCH" if args.apply else "DRY_RUN"
    print(json.dumps({"action": action, "chain_key": args.chain_key, **result},
                     sort_keys=True))


if __name__ == "__main__":
    main()
