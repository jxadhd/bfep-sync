"""Fundraiser figure sync. Dry run unless --apply is given.

    python -m bfep_sync [--apply] [--limit N] [--after CMD] [--provider NAMES]

Exit codes: 0 ok, 2 a platform blocked us (403/429), 3 another applying run is
in progress, 4 bad configuration, 5 the database could not be reached.
"""
from __future__ import annotations

import argparse
import sys

from . import providers as registry
from .runner import run


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog='bfep_sync')
    ap.add_argument('--apply', action='store_true', help='write changes (default is a dry run)')
    ap.add_argument('--limit', type=int, default=0, help='check at most N campaigns')
    ap.add_argument('--after', default='', help='command to run after a run that wrote changes')
    ap.add_argument('--provider', default=None,
                    help='comma-separated providers to run (default: $SYNC_PROVIDERS or "gofundme")')
    ap.add_argument('--retries', type=int, default=1,
                    help='retries for network errors and HTTP 5xx per campaign (default 1; 0 = original behaviour)')
    args = ap.parse_args(argv)

    try:
        providers = registry.enabled(args.provider)
    except ValueError as e:
        print(f'config error: {e}', file=sys.stderr)
        return 4

    from .store import AlreadyRunning, PostgresStore
    try:
        store = PostgresStore.connect()
    except Exception as e:  # psycopg2.OperationalError, missing psycopg2, bad settings
        print(f'database connection failed: {str(e).strip()}', file=sys.stderr)
        return 5
    # Only writing runs need to exclude each other. A dry run takes no lock, so
    # it can't make the timer skip a cycle.
    if args.apply:
        try:
            store.lock()
        except AlreadyRunning as e:
            print(str(e), file=sys.stderr)
            return 3
    result = run(store, providers, apply=args.apply, limit=args.limit, after=args.after,
                 retries=max(0, args.retries))
    return result.exit_code


if __name__ == '__main__':
    sys.exit(main())
