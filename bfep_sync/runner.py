"""The sync loop: picks rows, asks each row's provider for figures, applies
the safeguards and records the outcome.

The decision rules in check() are the original sync.py rules, except that a
stated currency is now adopted (see below):

* invalid URL -> 'invalid_url' (not a failure)
* HTTP 403/429 -> Blocked: stop calling that platform for the rest of the run
* HTTP 404/410 -> 'not_found' (a failure; shown as 'not_found_pending' until
  NOT_FOUND_AFTER consecutive failures, when the store also turns auto_sync off)
* other HTTP status -> 'http_<code>' (not a failure)
* unparseable page -> 'parse_error' (a failure)
* the platform hides the figures -> 'hidden_totals' (not a failure; the
  stored figures stay as staff entered them)
* other exception -> 'error' (not a failure)
* page shows only a bare "$" -> the stored currency is kept when it is a
  dollar currency, otherwise 'currency_unknown' (GoGetFunding only)
* the page states a currency that differs from the stored one (or there is
  none stored) -> the page's currency is adopted, and the drop check below is
  skipped because the old and new amounts are in different currencies.
  With SYNC_HOLD_CURRENCY=1 the original rules apply instead: no stored
  currency but a stored raised amount -> 'currency_unknown'; a different
  currency with a different goal -> 'currency_mismatch' (the same goal under
  another label means the stored label is wrong, so it is adopted)
* raised fell by more than DROP_LIMIT -> 'suspicious_drop' (held)

A held outcome ('suspicious_drop', 'currency_mismatch') still carries the
figures the platform showed, so the run can say what was held; they are
never written over the stored figures.
"""
from __future__ import annotations

import os
import random
import re
import shlex
import subprocess
import time
import urllib.error
import urllib.request
from collections import Counter
from dataclasses import dataclass, field, replace
from decimal import Decimal
from typing import Callable, Iterable

from . import repair
from .providers import Blocked, Fetcher, Figures, NotConfigured, ParseError, Provider, TotalsHidden

UA = 'TBEPSync/1.0 (+https://bfep.org)'
NOT_FOUND_AFTER = 3            # consecutive failures before status 'not_found'
DROP_LIMIT = Decimal('0.20')   # hold the update if raised falls by more than this
TRANSIENT_HTTP = {500, 502, 503, 504}
# Currencies written with a bare "$", so a page showing only "$" can be one of them.
DOLLAR_CODES = {'USD', 'AUD', 'NZD', 'CAD', 'HKD', 'SGD'}


