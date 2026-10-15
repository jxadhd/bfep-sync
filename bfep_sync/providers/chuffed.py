"""Chuffed provider (off unless listed in SYNC_PROVIDERS and CHUFFED_TOKEN is set).

Chuffed's pages load their figures with JavaScript, so this reads Chuffed's
private GraphQL API instead (https://dev-portal.chuffed.org/graphdoc), which
Chuffed opened for our server in October 2026. Every request carries the
token they issued in an x-chuffed-token header; it lives in CHUFFED_TOKEN in
/etc/bfep-sync.env and never in this repo. Without it the provider reports
itself as not configured and the runner skips its rows.

Chuffed allows 10 requests a minute. The runner already waits `delay` (plus
up to 2 s) after each campaign, and fetch_figures also keeps at least
MIN_INTERVAL between its own requests, so retries can't go over the limit.

A campaign is looked up by the number at the start of its link
(chuffed.org/project/120680-a-mothers-plea -> 120680). The API answers
{"campaign": {"target": {"amount": "3500000", "currency": "USD"},
"collected": {...}, "donations": {"totalCount": 569}}}, with amounts in
cents. A campaign the API doesn't know is treated like a 404 page, so the
usual not_found rule applies.
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
PATH = re.compile(r'^/project/(\d+)(?:[-/].*)?$')
# Built from digits only, so the ID can't change the query.
QUERY = ('{ campaign(id: "%s") { id slug title status target { amount currency } '
         'collected { amount currency } donations { totalCount } } }')
# ISO 4217 currencies without minor units: their amounts are not in cents.
ZERO_DECIMAL = {'BIF', 'CLP', 'DJF', 'GNF', 'ISK', 'JPY', 'KMF', 'KRW', 'PYG', 'RWF',
                'UGX', 'VND', 'VUV', 'XAF', 'XOF', 'XPF'}
MIN_INTERVAL = 6.5   # seconds between requests: at most ~9 a minute, under Chuffed's 10


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


def parse_chuffed(body: str, url: str = API) -> Figures:
    try:
        data = json.loads(body)
    except ValueError:
        raise ParseError('response is not JSON')
    if not isinstance(data, dict):
        raise ParseError('unexpected response')
    if data.get('errors'):
        msg = str((data['errors'][0] or {}).get('message', 'error'))[:80]
        raise ParseError(f'API error: {msg}')
    campaign = (data.get('data') or {}).get('campaign')
    if campaign is None:
        raise urllib.error.HTTPError(url, 404, 'campaign not found', {}, io.BytesIO())
    raised, code = _money(campaign.get('collected'), 'raised')
    goal, goal_code = _money(campaign.get('target'), 'goal')
    if goal_code != code:
        raise ParseError(f'raised in {code} but goal in {goal_code}')
    count = (campaign.get('donations') or {}).get('totalCount')
    donations = count if isinstance(count, int) else None
    validate_figures(raised, goal or None, code)
    return Figures(raised, goal or None, code, donations)


class Chuffed(Provider):
    name = 'chuffed'
    url_pattern = 'chuffed[.]org/'
    delay = MIN_INTERVAL

    def __init__(self, token: str | None = None,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep):
        self.token = token if token is not None else os.environ.get('CHUFFED_TOKEN', '')
        self.clock, self.sleep = clock, sleep
        self._last: float | None = None

    def configured(self) -> bool:
        return bool(self.token.strip())

    def normalize(self, raw: str | None) -> str | None:
        u = (raw or '').strip()
        if not u or ' ' in u:
            return None
        if not u.startswith(('http://', 'https://')):
            u = 'https://' + u
        p = urlparse(u)
        m = PATH.match(p.path)
        if p.hostname not in HOSTS or not m:
            return None
        return f'https://chuffed.org/project/{m.group(1)}'

    def _wait_turn(self) -> None:
        if self._last is not None:
            gap = self.clock() - self._last
            if gap < MIN_INTERVAL:
                self.sleep(MIN_INTERVAL - gap)
        self._last = self.clock()

    def fetch_figures(self, url: str, fetch: Fetcher) -> Figures:
        if not self.configured():
            raise NotConfigured('CHUFFED_TOKEN is not set')
        m = PATH.match(urlparse(url).path)
        if not m:
            raise ParseError('no campaign number in URL')
        self._wait_turn()
        body = fetch(API, headers={'x-chuffed-token': self.token.strip(),
                                   'Content-Type': 'application/json',
                                   'Accept': 'application/json'},
                     data=json.dumps({'query': QUERY % m.group(1)}).encode())
        return parse_chuffed(body, url)
