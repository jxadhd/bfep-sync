"""GoGetFunding provider (off unless listed in SYNC_PROVIDERS).

The parser is Josh's gogetfunding-wip parser, adapted to the shared Figures
and ParseError. It reads the page's visible text rather than its CSS classes:
the campaign sidebar reads "US$7,352.00 raised of $50,000.00 goal" and the
headline count reads "277 Donors". The raised figure carries a currency
prefix ("US$"); the goal usually carries a bare "$".

When a page shows only a bare "$", the currency is returned as None and the
runner keeps the row's stored dollar currency (see runner.check()).

The test fixtures are built from the text shapes above, not saved pages.
Before enabling, dry-run it (`bfep sync --provider gogetfunding --limit 5`)
and compare the printed figures with the pages in a browser.
"""
from __future__ import annotations

import html
import re
from decimal import Decimal, InvalidOperation
from urllib.parse import urlparse

from .base import Fetcher, Figures, ParseError, Provider, validate_figures

HOSTS = {'gogetfunding.com', 'www.gogetfunding.com'}

# Currency prefix -> ISO code. None means the prefix alone does not identify the currency.
SYMBOLS = {'$': None, '£': 'GBP', '€': 'EUR',
           'US$': 'USD', 'A$': 'AUD', 'AU$': 'AUD', 'NZ$': 'NZD',
           'C$': 'CAD', 'CA$': 'CAD', 'HK$': 'HKD', 'S$': 'SGD', 'SG$': 'SGD'}

SCRIPTS = re.compile(r'<(script|style)\b.*?</\1\s*>', re.S | re.I)
TAGS = re.compile(r'<[^>]+>')
AMOUNT = r'(?<![A-Za-z])((?:[A-Za-z]{1,3})?[$£€¥])\s?(\d[\d,]*(?:\.\d+)?)'
WITH_GOAL = re.compile(AMOUNT + r'\s*raised of\s*' + AMOUNT + r'\s*goal', re.I)
NO_GOAL = re.compile(AMOUNT + r'\s*raised\b', re.I)
# Anchored to the "14% Funded" line. A bare "(\d+) Donors" also matches the tab bar, where the
# Updates count runs into the Donors label ("Updates 3 Donors 277 Followers 14").
DONORS = re.compile(r'\d+(?:\.\d+)?%\s*Funded\s*(\d[\d,]*)\s+Donors?\b')
DONORS_TAB = re.compile(r'\bDonors\s*(\d[\d,]*)')


def _text(page: str) -> str:
    page = TAGS.sub(' ', SCRIPTS.sub(' ', page))
    return re.sub(r'\s+', ' ', html.unescape(page))


def _amount(symbol: str, number: str) -> tuple[str, Decimal]:
    if symbol not in SYMBOLS:
        raise ParseError(f'unrecognised currency prefix {symbol!r}')
    try:
        return symbol, Decimal(number.replace(',', ''))
    except InvalidOperation as e:
        raise ParseError(f'bad amount: {e!r}')


def parse_gogetfunding(page: str) -> Figures:
    text = _text(page)

    # The sidebar block appears more than once on the page; every copy must agree.
    found = {(_amount(m[0], m[1]), _amount(m[2], m[3])) for m in WITH_GOAL.findall(text)}
    if found:
        if len(found) > 1:
            raise ParseError('conflicting raised/goal figures')
        (rsym, raised), (gsym, goal) = found.pop()
        if not rsym.endswith(gsym):          # "US$" with a bare "$" is fine; "£" with "$" is not
            raise ParseError('raised and goal use different currency symbols')
    else:
        found = {_amount(m[0], m[1]) for m in NO_GOAL.findall(text)}
        if not found:
            raise ParseError('no raised figure')
        if len(found) > 1:
            raise ParseError('conflicting raised figures')
        (rsym, raised), goal = found.pop(), None

    currency = SYMBOLS[rsym]
    validate_figures(raised, goal, currency)

    donors = DONORS.search(text) or DONORS_TAB.search(text)
    donations = int(donors.group(1).replace(',', '')) if donors else None
    return Figures(raised, goal, currency, donations)


class GoGetFunding(Provider):
    name = 'gogetfunding'
    url_pattern = 'gogetfunding[.]com/'
    delay = 6.0

    def normalize(self, raw: str | None) -> str | None:
        u = (raw or '').strip()
        if not u or ' ' in u:
            return None
        if not u.startswith(('http://', 'https://')):
            u = 'https://' + u
        p = urlparse(u)
        if p.hostname not in HOSTS or not p.path.strip('/'):
            return None
        return f'https://gogetfunding.com{p.path}'

    def fetch_figures(self, url: str, fetch: Fetcher) -> Figures:
        return parse_gogetfunding(fetch(url))
