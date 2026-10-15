"""Steunactie provider (Belgian crowdfunding, off unless listed in SYNC_PROVIDERS).

A campaign page shows its figures as text: "€ 3.721 van € 30.000 opgehaald",
"12% van het doel" and "135 donaties", with the amounts repeated in a link to
the donations list. Amounts are written the Dutch way (3.721 is three
thousand, 1.234,56 has cents) and in euros. Every copy of the amounts must
agree. Built from the text of fundraiser 1830's page as read on 2026-10-15;
the HTML around it is not known, so tags are stripped before matching.

Links look like steunactie.be/actie/<name>/-<number>.
"""
from __future__ import annotations

import html
import re
from decimal import Decimal, InvalidOperation
from urllib.parse import urlparse

from .base import Fetcher, Figures, ParseError, Provider, validate_figures

HOSTS = {'steunactie.be', 'www.steunactie.be'}
TAGS = re.compile(r'<[^>]+>')
NUMBER = r'(\d{1,3}(?:\.\d{3})*(?:,\d{1,2})?|\d+(?:,\d{1,2})?)'
AMOUNTS = re.compile(r'€\s*' + NUMBER + r'\s+van\s+€\s*' + NUMBER)
RAISED_ONLY = re.compile(r'€\s*' + NUMBER + r'\s+opgehaald')
DONATIONS = re.compile(r'(\d[\d.]*)\s*donaties')


def _number(s: str) -> Decimal:
    try:
        return Decimal(s.replace('.', '').replace(',', '.'))
    except InvalidOperation:
        raise ParseError(f'bad amount: {s!r}')


def parse_steunactie(page: str) -> Figures:
    text = re.sub(r'\s+', ' ', html.unescape(TAGS.sub(' ', page)))
    found = {(_number(r), _number(g)) for r, g in AMOUNTS.findall(text)}
    if not found:
        found = {(_number(r), None) for r in RAISED_ONLY.findall(text)}
    if not found:
        raise ParseError('no amounts')
    if len(found) > 1:
        raise ParseError('conflicting amounts')
    raised, goal = found.pop()
    counts = {int(n.replace('.', '')) for n in DONATIONS.findall(text)}
    donations = counts.pop() if len(counts) == 1 else None
    validate_figures(raised, goal or None, 'EUR')
    return Figures(raised, goal or None, 'EUR', donations)


class Steunactie(Provider):
    name = 'steunactie'
    url_pattern = 'steunactie[.]be/'
    delay = 6.0

    def normalize(self, raw: str | None) -> str | None:
        u = (raw or '').strip()
        if not u or ' ' in u:
            return None
        if not u.startswith(('http://', 'https://')):
            u = 'https://' + u
        p = urlparse(u)
        if p.hostname not in HOSTS or not p.path.startswith('/actie/'):
            return None
        return f'https://steunactie.be{p.path}'

    def fetch_figures(self, url: str, fetch: Fetcher) -> Figures:
        return parse_steunactie(fetch(url))
