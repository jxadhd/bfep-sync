"""Chuffed provider (off unless listed in SYNC_PROVIDERS and CHUFFED_TOKEN is set).

Chuffed's pages load their figures with JavaScript, so this reads Chuffed's
private GraphQL API instead (https://dev-portal.chuffed.org/graphdoc), which
Chuffed opened for our server in October 2026. Every request carries the
token they issued in an x-chuffed-token header; it lives in CHUFFED_TOKEN in
/etc/bfep-sync.env and never in this repo. Without it the provider reports
itself as not configured and the runner skips its rows.

Chuffed allows 10 requests a minute, so before the run prefetch() asks for
BATCH campaigns per request, each under its own alias (c0, c1, ...) so every
answer maps back to its link. About 205 campaigns take 11 requests. A
campaign missing from the batches is fetched on its own. Every request,
batched or single, waits for MIN_INTERVAL since the last one, so the
provider stays under the limit on its own (its `delay` is 0, so the runner
doesn't add waits for campaigns already answered).

A campaign is looked up by the number at the start of its link
(chuffed.org/project/120680-a-mothers-plea -> 120680), or by its name when
the link has no number (chuffed.org/project/babygharam). The API answers
{"campaign": {"target": {"amount": "3500000", "currency": "USD"},
"collected": {...}, "donations": {"totalCount": 569}}}, with amounts in
cents. A campaign the API doesn't know comes back as null (with an "Invalid
query" error) and is treated like a 404 page, so the usual not_found rule
applies.
"""
from __future__ import annotations

import io
import json
import os
import re
import time
import urllib.error
from decimal import Decimal, InvalidOperation
from typing import Callable
from urllib.parse import urlparse

from .base import Fetcher, Figures, NotConfigured, ParseError, Provider, validate_figures

API = 'https://chuffed.org/api/graphql'
HOSTS = {'chuffed.org', 'www.chuffed.org'}
PATH = re.compile(r'^/project/([A-Za-z0-9][A-Za-z0-9_-]*)/?$')
NUMBER = re.compile(r'^(\d+)(?:-|$)')
# Keys are digits or slugs of [A-Za-z0-9_-] only, so they can't change the query.
FIELDS = 'slug status target { amount currency } collected { amount currency } donations { totalCount }'
BATCH = 20
# ISO 4217 currencies without minor units: their amounts are not in cents.
ZERO_DECIMAL = {'BIF', 'CLP', 'DJF', 'GNF', 'ISK', 'JPY', 'KMF', 'KRW', 'PYG', 'RWF',
                'UGX', 'VND', 'VUV', 'XAF', 'XOF', 'XPF'}
MIN_INTERVAL = 6.5   # seconds between requests: at most ~9 a minute, under Chuffed's 10


def _key(path: str) -> str | None:
    """The campaign number, or the slug when the link has no number."""
    m = PATH.match(path)
    if not m:
        return None
    n = NUMBER.match(m.group(1))
    return n.group(1) if n else m.group(1)


def _money(node, what: str) -> tuple[Decimal, str]:
    if not isinstance(node, dict):
        raise ParseError(f'no {what}')
    code = node.get('currency')
    if not isinstance(code, str):
        raise ParseError(f'no {what} currency')
    try:
        amount = Decimal(str(node.get('amount')))
    except InvalidOperation:
        raise ParseError(f'bad {what} amount')
    if not amount.is_finite():
        raise ParseError(f'bad {what} amount')
    return (amount if code in ZERO_DECIMAL else amount / 100), code


def campaign_figures(campaign, url: str = API) -> Figures:
    """Figures from one campaign object; None means Chuffed doesn't know it."""
    if campaign is None:
        raise urllib.error.HTTPError(url, 404, 'campaign not found', {}, io.BytesIO())
    if not isinstance(campaign, dict):
        raise ParseError('unexpected campaign')
    raised, code = _money(campaign.get('collected'), 'raised')
    goal, goal_code = _money(campaign.get('target'), 'goal')
    if goal_code != code:
        raise ParseError(f'raised in {code} but goal in {goal_code}')
    count = (campaign.get('donations') or {}).get('totalCount')
    donations = count if isinstance(count, int) else None
    validate_figures(raised, goal or None, code)
    return Figures(raised, goal or None, code, donations)


