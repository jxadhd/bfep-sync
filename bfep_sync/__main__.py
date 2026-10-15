"""Fundraiser figure sync. Dry run unless --apply is given.

    python -m bfep_sync [--apply] [--limit N] [--after CMD] [--provider NAMES]

Exit codes: 0 ok, 2 a platform blocked us (403/429), 3 another applying run is
in progress, 4 bad configuration, 5 the database could not be reached.
"""
from __future__ import annotations

import argparse
import difflib
import os
import sys

from . import providers as registry
from .runner import run


class Parser(argparse.ArgumentParser):
    """Suggests the nearest option for a mistyped one (--aply -> --apply)."""

    def parse_args(self, args=None, namespace=None):
        args, unknown = self.parse_known_args(args, namespace)
        if unknown:
            options = [o for a in self._actions for o in a.option_strings if o.startswith('--')]
            lines = []
            for arg in unknown:
                word = '--' + arg.lstrip('-').split('=')[0]
                close = difflib.get_close_matches(word, options, 1, 0.6)
                lines.append(f'unknown option {arg}' + (f' (did you mean {close[0]}?)' if close else ''))
            self.error('; '.join(lines))
        return args

    def error(self, message):
        self.print_usage(sys.stderr)
        hint = "\nRun 'bfep help sync' for the options." if self.prog == 'bfep sync' else ''
        self.exit(2, f'{self.prog}: {message}{hint}\n')


def main(argv: list[str] | None = None) -> int:
    # "bfep sync" runs this with BFEP_SYNC_PROG set, so messages name the
    # command that was typed.
    ap = Parser(prog=os.environ.get('BFEP_SYNC_PROG') or 'bfep_sync')
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
