"""Spotfund provider (off unless listed in SYNC_PROVIDERS).

A Spotfund page shows its figures in an "amount-raised-section" block,
which appears twice (desktop and mobile layouts) and reads
"$31,282 of $65,000 goal 479 Donors". Both copies must agree. The amounts
carry a bare "$", so the currency comes from the story's own currency_code
in the page's embedded data ("is_zero_fee":true,"currency_code":"USD").
Without it the currency is None and the runner keeps a stored dollar
currency (see runner.check()).

Stored links are short spot.fund/<code> links, which redirect, or
www.spotfund.com/story/<id> pages. Built from pages saved on 2026-10-12.
"""
from __future__ import annotations

import html
import re
from decimal import Decimal, InvalidOperation
from urllib.parse import urlparse

from .base import Fetcher, Figures, ParseError, Provider, validate_figures

HOSTS = {'spot.fund', 'www.spot.fund', 'spotfund.com', 'www.spotfund.com'}

SECTION = re.compile(r'data-testid="amount-raised-section"(.*?)Donors', re.S)
TAGS = re.compile(r'<[^>]+>')
NUMBER = r'(\d[\d,]*(?:\.\d+)?)'
FIGURES = re.compile(r'^\W*\$\s?' + NUMBER + r'(?:\s*of\s*\$\s?' + NUMBER + r'\s*goal)?\s*' + NUMBER + r'\s*$')
# The story's currency; donations in the same data carry their own currency_code fields.
CURRENCY = re.compile(r'is_zero_fee\\?":(?:true|false),\\?"currency_code\\?":\\?"([A-Z]{3})')


def _number(s: str | None) -> Decimal | None:
    if s is None:
        return None
    try:
        return Decimal(s.replace(',', ''))
    except InvalidOperation as e:
        raise ParseError(f'bad amount: {e!r}')


def parse_spotfund(page: str) -> Figures:
    found = set()
    for m in SECTION.finditer(page):
        text = re.sub(r'\s+', ' ', html.unescape(TAGS.sub(' ', '<' + m.group(1)))).strip()
        f = FIGURES.match(text)
        if not f:
            raise ParseError(f'unexpected amount section: {text[:80]!r}')
        found.add((_number(f[1]), _number(f[2]), int(f[3].replace(',', ''))))
    if not found:
        raise ParseError('no amount section')
    if len(found) > 1:
        raise ParseError('conflicting amount sections')
    raised, goal, donors = found.pop()

    codes = set(CURRENCY.findall(page))
    if len(codes) > 1:
        raise ParseError(f'conflicting currencies: {sorted(codes)}')
    currency = codes.pop() if codes else None

    validate_figures(raised, goal, currency)
    return Figures(raised, goal, currency, donors)


class Spotfund(Provider):
    name = 'spotfund'
    url_pattern = '(spot[.]fund|spotfund[.]com)/'
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
        return f'https://{p.hostname}{p.path}'

    def fetch_figures(self, url: str, fetch: Fetcher) -> Figures:
        return parse_spotfund(fetch(url))
