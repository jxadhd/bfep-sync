"""Help for fixing what the sync could not: what each status means, where to
fix it, and the rows that need a look (bfep sync problems).
"""
from __future__ import annotations

import os
from datetime import datetime
from decimal import Decimal
from typing import Callable

# What each status means and what to do about it, in the order problems are listed.
STATUSES = {
    'suspicious_drop': (
        'Raised fell by more than 20%, so the new figure was not saved.',
        'Compare with the fundraiser page. If the new figure is right, accept it '
        '(bfep sync accept ID, or "Use these figures" on the fundraiser). '
        'If not, leave it: it stays held until the page recovers.'),
    'currency_mismatch': (
        'The page states another currency and goal, so nothing was saved.',
        'Check the page. If its currency is right, accept it.'),
    'currency_unknown': (
        'The page shows only "$" and no currency is stored, so nothing was saved.',
        'Set the currency on the fundraiser.'),
    'parse_error': (
        "The page loaded but its figures couldn't be read.",
        'One on its own: check the page. Several on one platform: its page changed; tell Josh.'),
    'not_found': (
        'The page was not found 3 nights running, so the sync turned itself off for it.',
        'Fix the link, add the new fundraiser if it moved, or end this one.'),
    'invalid_url': (
        "The link isn't a fundraiser address the sync understands.",
        'Fix the link on the fundraiser.'),
    'hidden_totals': (
        "The platform hides this fundraiser's figures (its page shows only a percentage), "
        'so the sync leaves the saved figures alone.',
        'Now and then, enter raised = goal x the percentage on the page (the fundraiser form '
        'has a box for the percentage). The sync carries on checking and takes over again if '
        'the figures come back.'),
    'not_found_pending': (
        'The page was not found, but fewer than 3 nights running.',
        'Usually nothing yet; check the link if it looks wrong.'),
    'not_configured': (
        "The platform's sync needs a setting that is missing.",
        'Tell Josh.'),
}
OTHER = ('The platform answered with an error.', 'Nothing to do unless it repeats for days.')
# Statuses the end of a run lists; the rest usually sort themselves out.
ATTENTION = ('suspicious_drop', 'currency_mismatch', 'currency_unknown', 'parse_error',
             'not_found', 'invalid_url')
# Statuses whose figure bfep sync accept can take.
ACCEPTABLE = ('suspicious_drop', 'currency_mismatch')


def site() -> str:
    return (os.environ.get('SITE_URL') or 'https://bfep.org').rstrip('/')


def fix_link(campaign_id: int | None, fundraiser_id: int) -> str:
    """The admin page where staff edit this fundraiser."""
    if campaign_id is None:
        return f'{site()}/admin/bfep/campaigns'
    return f'{site()}/admin/bfep/campaign/{campaign_id}/fundraiser/{fundraiser_id}/edit'


def explain(status: str) -> tuple[str, str]:
    return STATUSES.get(status, OTHER)


def order(status: str) -> int:
    keys = list(STATUSES)
    return keys.index(status) if status in keys else len(keys)


def when(value: datetime | None) -> str:
    return value.strftime('%Y-%m-%d') if value else 'never'


def amount(value: Decimal | None, currency: str | None) -> str:
    from .runner import money
    return f'{money(value)} {(currency or "").strip() or "?"}'.strip()


PROBLEMS = """
SELECT cf.id, cf.campaign_id, c.line_number, cf.url, CAST(cf.sync_status AS TEXT),
       cf.currency_code, cf.goal_amount, cf.donated_amount, cf.consecutive_failures,
       cf.last_success_at, {details}
FROM campaign_fundraisers cf
JOIN campaigns c ON c.id = cf.campaign_id
WHERE cf.is_active AND c.deleted_at IS NULL
  AND cf.sync_status IS NOT NULL AND CAST(cf.sync_status AS TEXT) <> 'ok'
  AND (cf.auto_sync OR CAST(cf.sync_status AS TEXT) = 'not_found')
ORDER BY cf.id
"""
DETAILS = 'cf.platform_raised, cf.platform_goal, cf.platform_currency, cf.platform_checked_at, cf.sync_detail'


def list_problems(conn, out: Callable[[str], None] = print) -> int:
    """Prints every active fundraiser whose last sync was not ok, grouped by
    status, with what it means, what to do and where. Reads only."""
    import psycopg2
    cur = conn.cursor()
    try:
        try:
            cur.execute(PROBLEMS.format(details=DETAILS))
        except psycopg2.errors.UndefinedColumn:
            conn.rollback()
            cur.execute(PROBLEMS.format(details='NULL, NULL, NULL, NULL, NULL'))
    except psycopg2.errors.InsufficientPrivilege:
        conn.rollback()
        out('The sync database user cannot read sync statuses yet: run deploy/sync-details.sql '
            'as data (see CHANGES.md item 28).')
        return 4
    rows = cur.fetchall()
    conn.rollback()
    if not rows:
        out('No problems: every synced fundraiser was updated on its last check.')
        return 0
    groups: dict[str, list] = {}
    for r in rows:
        groups.setdefault(r[4], []).append(r)
    out(f'{len(rows)} fundraisers need a look or are waiting:')
    for status in sorted(groups, key=order):
        meaning, todo = explain(status)
        out('')
        out(f'== {status} ({len(groups[status])}): {meaning}')
        out(f'   {todo}')
        for (fid, cid, line, url, _, ccy, goal, raised, fails, success,
             p_raised, p_goal, p_ccy, p_checked, detail) in groups[status]:
            head = f'line {line}, fundraiser {fid}' if line is not None else f'fundraiser {fid}'
            if detail:
                head += f': {detail}' + (f' (checked {when(p_checked)})' if p_raised is not None else '')
            elif status in ACCEPTABLE and p_raised is not None:
                head += f': stored {amount(raised, ccy)}, platform showed {amount(p_raised, p_ccy)} on {when(p_checked)}'
            else:
                head += f': stored {amount(raised, ccy)} of {amount(goal, ccy)}'
            if fails:
                head += f', failed {fails} time{"s" if fails != 1 else ""} in a row'
            head += f', last updated {when(success)}' if success else ', never updated by the sync'
            out(f'  - {head}')
            out(f'    page: {(url or "").strip() or "(no link)"}')
            out(f'    edit: {fix_link(cid, fid)}')
    held = [str(r[0]) for s in ACCEPTABLE for r in groups.get(s, [])]
    if held:
        out('')
        out('After checking a page, to take the figure the platform shows now:')
        out(f'  bfep sync accept {" ".join(held)}   (or just the IDs you checked)')
    return 0