def http_fetch(url: str, headers: dict | None = None, data: bytes | None = None) -> str:
    """GETs `url`, or POSTs `data` to it when given (for JSON APIs)."""
    req = urllib.request.Request(url, data=data, headers={'User-Agent': UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.read().decode('utf-8', 'replace')


@dataclass
class Row:
    id: int
    url: str | None
    currency_code: str | None
    goal: Decimal | None
    raised: Decimal | None
    consecutive_failures: int
    campaign_id: int | None = None


@dataclass
class Outcome:
    status: str
    figures: Figures | None
    failed: bool
    detail: str = ''   # why, for parse_error and error

    @property
    def held(self) -> bool:
        """The platform answered, but the figures were not saved."""
        return self.status != 'ok' and self.figures is not None


def provider_for(url: str | None, providers: Iterable[Provider]) -> Provider | None:
    """The provider whose url_pattern matches the stored URL (as the SQL did)."""
    for p in providers:
        if re.search(p.url_pattern, url or '', re.IGNORECASE):
            return p
    return None


def fetch_with_retry(provider: Provider, url: str, fetch: Fetcher, retries: int,
                     sleep: Callable[[float], None]) -> Figures:
    """Calls the provider, retrying network errors and HTTP 5xx `retries` times.

    403/429/404/410 and parse errors are never retried: they are answers, not
    glitches, and retrying a 429 would make the block worse.
    """
    attempt = 0
    while True:
        try:
            return provider.fetch_figures(url, fetch)
        except urllib.error.HTTPError as e:
            if e.code not in TRANSIENT_HTTP or attempt >= retries:
                raise
        except (ParseError, NotConfigured, TotalsHidden):
            raise
        except Exception:
            if attempt >= retries:
                raise
        attempt += 1
        sleep(5 * attempt)


def check(provider: Provider, row: Row, fetch: Fetcher, retries: int = 0,
          sleep: Callable[[float], None] | None = None, accept: bool = False) -> Outcome:
    """`accept` takes whatever the platform shows: no drop or currency hold
    (bfep sync accept, after someone has checked the page)."""
    sleep = sleep or time.sleep
    u = provider.normalize(row.url)
    if u is None:
        return Outcome('invalid_url', None, False)
    try:
        fig = fetch_with_retry(provider, u, fetch, retries, sleep)
    except urllib.error.HTTPError as e:
        if e.code in (403, 429):
            raise Blocked(e.code)
        if e.code in (404, 410):
            return Outcome('not_found', None, True)
        return Outcome(f'http_{e.code}', None, False)
    except ParseError as e:
        return Outcome('parse_error', None, True, str(e))
    except NotConfigured:
        return Outcome('not_configured', None, False)
    except TotalsHidden as e:
        return Outcome('hidden_totals', None, False, str(e))
    except Exception as e:
        return Outcome('error', None, False, f'{type(e).__name__}: {e}')

    code = (row.currency_code or '').strip()
    raised, goal = row.raised, row.goal
    if fig.currency is None:
        if code not in DOLLAR_CODES:
            return Outcome('currency_unknown', None, False)
        fig = replace(fig, currency=code)
    if accept:
        return Outcome('ok', fig, False)
    if os.environ.get('SYNC_HOLD_CURRENCY') == '1':
        if not code:
            if raised:
                return Outcome('currency_unknown', None, False)
        elif fig.currency != code:
            if goal is None or fig.goal != goal:
                return Outcome('currency_mismatch', fig, False)
    elif fig.currency != code:
        return Outcome('ok', fig, False)    # adopted; the run prints a CURRENCY line
    if raised and raised > 0 and fig.raised < raised * (1 - DROP_LIMIT):
        return Outcome('suspicious_drop', fig, False)
    return Outcome('ok', fig, False)


def shown_status(outcome: Outcome, fails: int) -> tuple[str, int]:
    """The sync_status and consecutive_failures to store for a non-ok outcome."""
    n = fails + 1 if outcome.failed else fails
    shown = 'not_found_pending' if outcome.status == 'not_found' and n < NOT_FOUND_AFTER else outcome.status
    return shown, n


def describe_ok(row: Row, fig: Figures) -> tuple[str, bool]:
    note = f'raised {row.raised} -> {fig.raised}'
    cur_changed = fig.currency != (row.currency_code or '').strip()
    if cur_changed:
        note += f', CURRENCY {row.currency_code} -> {fig.currency}'
    if fig.goal is not None and fig.goal != row.goal:
        note += f', goal {row.goal} -> {fig.goal}'
    changed = cur_changed or fig.raised != row.raised or (fig.goal is not None and fig.goal != row.goal)
    return note, changed


def money(amount: Decimal | None) -> str:
    """1234.5 -> '1,234.50', 1200 -> '1,200'."""
    if amount is None:
        return 'none'
    return f'{amount:,.0f}' if amount == amount.to_integral_value() else f'{amount:,.2f}'


def percent(raised: Decimal | None, goal: Decimal | None) -> str | None:
    if raised is None or not goal or goal <= 0:
        return None
    return f'{raised / goal * 100:.0f}%'


def describe_held(row: Row, fig: Figures) -> str:
    """What the platform showed against what is stored, for a held outcome.

    The percentages let staff check a platform page that shows only how far
    along the goal a fundraiser is (some Chuffed pages show nothing else).
    """
    stored_ccy = (row.currency_code or '').strip() or '?'
    note = f'stored {money(row.raised)} {stored_ccy}, platform shows {money(fig.raised)} {fig.currency or stored_ccy}'
    if row.raised and row.raised > 0 and fig.currency in (None, stored_ccy):
        change = (fig.raised - row.raised) / row.raised * 100
        note += f' ({change:+.0f}%)'
    old_pct, new_pct = percent(row.raised, row.goal), percent(fig.raised, fig.goal if fig.goal is not None else row.goal)
    if old_pct or new_pct:
        goal = fig.goal if fig.goal is not None else row.goal
        note += f'; {old_pct or "?"} -> {new_pct or "?"} of the {money(goal)} goal'
    if fig.goal is not None and row.goal is not None and fig.goal != row.goal:
        note += f' (stored goal {money(row.goal)})'
    if fig.donations is not None:
        note += f'; {fig.donations} donations'
    return note + '; not saved'


@dataclass
class RunResult:
    counts: Counter = field(default_factory=Counter)
    changed: int = 0
    exit_code: int = 0
    blocked: list[str] = field(default_factory=list)


def run(store, providers: list[Provider], *, apply: bool, limit: int = 0, after: str = '',
        fetch: Fetcher | None = None, retries: int = 1, out: Callable[[str], None] | None = None,
        sleep: Callable[[float], None] | None = None,
        jitter: Callable[[], float] | None = None, ids: list[int] | None = None) -> RunResult:
    """`ids` checks only those fundraisers (active ones, even with nightly
    sync off), with the same rules as a full run."""
    # Resolved at call time (not as default arguments) so tests can patch them.
    fetch, sleep, jitter = fetch or http_fetch, sleep or time.sleep, jitter or random.random
    # Flushed so each line shows up at once, even through a pipe or the journal.
    out = out or (lambda line: print(line, flush=True))
    result = RunResult()
    active = []
    for p in providers:
        if p.configured():
            active.append(p)
        else:
            out(f'{p.name}: not configured, skipped')
    if ids:
        rows = [r for r in store.select_ids(ids) if provider_for(r.url, active)]
        missing = sorted(set(ids) - {r.id for r in rows})
        if missing:
            out(f'not checked (no active fundraiser with a link an enabled platform syncs): '
                f'{", ".join(map(str, missing))}')
    else:
        rows = store.select(active, limit)
    out(('APPLYING' if apply else 'DRY RUN') + f': {len(rows)} campaigns')
    for p in active:
        urls = [u for row in rows if provider_for(row.url, active) is p and (u := p.normalize(row.url))]
        if urls:
            p.log = out
            try:
                note = p.prefetch(urls, fetch)
                if note:
                    out(f'{p.name}: {note}')
            except Exception as e:
                out(f'{p.name}: batch fetch failed ({type(e).__name__}: {e}), fetching one at a time')

    blocked: set[str] = set()
    attention: list[str] = []
    for row in rows:
        provider = provider_for(row.url, active)
        if provider is None or provider.name in blocked:
            continue
        try:
            outcome = check(provider, row, fetch, retries, sleep)
        except Blocked as e:
            out(f'BLOCKED by {provider.name} (HTTP {e}), skipping its remaining campaigns')
            blocked.add(provider.name)
            result.blocked.append(provider.name)
            result.exit_code = 2
            if len(blocked) == len(active):
                break
            continue
        result.counts[outcome.status] += 1
        if outcome.status == 'ok':
            note, changed = describe_ok(row, outcome.figures)
            out(f'{row.id}: ok, {note}')
            if changed:
                result.changed += 1
        else:
            why = describe_held(row, outcome.figures) if outcome.held else outcome.detail
            note = f' ({outcome.detail})' if outcome.detail else ''
            if outcome.held:
                note += ', ' + why
            shown = shown_status(outcome, row.consecutive_failures)[0]
            if shown == 'not_found':
                note += ', sync turned off' if apply else ', sync would be turned off'
            out(f'{row.id}: {outcome.status}{note}')
            if shown in repair.ATTENTION:
                attention.append(f'{row.id} {shown}: {repair.fix_link(row.campaign_id, row.id)}')
        if apply:
            if outcome.status == 'ok':
                store.record_ok(row.id, outcome.figures)
            else:
                store.record_problem(row.id, *shown_status(outcome, row.consecutive_failures),
                                     outcome.figures, why)
        # A provider with delay 0 paces its own requests (see Chuffed).
        if provider.delay and provider.normalize(row.url) is not None:
            sleep(provider.delay + jitter() * 2)

    out(f'{dict(result.counts)} | changed: {result.changed}')
    if attention:
        out(f'Needs a look ({len(attention)}), see bfep sync problems for what to do:')
        for line in attention:
            out(f'  {line}')
    if apply and result.changed and after:
        subprocess.run(shlex.split(after), check=False)
    return result


def accept(store, providers: list[Provider], ids: list[int], *, after: str = '',
           fetch: Fetcher | None = None, out: Callable[[str], None] = print,
           sleep: Callable[[float], None] | None = None) -> int:
    """Checks the named fundraisers again and saves what their platforms show
    now, even when it is a big drop. For held figures someone has checked.

    Returns 0 when every one was saved, 1 otherwise.
    """
    fetch, sleep = fetch or http_fetch, sleep or time.sleep
    active = [p for p in providers if p.configured()]
    rows = {r.id: r for r in store.select_ids(ids)}
    saved = 0
    for rid in ids:
        row = rows.get(rid)
        if row is None:
            out(f'{rid}: not saved, no active fundraiser with this ID')
            continue
        provider = provider_for(row.url, active)
        if provider is None:
            out(f'{rid}: not saved, the sync does not read this link: {row.url}')
            continue
        try:
            outcome = check(provider, row, fetch, 1, sleep, accept=True)
        except Blocked as e:
            out(f'{rid}: not saved, {provider.name} refused the request (HTTP {e}); try again later')
            continue
        if outcome.status != 'ok':
            note = f' ({outcome.detail})' if outcome.detail else ''
            out(f'{rid}: not saved, {outcome.status}{note}; see bfep sync problems')
            continue
        note, _ = describe_ok(row, outcome.figures)
        store.record_ok(rid, outcome.figures)
        saved += 1
        out(f'{rid}: saved, {note}')
    if saved and after:
        subprocess.run(shlex.split(after), check=False)
    return 0 if saved == len(ids) else 1
