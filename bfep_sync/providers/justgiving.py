"""JustGiving provider (off unless JUSTGIVING_APP_ID is set).

Uses JustGiving's public REST API rather than scraping, which needs a free
application ID from https://developer.justgiving.com/. Without one the
provider reports itself as not configured and the runner skips its rows.

UNVERIFIED: written from the published API shape without live access. Before
enabling, run a dry run (`python -m bfep_sync --provider justgiving --limit 3`)
and compare the printed figures with the pages in a browser.
"""
from __future__ import annotations

import json
import os
import re
from decimal import Decimal, InvalidOperation
from urllib.parse import quote, urlparse

from .base import Fetcher, Figures, NotConfigured, ParseError, Provider, validate_figures

HOSTS = {'justgiving.com', 'www.justgiving.com'}
API = 'https://api.justgiving.com/{app_id}/v1/fundraising/pages/{short_name}'
# /page/<short-name>, /fundraising/<short-name> or a bare /<short-name>.
PATH = re.compile(r'^/(?:page/|fundraising/)?([A-Za-z0-9][A-Za-z0-9._-]*)/?$')


class JustGiving(Provider):
    name = 'justgiving'
    url_pattern = 'justgiving[.]com/'
    delay = 2.0

    def __init__(self, app_id: str | None = None):
        self.app_id = app_id if app_id is not None else os.environ.get('JUSTGIVING_APP_ID', '')

    def configured(self) -> bool:
        return bool(self.app_id.strip())

    def normalize(self, raw: str | None) -> str | None:
        u = (raw or '').strip()
        if not u or ' ' in u:
            return None
        if not u.startswith(('http://', 'https://')):
            u = 'https://' + u
        p = urlparse(u)
        if p.hostname not in HOSTS or not PATH.match(p.path):
            return None
        return f'https://www.justgiving.com{p.path.rstrip("/")}'

    def short_name(self, url: str) -> str:
        m = PATH.match(urlparse(url).path)
        if not m:
            raise ParseError('no page short name in URL')
        return m.group(1)

    def fetch_figures(self, url: str, fetch: Fetcher) -> Figures:
        if not self.configured():
            raise NotConfigured('JUSTGIVING_APP_ID is not set')
        api_url = API.format(app_id=quote(self.app_id.strip(), safe=''), short_name=quote(self.short_name(url), safe=''))
        return parse_justgiving(fetch(api_url, headers={'Accept': 'application/json'}))


def parse_justgiving(body: str) -> Figures:
    try:
        data = json.loads(body)
    except ValueError as e:
        raise ParseError(f'not JSON: {e!r}')
    if not isinstance(data, dict):
        raise ParseError('unexpected JSON shape')
    try:
        raised = Decimal(str(data.get('grandTotalRaisedExcludingGiftAid', data['totalRaisedOnline'])))
        target = data.get('fundraisingTarget')
        goal = Decimal(str(target)) if target not in (None, '', 0, '0') else None
        currency = str(data['currencyCode']).upper()
    except (KeyError, TypeError, InvalidOperation) as e:
        raise ParseError(f'bad amount fields: {e!r}')
    validate_figures(raised, goal, currency)
    return Figures(raised, goal, currency, None)
