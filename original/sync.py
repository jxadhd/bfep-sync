#!/usr/bin/env python3
"""GoFundMe donation-figure sync. Dry run unless --apply is given.

Connection settings come from the standard PGHOST, PGPORT, PGDATABASE,
PGUSER and PGPASSWORD environment variables.
"""
import argparse, random, shlex, subprocess, sys, time, urllib.error, urllib.request
from collections import Counter
from decimal import Decimal
from urllib.parse import urlparse

import psycopg2

from gofundme import ParseError, parse_gofundme

UA = 'TBEPSync/1.0 (+https://butterfly.joshcross.co.nz)'
HOSTS = {'gofund.me', 'www.gofund.me', 'gofundme.com', 'www.gofundme.com'}
DELAY = 6                      # seconds between requests (plus 0-2s jitter)
NOT_FOUND_AFTER = 3            # consecutive failures before status 'not_found'
DROP_LIMIT = Decimal('0.20')   # hold the update if raised falls by more than this

SELECT = """
SELECT cf.id, cf.url, cf.currency_code, cf.goal_amount, cf.donated_amount,
       cf.consecutive_failures
FROM campaign_fundraisers cf
JOIN campaigns c ON c.id = cf.campaign_id
WHERE cf.is_active AND cf.auto_sync AND c.deleted_at IS NULL
  AND cf.url ~* '(gofund[.]me|gofundme[.]com)'
ORDER BY cf.last_checked_at NULLS FIRST, cf.id
"""


class Blocked(Exception):
    pass


def normalize(raw):
    u = (raw or '').strip()
    if not u or ' ' in u:
        return None
    if not u.startswith(('http://', 'https://')):
        u = 'https://' + u
    p = urlparse(u)
    return u if p.hostname in HOSTS and p.path.strip('/') else None


def fetch(url):
    req = urllib.request.Request(url, headers={'User-Agent': UA})
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.read().decode('utf-8', 'replace')


def check(url, code, raised, goal=None):
    """Returns (status, figures_or_None, counts_as_failure)."""
    u = normalize(url)
    if u is None:
        return 'invalid_url', None, False
    try:
        fig = parse_gofundme(fetch(u))
    except urllib.error.HTTPError as e:
        if e.code in (403, 429):
            raise Blocked(e.code)
        if e.code in (404, 410):
            return 'not_found', None, True
        return f'http_{e.code}', None, False
    except ParseError:
        return 'parse_error', None, True
    except Exception:
        return 'error', None, False

    code = (code or '').strip()
    if not code:
        if raised:
            return 'currency_unknown', None, False
    elif fig.currency != code:
        # Same goal figure under a different label: the stored label is almost
        # certainly wrong, so adopt GoFundMe's currency. Anything else is held.
        if goal is None or fig.goal != goal:
            return 'currency_mismatch', None, False
    if raised and raised > 0 and fig.raised < raised * (1 - DROP_LIMIT):
        return 'suspicious_drop', None, False
    return 'ok', fig, False


def record(cur, rid, status, fig, failed, fails):
    if status == 'ok':
        cur.execute(
            """UPDATE campaign_fundraisers
               SET donated_amount = %s, goal_amount = COALESCE(%s, goal_amount),
                   currency_code = %s, last_checked_at = now(), last_success_at = now(),
                   sync_status = 'ok', consecutive_failures = 0
               WHERE id = %s""",
            (fig.raised, fig.goal, fig.currency, rid))
        return
    n = fails + 1 if failed else fails
    shown = 'not_found_pending' if status == 'not_found' and n < NOT_FOUND_AFTER else status
    cur.execute(
        """UPDATE campaign_fundraisers
           SET last_checked_at = now(), sync_status = %s, consecutive_failures = %s
           WHERE id = %s""",
        (shown, n, rid))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--apply', action='store_true', help='write changes (default is a dry run)')
    ap.add_argument('--limit', type=int, default=0, help='check at most N campaigns')
    ap.add_argument('--after', default='', help='command to run after a run that wrote changes')
    args = ap.parse_args()

    conn = psycopg2.connect(connect_timeout=10)
    cur = conn.cursor()
    cur.execute(SELECT + (' LIMIT %s' if args.limit else ''), (args.limit,) if args.limit else None)
    rows = cur.fetchall()
    conn.rollback()

    counts, changed, code = Counter(), 0, 0
    print(('APPLYING' if args.apply else 'DRY RUN') + f': {len(rows)} campaigns')
    for rid, url, ccode, goal, raised, fails in rows:
        try:
            status, fig, failed = check(url, ccode, raised, goal)
        except Blocked as e:
            print(f'BLOCKED (HTTP {e}), aborting run')
            code = 2
            break
        counts[status] += 1
        if status == 'ok':
            note = f'raised {raised} -> {fig.raised}'
            cur_changed = fig.currency != (ccode or '').strip()
            if cur_changed:
                note += f', CURRENCY {ccode} -> {fig.currency}'
            if fig.goal is not None and fig.goal != goal:
                note += f', goal {goal} -> {fig.goal}'
            print(f'{rid}: ok, {note}')
            if cur_changed or fig.raised != raised or (fig.goal is not None and fig.goal != goal):
                changed += 1
        else:
            print(f'{rid}: {status}')
        if args.apply:
            record(cur, rid, status, fig, failed, fails)
            conn.commit()
        if normalize(url) is not None:
            time.sleep(DELAY + random.random() * 2)

    print(dict(counts), f'| changed: {changed}')
    if args.apply and changed and args.after:
        subprocess.run(shlex.split(args.after), check=False)
    sys.exit(code)


main()
