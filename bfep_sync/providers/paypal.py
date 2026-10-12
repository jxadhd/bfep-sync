"""PayPal fundraiser provider (off unless listed in SYNC_PROVIDERS).

Only PayPal fundraiser links (paypal.com/donate?campaign_id=...) carry
figures. paypal.me profiles and plain donate buttons (hosted_button_id) show
no total, so url_pattern leaves them out and the sync never touches them.

A fundraiser page embeds its figures in the page data, next to the
campaign's productId:
"productId":"A4PQB5UYBA7T6",...,"campaignTargetAmount":"50000.00",
"campaignStatus":"INACTIVE","campaignTotalAmountRaised":"10952.56",
"campaignTotalNumberOfTxn":203, with the currency in "donationCode":"USD".
The productId must match the link's campaign_id. Closed campaigns
(INACTIVE) still show their final figures and are synced like any other.
Field names are from fundraiser 1972's page as fetched from the server on
2026-10-12; the test fixture is built from them, not saved from the site.
"""
from __future__ import annotations

import json
import re
from decimal import Decimal, InvalidOperation
from urllib.parse import parse_qs, urlparse

from .base import Fetcher, Figures, ParseError, Provider, validate_figures

HOSTS = {'paypal.com', 'www.paypal.com'}
CAMPAIGN_ID = re.compile(r'[A-Z0-9]{8,20}')


def _field(page: str, name: str) -> str | None:
    """The one value of a "name": field, as text; None when absent."""
    values = {m.group(1) for m in re.finditer(r'"' + name + r'":\s*("(?:[^"\\]|\\.)*"|-?\d+(?:\.\d+)?|null)', page)}
    if len(values) > 1:
        raise ParseError(f'conflicting {name} values')
    if not values:
        return None
    value = json.loads(values.pop())
    return None if value is None else str(value)


def _amount(s: str | None) -> Decimal | None:
    if s is None or s.strip() == '':
        return None
    try:
        return Decimal(s.replace(',', ''))
    except InvalidOperation:
        raise ParseError(f'bad amount: {s[:20]!r}')


def parse_paypal(page: str, campaign_id: str) -> Figures:
    product = _field(page, 'productId')
    if product is None:
        raise ParseError('no campaign data')
    if product != campaign_id:
        raise ParseError(f'page is for campaign {product[:20]!r}')
    raised = _amount(_field(page, 'campaignTotalAmountRaised'))
    if raised is None:
        raise ParseError('no raised amount')
    goal = _amount(_field(page, 'campaignTargetAmount'))
    if goal == 0:
        goal = None
    currency = _field(page, 'donationCode') or None
    count = _field(page, 'campaignTotalNumberOfTxn')
    donations = int(count) if count and count.isdigit() else None
    validate_figures(raised, goal, currency)
    return Figures(raised, goal, currency, donations)


class PayPal(Provider):
    name = 'paypal'
    url_pattern = r'paypal[.]com/donate/?[?](.*&)?campaign_id='
    delay = 6.0

    def normalize(self, raw: str | None) -> str | None:
        u = (raw or '').strip()
        if not u or ' ' in u:
            return None
        if not u.startswith(('http://', 'https://')):
            u = 'https://' + u
        p = urlparse(u)
        if p.hostname not in HOSTS or p.path.rstrip('/') != '/donate':
            return None
        ids = parse_qs(p.query).get('campaign_id', [])
        if len(ids) != 1 or not CAMPAIGN_ID.fullmatch(ids[0]):
            return None
        return f'https://www.paypal.com/donate/?campaign_id={ids[0]}'

    def fetch_figures(self, url: str, fetch: Fetcher) -> Figures:
        campaign_id = parse_qs(urlparse(url).query)['campaign_id'][0]
        return parse_paypal(fetch(url), campaign_id)
