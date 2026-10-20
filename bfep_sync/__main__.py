"""Fundraiser figure sync. Dry run unless --apply is given.

    python -m bfep_sync [--apply] [--limit N] [--after CMD] [--provider NAMES]
    python -m bfep_sync --id ID ...      check only these fundraisers (dry run unless --apply)
    python -m bfep_sync --line N ...     check only these campaigns' active fundraisers
    python -m bfep_sync --problems       list fundraisers the sync could not update
    python -m bfep_sync --accept ID ...  save what the platform shows now, even a drop

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
    ap.add_argument('--problems', action='store_true',
                    help='list the fundraisers whose last check was not ok, with what to do (reads only)')
    ap.add_argument('--id', type=int, nargs='+', metavar='ID', dest='ids',
                    help='check only these fundraisers, with the usual rules (a dry run unless --apply)')
    ap.add_argument('--line', type=int, nargs='+', metavar='N', dest='lines',
                    help="check only the active fundraisers of the campaigns with these line numbers")
    ap.add_argument('--accept', type=int, nargs='+', metavar='ID',
                    help='check these fundraisers again and save what their platforms show, even a big drop')
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
    if args.problems:
        from .repair import list_problems
        return list_problems(store.conn)
    # Only writing runs need to exclude each other. A dry run takes no lock, so
    # it can't make the timer skip a cycle.
    if args.apply or args.accept:
        try:
            store.lock()
        except AlreadyRunning as e:
            print(str(e), file=sys.stderr)
            return 3
    if args.accept:
        from .runner import accept
        return accept(store, providers, args.accept, after=args.after)
    ids = list(args.ids or [])
    if args.lines:
        import psycopg2
        try:
            found = store.ids_for_lines(args.lines)
        except psycopg2.errors.InsufficientPrivilege:
            print('--line needs deploy/sync-details.sql (it lets the sync read line numbers)', file=sys.stderr)
            return 4
        if not found:
            print(f'no active fundraisers on line {", ".join(map(str, args.lines))}', file=sys.stderr)
            return 4
        ids += [i for i in found if i not in ids]
    result = run(store, providers, apply=args.apply, limit=args.limit, after=args.after,
                 retries=max(0, args.retries), ids=ids)
    return result.exit_code


if __name__ == '__main__':
    sys.exit(main())
