"""GoFundMe provider.

GoFundMe has no API for fundraisers that aren't your own (its official API,
developers.gofundme.com, is GoFundMe Pro's and only covers a nonprofit's own
campaigns). Its own website, though, loads fundraisers from a public GraphQL
API at graphql.gofundme.com with no key, and the figures in every page are a
copy of that API's answer (the page's __APOLLO_STATE__). So before the run
prefetch() asks that API for BATCH fundraisers per request, each under its
own alias (f0, f1, ...), and fetch_figures() serves rows from those answers.

Anything the API doesn't answer is read from the page exactly as before:
the API is down or refuses us, a fundraiser comes back null (a deleted one
then gets its real 404 from the page), or a link has no slug the API can
use. GOFUNDME_API=0 in /etc/bfep-sync.env turns the API off and reads every
page, which is the old behaviour.

Short gofund.me links don't contain the slug, so prefetch() first asks each
one where it redirects (a HEAD request, nothing downloaded) and reads the
slug from that. A short link that can't be resolved is read from its page.

Requests are paced here rather than by the runner (`delay` is 0): API_INTERVAL
between API requests and redirect lookups, and PAGE_INTERVAL plus 0-2 s of
jitter before each page, the same pace the page reader always had.

The page parser is the original gofundme.py logic, unchanged apart from moving
Figures/ParseError into providers.base: it reads the Apollo state embedded
in the page's __NEXT_DATA__ script and expects exactly one Fundraiser.
"""
from __future__ import annotations

import json
import os
import random
import re
import time
import urllib.error
import urllib.request
from decimal import Decimal
from typing import Callable
from urllib.parse import urljoin, urlparse

from .base import Fetcher, Figures, ParseError, Provider, validate_figures

NEXT_DATA = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)
HOSTS = {'gofund.me', 'www.gofund.me', 'gofundme.com', 'www.gofundme.com'}
SHORT_HOSTS = {'gofund.me', 'www.gofund.me'}
API = 'https://graphql.gofundme.com/graphql'
# Slugs are [A-Za-z0-9_-] only (see _slug), so they can't change the query.
FIELDS = 'currentAmount { amount currencyCode } goalAmount { amount currencyCode } donationCount'
SLUG = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_-]*$')
BATCH = 20
API_INTERVAL = 2.0    # seconds between API requests and redirect lookups
PAGE_INTERVAL = 6.0   # seconds (plus 0-2 s of jitter) before each page


def fundraiser_figures(f) -> Figures:
    """Figures from one Fundraiser object, from a page or from the API."""
    if not isinstance(f, dict):
        raise ParseError('unexpected fundraiser')
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
    return fundraiser_figures(found[0])


def _slug(url: str | None) -> str | None:
    """The fundraiser's slug from a gofundme.com link, or None.

    gofundme.com/f/<slug>, /f/<slug>/donate, /manage/<slug> and the older
    gofundme.com/<slug> all name the fundraiser by its slug.
    """
    p = urlparse(url or '')
    if p.hostname not in HOSTS - SHORT_HOSTS:
        return None
    parts = [s for s in p.path.split('/') if s]
    if len(parts) >= 2 and parts[0] in ('f', 'manage'):
        slug = parts[1]
    elif len(parts) == 1:
        slug = parts[0]
    else:
        return None
    return slug if SLUG.match(slug) else None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def http_resolve(url: str) -> str | None:
    """Where a short link redirects to (its Location header), or None.

    Follows at most three hops while they stay on gofund.me.
    """
    from ..runner import UA   # here, not at the top: runner imports providers
    opener = urllib.request.build_opener(_NoRedirect)
    for _ in range(3):
        req = urllib.request.Request(url, method='HEAD', headers={'User-Agent': UA})
        try:
            with opener.open(req, timeout=20):
                return None
        except urllib.error.HTTPError as e:
            if e.code not in (301, 302, 303, 307, 308) or not e.headers.get('Location'):
                raise
            url = urljoin(url, e.headers['Location'])
        if urlparse(url).hostname not in SHORT_HOSTS:
            return url
    return None


