"""GoFundMe provider.

The parser is the original gofundme.py logic, unchanged apart from moving
Figures/ParseError into providers.base: it reads the Apollo state embedded
in the page's __NEXT_DATA__ script and expects exactly one Fundraiser.
"""
from __future__ import annotations

import json
import re
from decimal import Decimal
from urllib.parse import urlparse

from .base import Fetcher, Figures, ParseError, Provider, validate_figures

NEXT_DATA = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)
HOSTS = {'gofund.me', 'www.gofund.me', 'gofundme.com', 'www.gofundme.com'}


def parse_gofundme(html: str) -> Figures:
    m = NEXT_DATA.search(html)
    if not m:
        raise ParseError('no __NEXT_DATA__ block')
    try:
        state = json.loads(m.group(1))['props']['pageProps']['__APOLLO_STATE__']
    except (KeyError, TypeError, ValueError) as e:
        raise ParseError(f'no apollo state: {e!r}')

    found = [v for k, v in state.items()
             if k.startswith('Fundraiser:') and isinstance(v, dict) and 'currentAmount' in v]
    if len(found) != 1:
        raise ParseError(f'expected 1 fundraiser, found {len(found)}')
    f = found[0]

    try:
        raised = Decimal(str(f['currentAmount']['amount']))
        currency = str(f['currentAmount']['currencyCode'])
        goal = f.get('goalAmount')
        goal = Decimal(str(goal['amount'])) if goal else None
    except (KeyError, TypeError, ArithmeticError) as e:
        raise ParseError(f'bad amount fields: {e!r}')

    validate_figures(raised, goal, currency)

    donations = f.get('donationCount')
    return Figures(raised, goal, currency, donations if isinstance(donations, int) else None)


class GoFundMe(Provider):
    name = 'gofundme'
    url_pattern = '(gofund[.]me|gofundme[.]com)'
    delay = 6.0

    def normalize(self, raw: str | None) -> str | None:
        u = (raw or '').strip()
        if not u or ' ' in u:
            return None
        if not u.startswith(('http://', 'https://')):
            u = 'https://' + u
        p = urlparse(u)
        return u if p.hostname in HOSTS and p.path.strip('/') else None

    def fetch_figures(self, url: str, fetch: Fetcher) -> Figures:
        return parse_gofundme(fetch(url))
