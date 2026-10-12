"""The sync loop: picks rows, asks each row's provider for figures, applies
the safeguards and records the outcome.

The decision rules in check() are the original sync.py rules, unchanged:

* invalid URL -> 'invalid_url' (not a failure)
* HTTP 403/429 -> Blocked: stop calling that platform for the rest of the run
* HTTP 404/410 -> 'not_found' (a failure; shown as 'not_found_pending' until
  NOT_FOUND_AFTER consecutive failures)
* other HTTP status -> 'http_<code>' (not a failure)
* unparseable page -> 'parse_error' (a failure)
* other exception -> 'error' (not a failure)
* no stored currency but a stored raised amount -> 'currency_unknown'
* page shows only a bare "$" -> the stored currency is kept when it is a
  dollar currency, otherwise 'currency_unknown' (GoGetFunding only)
* currency differs and the goal does not match -> 'currency_mismatch'
  (same goal under another label means the stored label is wrong, so the
  platform's currency is adopted)
* raised fell by more than DROP_LIMIT -> 'suspicious_drop' (held)
"""
from __future__ import annotations

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

from .providers import Blocked, Fetcher, Figures, NotConfigured, ParseError, Provider

UA = 'TBEPSync/1.0 (+https://butterfly.joshcross.co.nz)'
NOT_FOUND_AFTER = 3            # consecutive failures before status 'not_found'
DROP_LIMIT = Decimal('0.20')   # hold the update if raised falls by more than this
TRANSIENT_HTTP = {500, 502, 503, 504}
# Currencies written with a bare "$", so a page showing only "$" can be one of them.
DOLLAR_CODES = {'USD', 'AUD', 'NZD', 'CAD', 'HKD', 'SGD'}


def http_fetch(url: str, headers: dict | None = None) -> str:
    req = urllib.request.Request(url, headers={'User-Agent': UA, **(headers or {})})
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


@dataclass
class Outcome:
    status: str
    figures: Figures | None
    failed: bool
    detail: str = ''   # why, for parse_error and error; printed, never stored


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
        except (ParseError, NotConfigured):
            raise
        except Exception:
            if attempt >= retries:
                raise
        attempt += 1
        sleep(5 * attempt)


def check(provider: Provider, row: Row, fetch: Fetcher, retries: int = 0,
          sleep: Callable[[float], None] | None = None) -> Outcome:
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
    except Exception as e:
        return Outcome('error', None, False, f'{type(e).__name__}: {e}')

    code = (row.currency_code or '').strip()
    raised, goal = row.raised, row.goal
    if fig.currency is None:
        if code not in DOLLAR_CODES:
            return Outcome('currency_unknown', None, False)
        fig = replace(fig, currency=code)
    if not code:
        if raised:
            return Outcome('currency_unknown', None, False)
    elif fig.currency != code:
        if goal is None or fig.goal != goal:
            return Outcome('currency_mismatch', None, False)
    if raised and raised > 0 and fig.raised < raised * (1 - DROP_LIMIT):
        return Outcome('suspicious_drop', None, False)
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


@dataclass
class RunResult:
    counts: Counter = field(default_factory=Counter)
    changed: int = 0
    exit_code: int = 0
    blocked: list[str] = field(default_factory=list)


def run(store, providers: list[Provider], *, apply: bool, limit: int = 0, after: str = '',
        fetch: Fetcher | None = None, retries: int = 1, out: Callable[[str], None] = print,
        sleep: Callable[[float], None] | None = None,
        jitter: Callable[[], float] | None = None) -> RunResult:
    # Resolved at call time (not as default arguments) so tests can patch them.
    fetch, sleep, jitter = fetch or http_fetch, sleep or time.sleep, jitter or random.random
    result = RunResult()
    active = []
    for p in providers:
        if p.configured():
            active.append(p)
        else:
            out(f'{p.name}: not configured, skipped')
    rows = store.select(active, limit)
    out(('APPLYING' if apply else 'DRY RUN') + f': {len(rows)} campaigns')

    blocked: set[str] = set()
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
            out(f'{row.id}: {outcome.status}' + (f' ({outcome.detail})' if outcome.detail else ''))
        if apply:
            if outcome.status == 'ok':
                store.record_ok(row.id, outcome.figures)
            else:
                store.record_problem(row.id, *shown_status(outcome, row.consecutive_failures))
        if provider.normalize(row.url) is not None:
            sleep(provider.delay + jitter() * 2)

    out(f'{dict(result.counts)} | changed: {result.changed}')
    if apply and result.changed and after:
        subprocess.run(shlex.split(after), check=False)
    return result