def _response(body: str) -> dict:
    try:
        data = json.loads(body)
    except ValueError:
        raise ParseError('response is not JSON')
    if not isinstance(data, dict):
        raise ParseError('unexpected response')
    return data


def _error(data: dict) -> str:
    return str(((data.get('errors') or [{}])[0] or {}).get('message', 'error'))[:80]


def parse_chuffed(body: str, url: str = API) -> Figures:
    """Figures from a single campaign(id:) response."""
    data = _response(body)
    d = data.get('data')
    if isinstance(d, dict) and 'campaign' in d:
        return campaign_figures(d['campaign'], url)
    raise ParseError(f'API error: {_error(data)}' if data.get('errors') else 'no campaign in response')


class Chuffed(Provider):
    name = 'chuffed'
    url_pattern = 'chuffed[.]org/'
    delay = 0   # paced in _wait_turn instead

    def __init__(self, token: str | None = None,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep):
        self.token = token if token is not None else os.environ.get('CHUFFED_TOKEN', '')
        self.clock, self.sleep = clock, sleep
        self._last: float | None = None
        self._answers: dict[str, object] = {}

    def configured(self) -> bool:
        return bool(self.token.strip())

    def normalize(self, raw: str | None) -> str | None:
        u = (raw or '').strip()
        if not u or ' ' in u:
            return None
        if not u.startswith(('http://', 'https://')):
            u = 'https://' + u
        p = urlparse(u)
        key = _key(p.path)
        if p.hostname not in HOSTS or key is None:
            return None
        return f'https://chuffed.org/project/{key}'

    def _wait_turn(self) -> None:
        if self._last is not None:
            gap = self.clock() - self._last
            if gap < MIN_INTERVAL:
                self.sleep(MIN_INTERVAL - gap)
        self._last = self.clock()

    def _post(self, query: str, fetch: Fetcher) -> str:
        if not self.configured():
            raise NotConfigured('CHUFFED_TOKEN is not set')
        self._wait_turn()
        return fetch(API, headers={'x-chuffed-token': self.token.strip(),
                                   'Content-Type': 'application/json',
                                   'Accept': 'application/json'},
                     data=json.dumps({'query': query}).encode())

    def prefetch(self, urls: list[str], fetch: Fetcher) -> None:
        keys = list(dict.fromkeys(k for k in (_key(urlparse(u).path) for u in urls) if k))
        for start in range(0, len(keys), BATCH):
            chunk = keys[start:start + BATCH]
            query = '{ ' + ' '.join(f'c{i}: campaign(id: {json.dumps(k)}) {{ {FIELDS} }}'
                                    for i, k in enumerate(chunk)) + ' }'
            data = _response(self._post(query, fetch))
            d = data.get('data')
            if not isinstance(d, dict):
                raise ParseError(f'API error: {_error(data)}')
            answers = {k: d.get(f'c{i}') for i, k in enumerate(chunk) if f'c{i}' in d}
            # All null with an error is more likely a rejected query than a
            # batch of vanished campaigns: leave those to single requests.
            if data.get('errors') and not any(answers.values()):
                continue
            self._answers.update(answers)

    def fetch_figures(self, url: str, fetch: Fetcher) -> Figures:
        key = _key(urlparse(url).path)
        if key is None:
            raise ParseError('no campaign in URL')
        if key in self._answers:
            return campaign_figures(self._answers.pop(key), url)
        query = '{ campaign(id: %s) { %s } }' % (json.dumps(key), FIELDS)
        return parse_chuffed(self._post(query, fetch), url)