class GoFundMe(Provider):
    name = 'gofundme'
    url_pattern = '(gofund[.]me|gofundme[.]com)'
    delay = 0   # paced in _wait_turn instead

    def __init__(self, use_api: bool | None = None,
                 resolve: Callable[[str], str | None] | None = None,
                 clock: Callable[[], float] | None = None,
                 sleep: Callable[[float], None] | None = None,
                 jitter: Callable[[], float] | None = None):
        self.use_api = (use_api if use_api is not None
                        else os.environ.get('GOFUNDME_API', '1').strip() not in ('0', 'no', 'off', 'false'))
        self.resolve = resolve or http_resolve
        # Looked up when used (not bound here) so tests can patch time.sleep.
        self.clock = clock or (lambda: time.monotonic())
        self.sleep = sleep or (lambda s: time.sleep(s))
        self.jitter = jitter or (lambda: random.random())
        self._last: float | None = None
        self._slugs: dict[str, str] = {}      # short link -> slug
        self._answers: dict[str, dict] = {}   # slug -> Fundraiser object

    def normalize(self, raw: str | None) -> str | None:
        u = (raw or '').strip()
        if not u or ' ' in u:
            return None
        if not u.startswith(('http://', 'https://')):
            u = 'https://' + u
        p = urlparse(u)
        return u if p.hostname in HOSTS and p.path.strip('/') else None

    def _wait_turn(self, interval: float) -> None:
        if self._last is not None:
            gap = self.clock() - self._last
            if gap < interval:
                self.sleep(interval - gap)
        self._last = self.clock()

    def _resolve_short_links(self, urls: list[str]) -> None:
        for u in urls:
            self._wait_turn(API_INTERVAL)
            try:
                slug = _slug(self.resolve(u))
            except urllib.error.HTTPError as e:
                if e.code in (403, 405, 429):
                    return   # refused: leave the rest to their pages
                continue
            except Exception:
                continue
            if slug:
                self._slugs[u] = slug

    def _ask_api(self, slugs: list[str], fetch: Fetcher) -> None:
        empty = 0
        for start in range(0, len(slugs), BATCH):
            chunk = slugs[start:start + BATCH]
            query = '{ ' + ' '.join(f'f{i}: fundraiser(slug: {json.dumps(s)}) {{ {FIELDS} }}'
                                    for i, s in enumerate(chunk)) + ' }'
            self._wait_turn(API_INTERVAL)
            body = fetch(API, headers={'Content-Type': 'application/json',
                                       'Accept': 'application/json',
                                       'Origin': 'https://www.gofundme.com',
                                       'Referer': 'https://www.gofundme.com/'},
                         data=json.dumps({'query': query}).encode())
            try:
                data = json.loads(body)
            except ValueError:
                raise ParseError('API response is not JSON')
            d = data.get('data') if isinstance(data, dict) else None
            if not isinstance(d, dict) or not any(isinstance(d.get(f'f{i}'), dict) for i in range(len(chunk))):
                # One empty batch can be 20 deleted fundraisers; two in a row
                # means the API isn't answering us.
                empty += 1
                if empty < 2:
                    continue
                err = ((data.get('errors') or [{}])[0] or {}).get('message', 'no data') if isinstance(data, dict) else 'no data'
                raise ParseError(f'API answered nothing usable: {str(err)[:80]}')
            empty = 0
            for i, s in enumerate(chunk):
                if isinstance(d.get(f'f{i}'), dict):
                    self._answers[s] = d[f'f{i}']

    def prefetch(self, urls: list[str], fetch: Fetcher) -> str | None:
        if not self.use_api:
            return None
        short = list(dict.fromkeys(u for u in urls if urlparse(u).hostname in SHORT_HOSTS))
        self._resolve_short_links(short)
        slugs = list(dict.fromkeys(s for u in urls if (s := self._slugs.get(u) or _slug(u))))
        problem = ''
        try:
            self._ask_api(slugs, fetch)
        except Exception as e:
            problem = f'; API stopped ({type(e).__name__}: {e})'
        answered = sum(1 for u in dict.fromkeys(urls) if (self._slugs.get(u) or _slug(u)) in self._answers)
        return (f'API answered {answered} of {len(set(urls))} fundraisers, '
                f'the rest are read from their pages{problem}')

    def fetch_figures(self, url: str, fetch: Fetcher) -> Figures:
        slug = self._slugs.get(url) or _slug(url)
        if slug in self._answers:
            try:
                return fundraiser_figures(self._answers[slug])
            except ParseError:
                pass   # odd answer: the page decides
        self._wait_turn(PAGE_INTERVAL + self.jitter() * 2)
        return parse_gofundme(fetch(url))
