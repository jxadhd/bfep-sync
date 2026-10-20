"""Tests for the sync service. Standard library only; no network, no database.

    python -m unittest discover -s tests -v
"""
from __future__ import annotations

import io
import json
import pathlib
import re
import sys
import unittest
import urllib.error
from unittest import mock
from decimal import Decimal

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'original'))

from bfep_sync import providers as registry  # noqa: E402
from bfep_sync.providers import Blocked, Figures, ParseError  # noqa: E402
from bfep_sync.providers.chuffed import MIN_INTERVAL, Chuffed, parse_chuffed  # noqa: E402
from bfep_sync.providers.gofundme import GoFundMe, parse_gofundme  # noqa: E402
from bfep_sync.providers.gogetfunding import GoGetFunding, parse_gogetfunding  # noqa: E402
from bfep_sync.providers.justgiving import JustGiving, parse_justgiving  # noqa: E402
from bfep_sync.providers.paypal import PayPal, parse_paypal  # noqa: E402
from bfep_sync.providers.spotfund import Spotfund, parse_spotfund  # noqa: E402
from bfep_sync.providers.steunactie import Steunactie, parse_steunactie  # noqa: E402
from bfep_sync.runner import Row, check, describe_held, run, shown_status  # noqa: E402

FIXTURES = ROOT / 'tests' / 'fixtures'


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text()


def http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError('https://example.org', code, 'x', {}, io.BytesIO())


class FakeFetch:
    """Returns queued bodies or raises queued exceptions, recording URLs."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.urls = []

    def __call__(self, url, headers=None, data=None):
        self.urls.append(url)
        self.headers, self.data = headers, data
        item = self.responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def pages_only() -> GoFundMe:
    """GoFundMe reading pages only, as before the API, with no real waits."""
    return GoFundMe(use_api=False, sleep=lambda s: None)


def row(**kw) -> Row:
    base = dict(id=1, url='https://www.gofundme.com/f/help', currency_code='USD',
                goal=Decimal('50000'), raised=Decimal('12000'), consecutive_failures=0)
    base.update(kw)
    return Row(**base)


class GoFundMeParserTest(unittest.TestCase):

    def test_reads_figures(self):
        fig = parse_gofundme(fixture('gofundme_ok.html'))
        self.assertEqual(fig, Figures(Decimal('12345.5'), Decimal('50000'), 'USD', 321))

    def test_missing_goal(self):
        fig = parse_gofundme(fixture('gofundme_no_goal.html'))
        self.assertEqual((fig.raised, fig.goal, fig.currency), (Decimal('800'), None, 'EUR'))

    def test_rejects_bad_pages(self):
        for name in ('gofundme_no_next_data.html', 'gofundme_two_fundraisers.html', 'gofundme_implausible.html'):
            with self.subTest(name=name), self.assertRaises(ParseError):
                parse_gofundme(fixture(name))

    def test_matches_original_parser(self):
        """The refactored parser returns the same figures as the original file."""
        import gofundme as original  # original/gofundme.py
        for name in ('gofundme_ok.html', 'gofundme_no_goal.html'):
            with self.subTest(name=name):
                old = original.parse_gofundme(fixture(name))
                new = parse_gofundme(fixture(name))
                self.assertEqual((old.raised, old.goal, old.currency, old.donations),
                                 (new.raised, new.goal, new.currency, new.donations))
        for name in ('gofundme_no_next_data.html', 'gofundme_two_fundraisers.html', 'gofundme_implausible.html'):
            with self.subTest(name=name):
                with self.assertRaises(original.ParseError):
                    original.parse_gofundme(fixture(name))


class GoFundMeNormalizeTest(unittest.TestCase):

    def test_normalize(self):
        p = GoFundMe()
        self.assertEqual(p.normalize('gofundme.com/f/help'), 'https://gofundme.com/f/help')
        self.assertEqual(p.normalize(' https://gofund.me/abc '), 'https://gofund.me/abc')
        for bad in (None, '', 'https://gofundme.com/', 'https://evil.com/gofundme.com/f/x', 'gofundme.com/f/a b'):
            with self.subTest(bad=bad):
                self.assertIsNone(p.normalize(bad))


def gfm_node(raised=12345.5, goal=50000, currency='USD', count=321):
    """A Fundraiser as graphql.gofundme.com answers it (same shape as the page's Apollo state)."""
    return {'currentAmount': {'amount': raised, 'currencyCode': currency},
            'goalAmount': {'amount': goal, 'currencyCode': currency} if goal is not None else None,
            'donationCount': count}


def gfm_batch(*nodes, errors=None):
    body = {'data': {f'f{i}': n for i, n in enumerate(nodes)}}
    if errors:
        body['errors'] = [{'message': errors}]
    return json.dumps(body)


class GoFundMeApiTest(unittest.TestCase):

    def provider(self, redirects=None, waits=None):
        now = [0.0]

        def sleep(s):
            if waits is not None:
                waits.append(s)
            now[0] += s

        def resolve(u):
            r = (redirects or {})[u]
            if isinstance(r, BaseException):
                raise r
            return r
        return GoFundMe(use_api=True, resolve=resolve, clock=lambda: now[0], sleep=sleep, jitter=lambda: 0)

    def test_slugs_from_links(self):
        from bfep_sync.providers.gofundme import _slug
        self.assertEqual(_slug('https://www.gofundme.com/f/help-gaza-family?attribution_id=x'), 'help-gaza-family')
        self.assertEqual(_slug('https://gofundme.com/f/help/donate'), 'help')
        self.assertEqual(_slug('https://www.gofundme.com/manage/help'), 'help')
        self.assertEqual(_slug('https://www.gofundme.com/old-style-slug'), 'old-style-slug')
        for bad in ('https://gofund.me/abc123', 'https://www.gofundme.com/', 'https://www.gofundme.com/c/blog/x',
                    'https://www.gofundme.com/f/"x"', 'https://evil.com/f/help', None):
            with self.subTest(bad=bad):
                self.assertIsNone(_slug(bad))

    def test_prefetch_asks_for_many_fundraisers_in_one_request(self):
        p = self.provider()
        urls = ['https://www.gofundme.com/f/one', 'https://gofundme.com/f/two/donate', 'https://www.gofundme.com/f/one']
        fetch = FakeFetch(gfm_batch(gfm_node(), gfm_node(raised=10, goal=None, currency='EUR', count=None)))
        note = p.prefetch(urls, fetch)
        self.assertEqual(fetch.urls, ['https://graphql.gofundme.com/graphql'])
        query = json.loads(fetch.data)['query']
        self.assertIn('f0: fundraiser(slug: "one")', query)
        self.assertIn('f1: fundraiser(slug: "two")', query)
        self.assertNotIn('f2', query)
        self.assertEqual(p.fetch_figures(urls[0], fetch), Figures(Decimal('12345.5'), Decimal('50000'), 'USD', 321))
        self.assertEqual(p.fetch_figures(urls[1], fetch), Figures(Decimal('10'), None, 'EUR', None))
        self.assertEqual(p.fetch_figures(urls[2], fetch).raised, Decimal('12345.5'))
        self.assertEqual(len(fetch.urls), 1)
        self.assertEqual(note, 'API answered 2 of 2 fundraisers, the rest are read from their pages')

    def test_api_and_page_give_the_same_figures(self):
        p = self.provider()
        url = 'https://www.gofundme.com/f/help'
        p.prefetch([url], FakeFetch(gfm_batch(gfm_node())))
        self.assertEqual(p.fetch_figures(url, FakeFetch()), parse_gofundme(fixture('gofundme_ok.html')))

    def test_unanswered_and_odd_answers_are_read_from_the_page(self):
        p = self.provider()
        urls = ['https://www.gofundme.com/f/gone', 'https://www.gofundme.com/f/odd', 'https://www.gofundme.com/f/fine']
        p.prefetch(urls, FakeFetch(gfm_batch(None, gfm_node(currency='usd'), gfm_node(), errors='not found')))
        fetch = FakeFetch(http_error(404), fixture('gofundme_ok.html'))
        o = check(p, row(url=urls[0]), fetch)
        self.assertEqual((o.status, o.failed), ('not_found', True))
        self.assertEqual(p.fetch_figures(urls[1], fetch).currency, 'USD')
        self.assertEqual(fetch.urls, urls[:2])

    def test_short_links_are_resolved_to_their_slug(self):
        p = self.provider(redirects={
            'https://gofund.me/abc': 'https://www.gofundme.com/f/help-family?attribution_id=sl:1',
            'https://gofund.me/dead': http_error(404),
            'https://gofund.me/odd': None})
        urls = ['https://gofund.me/abc', 'https://gofund.me/dead', 'https://gofund.me/odd']
        fetch = FakeFetch(gfm_batch(gfm_node(raised=5)))
        p.prefetch(urls, fetch)
        self.assertIn('f0: fundraiser(slug: "help-family")', json.loads(fetch.data)['query'])
        self.assertNotIn('f1', json.loads(fetch.data)['query'])
        self.assertEqual(p.fetch_figures(urls[0], fetch).raised, Decimal('5'))
        page = FakeFetch(fixture('gofundme_ok.html'))
        self.assertEqual(p.fetch_figures(urls[2], page).raised, Decimal('12345.5'))
        self.assertEqual(page.urls, ['https://gofund.me/odd'])

    def test_refused_redirect_lookups_stop(self):
        calls = []
        p = self.provider()
        p.resolve = lambda u: (calls.append(u), (_ for _ in ()).throw(http_error(429)))[1]
        p.prefetch(['https://gofund.me/a', 'https://gofund.me/b'], FakeFetch())
        self.assertEqual(calls, ['https://gofund.me/a'])

    def test_api_that_answers_nothing_falls_back_to_pages(self):
        p = self.provider()
        urls = [f'https://www.gofundme.com/f/x{n}' for n in range(60)]
        fetch = FakeFetch('{"errors":[{"message":"Cannot query field"}]}', '<html>blocked</html>')
        note = p.prefetch(urls, fetch)
        self.assertEqual(len(fetch.urls), 2)   # gave up after two empty batches
        self.assertIn('API answered 0 of 60', note)
        self.assertIn('API stopped (ParseError', note)
        page = FakeFetch(fixture('gofundme_ok.html'))
        self.assertEqual(p.fetch_figures(urls[0], page).raised, Decimal('12345.5'))

    def test_one_empty_batch_is_not_the_end(self):
        p = self.provider()
        urls = [f'https://www.gofundme.com/f/x{n}' for n in range(30)]
        p.prefetch(urls, FakeFetch(gfm_batch(*[None] * 20), gfm_batch(*[gfm_node()] * 10)))
        self.assertEqual(p.fetch_figures(urls[29], FakeFetch()).raised, Decimal('12345.5'))

    def test_api_http_error_does_not_block_pages(self):
        p = self.provider()
        url = 'https://www.gofundme.com/f/help'
        note = p.prefetch([url], FakeFetch(http_error(403)))
        self.assertIn('API stopped (HTTPError', note)
        self.assertEqual(check(p, row(url=url), FakeFetch(fixture('gofundme_ok.html'))).status, 'ok')

    def test_paces_api_requests_and_pages(self):
        waits = []
        p = self.provider(waits=waits)
        urls = [f'https://www.gofundme.com/f/x{n}' for n in range(25)]
        p.prefetch(urls, FakeFetch(gfm_batch(*[gfm_node()] * 20), gfm_batch(*[None] * 5)))
        fetch = FakeFetch(fixture('gofundme_ok.html'), fixture('gofundme_ok.html'))
        p.fetch_figures(urls[0], fetch)    # answered: no wait
        p.fetch_figures(urls[20], fetch)
        p.fetch_figures(urls[21], fetch)
        self.assertEqual(waits, [2.0, 6.0, 6.0])

    def test_api_off_reads_pages(self):
        p = GoFundMe(use_api=False, sleep=lambda s: None)
        fetch = FakeFetch(fixture('gofundme_ok.html'))
        self.assertIsNone(p.prefetch(['https://gofund.me/abc'], fetch))
        self.assertEqual(p.fetch_figures('https://gofund.me/abc', fetch).raised, Decimal('12345.5'))
        self.assertEqual(fetch.urls, ['https://gofund.me/abc'])

    def test_setting_turns_api_off(self):
        for value, on in (('0', False), ('off', False), ('1', True), ('', True)):
            with self.subTest(value=value), mock.patch.dict('os.environ', {'GOFUNDME_API': value}):
                self.assertEqual(GoFundMe().use_api, on)

    def test_run_prints_the_api_note(self):
        lines = []
        store = FakeStore([row(id=1), row(id=2, url='https://www.gofundme.com/f/other')])
        run(store, [self.provider()], apply=False, fetch=FakeFetch(gfm_batch(gfm_node(), gfm_node())),
            retries=0, out=lines.append, sleep=lambda s: None, jitter=lambda: 0)
        self.assertEqual(lines[1], 'gofundme: API answered 2 of 2 fundraisers, the rest are read from their pages')
        self.assertTrue(lines[2].startswith('1: ok'))


class CheckRulesTest(unittest.TestCase):
    """The original sync.py decision rules, one case each."""

    def setUp(self):
        self.p = pages_only()
        self.ok = fixture('gofundme_ok.html')  # USD 12345.5 raised, goal 50000

    def outcome(self, r, *responses):
        return check(self.p, r, FakeFetch(*responses), retries=0, sleep=lambda s: None)

    def test_ok(self):
        o = self.outcome(row(), self.ok)
        self.assertEqual((o.status, o.failed), ('ok', False))
        self.assertEqual(o.figures.raised, Decimal('12345.5'))

    def test_invalid_url_does_not_fetch(self):
        fetch = FakeFetch()
        o = check(self.p, row(url='https://gofundme.com/'), fetch)
        self.assertEqual(o.status, 'invalid_url')
        self.assertEqual(fetch.urls, [])

    def test_blocked(self):
        for code in (403, 429):
            with self.subTest(code=code), self.assertRaises(Blocked):
                self.outcome(row(), http_error(code))

    def test_not_found_counts_as_failure(self):
        for code in (404, 410):
            o = self.outcome(row(), http_error(code))
            self.assertEqual((o.status, o.failed), ('not_found', True))

    def test_other_http_status(self):
        o = self.outcome(row(), http_error(500))
        self.assertEqual((o.status, o.failed), ('http_500', False))

    def test_parse_error_counts_as_failure(self):
        o = self.outcome(row(), fixture('gofundme_no_next_data.html'))
        self.assertEqual((o.status, o.failed, o.detail), ('parse_error', True, 'no __NEXT_DATA__ block'))

    def test_network_error(self):
        o = self.outcome(row(), OSError('reset'))
        self.assertEqual((o.status, o.failed, o.detail), ('error', False, 'OSError: reset'))

    def test_stated_currency_is_adopted(self):
        # Stored EUR 20000, page USD 50000: take the page's currency, goal and amount,
        # and skip the drop check, which would compare EUR with USD.
        o = self.outcome(row(currency_code='EUR', goal=Decimal('20000'), raised=Decimal('90000')), self.ok)
        self.assertEqual((o.status, o.figures.currency, o.figures.goal), ('ok', 'USD', Decimal('50000')))
        self.assertEqual(self.outcome(row(currency_code='', raised=Decimal('100')), self.ok).status, 'ok')

    @mock.patch.dict('os.environ', {'SYNC_HOLD_CURRENCY': '1'})
    def test_currency_unknown(self):
        self.assertEqual(self.outcome(row(currency_code=''), self.ok).status, 'currency_unknown')
        # No stored currency and nothing raised yet: adopt the platform's.
        self.assertEqual(self.outcome(row(currency_code=None, raised=None), self.ok).status, 'ok')

    @mock.patch.dict('os.environ', {'SYNC_HOLD_CURRENCY': '1'})
    def test_currency_mismatch_held_unless_goal_matches(self):
        self.assertEqual(self.outcome(row(currency_code='EUR', goal=Decimal('1')), self.ok).status, 'currency_mismatch')
        self.assertEqual(self.outcome(row(currency_code='EUR', goal=None), self.ok).status, 'currency_mismatch')
        # Same goal under a different label: the stored label is wrong.
        self.assertEqual(self.outcome(row(currency_code='EUR', goal=Decimal('50000')), self.ok).status, 'ok')

    def test_suspicious_drop(self):
        # 12345.5 is below 80% of 16000 (12800).
        self.assertEqual(self.outcome(row(raised=Decimal('16000')), self.ok).status, 'suspicious_drop')
        # 12345.5 is above 80% of 15000 (12000).
        self.assertEqual(self.outcome(row(raised=Decimal('15000')), self.ok).status, 'ok')

    def test_held_outcomes_keep_what_the_platform_showed(self):
        o = self.outcome(row(raised=Decimal('16000')), self.ok)
        self.assertTrue(o.held)
        self.assertEqual(o.figures.raised, Decimal('12345.5'))
        self.assertFalse(self.outcome(row(), http_error(404)).held)
        self.assertFalse(self.outcome(row(), self.ok).held)

    def test_not_found_threshold(self):
        o = self.outcome(row(), http_error(404))
        self.assertEqual(shown_status(o, 0), ('not_found_pending', 1))
        self.assertEqual(shown_status(o, 1), ('not_found_pending', 2))
        self.assertEqual(shown_status(o, 2), ('not_found', 3))
        err = self.outcome(row(), http_error(502))
        self.assertEqual(shown_status(err, 2), ('http_502', 2))


class DescribeHeldTest(unittest.TestCase):

    def test_drop_shows_both_figures_change_and_progress(self):
        r = row(raised=Decimal('5000'), goal=Decimal('10000'))
        note = describe_held(r, Figures(Decimal('1200'), Decimal('10000'), 'USD', 31))
        self.assertEqual(note, 'stored 5,000 USD, platform shows 1,200 USD (-76%); '
                               '50% -> 12% of the 10,000 goal; 31 donations; not saved')

    def test_new_goal_and_other_currency(self):
        r = row(raised=Decimal('5000'), goal=Decimal('10000'), currency_code='EUR')
        note = describe_held(r, Figures(Decimal('99.5'), Decimal('20000'), 'USD'))
        self.assertEqual(note, 'stored 5,000 EUR, platform shows 99.50 USD; '
                               '50% -> 0% of the 20,000 goal (stored goal 10,000); not saved')

    def test_no_goal(self):
        r = row(raised=Decimal('5000'), goal=None)
        self.assertEqual(describe_held(r, Figures(Decimal('10'), None, 'USD')),
                         'stored 5,000 USD, platform shows 10 USD (-100%); not saved')


class RetryTest(unittest.TestCase):

    def test_retries_transient_errors_only(self):
        p, ok = pages_only(), fixture('gofundme_ok.html')
        sleeps = []
        o = check(p, row(), FakeFetch(OSError('timeout'), ok), retries=1, sleep=sleeps.append)
        self.assertEqual(o.status, 'ok')
        self.assertEqual(sleeps, [5])
        o = check(p, row(), FakeFetch(http_error(503), ok), retries=1, sleep=sleeps.append)
        self.assertEqual(o.status, 'ok')
        # 404 and 429 are answers, never retried.
        fetch = FakeFetch(http_error(404), ok)
        self.assertEqual(check(p, row(), fetch, retries=3, sleep=sleeps.append).status, 'not_found')
        self.assertEqual(len(fetch.urls), 1)


class FakeStore:
    def __init__(self, rows):
        self.rows = rows
        self.written = []

    def select(self, providers, limit):
        rows = [r for r in self.rows if any(p.matches(r.url) or p.url_pattern for p in providers)]
        return rows[:limit] if limit else rows

    def record_ok(self, rid, fig):
        self.written.append((rid, 'ok', fig.raised))

    def record_problem(self, rid, status, failures, fig=None, detail=''):
        self.written.append((rid, status, failures))
        self.details = getattr(self, 'details', {})
        self.details[rid] = (fig, detail)


class RunTest(unittest.TestCase):

    def run_sync(self, store, responses, apply=True, providers=None):
        lines = []
        result = run(store, providers or [pages_only()], apply=apply, fetch=FakeFetch(*responses),
                     retries=0, out=lines.append, sleep=lambda s: None, jitter=lambda: 0)
        return result, lines

    def test_dry_run_writes_nothing(self):
        store = FakeStore([row()])
        result, lines = self.run_sync(store, [fixture('gofundme_ok.html')], apply=False)
        self.assertEqual(store.written, [])
        self.assertEqual(result.changed, 1)
        self.assertEqual(lines[0], 'DRY RUN: 1 campaigns')
        self.assertIn('1: ok, raised 12000 -> 12345.5', lines)

    def test_apply_records_each_outcome(self):
        store = FakeStore([row(id=1), row(id=2, consecutive_failures=2), row(id=3, url='gofundme.com/')])
        self.run_sync(store, [fixture('gofundme_ok.html'), http_error(404)])
        self.assertEqual(store.written, [(1, 'ok', Decimal('12345.5')), (2, 'not_found', 3), (3, 'invalid_url', 0)])

    def test_says_when_sync_is_turned_off(self):
        store = FakeStore([row(id=1, consecutive_failures=1), row(id=2, consecutive_failures=2)])
        _, lines = self.run_sync(store, [http_error(404), http_error(404)], apply=False)
        self.assertEqual(lines[1:3], ['1: not_found', '2: not_found, sync would be turned off'])
        _, lines = self.run_sync(store, [http_error(404), http_error(404)])
        self.assertEqual(lines[2], '2: not_found, sync turned off')

    def test_held_line_says_what_the_platform_showed(self):
        store = FakeStore([row(id=4, raised=Decimal('16000'), goal=Decimal('50000'))])
        result, lines = self.run_sync(store, [fixture('gofundme_ok.html')])
        self.assertEqual(lines[1], '4: suspicious_drop, stored 16,000 USD, platform shows 12,345.50 USD (-23%); '
                                   '32% -> 25% of the 50,000 goal; 321 donations; not saved')
        self.assertEqual(store.written, [(4, 'suspicious_drop', 0)])
        fig, detail = store.details[4]
        self.assertEqual(fig.raised, Decimal('12345.5'))
        self.assertTrue(detail.startswith('stored 16,000 USD, platform shows 12,345.50 USD'))
        self.assertEqual(result.changed, 0)

    def test_block_stops_the_provider(self):
        store = FakeStore([row(id=1), row(id=2), row(id=3)])
        result, lines = self.run_sync(store, [fixture('gofundme_ok.html'), http_error(429)])
        self.assertEqual(result.exit_code, 2)
        self.assertEqual([w[0] for w in store.written], [1])
        self.assertIn('BLOCKED by gofundme (HTTP 429), skipping its remaining campaigns', lines)

    def test_unconfigured_provider_is_skipped(self):
        store = FakeStore([])
        _, lines = self.run_sync(store, [], providers=[pages_only(), JustGiving(app_id='')])
        self.assertIn('justgiving: not configured, skipped', lines)


class RegistryTest(unittest.TestCase):

    def test_default_is_gofundme_only(self):
        self.assertEqual([p.name for p in registry.enabled('gofundme')], ['gofundme'])

    def test_unknown_provider_fails_loudly(self):
        with self.assertRaises(ValueError):
            registry.enabled('gofundme,gofundmee')


class JustGivingTest(unittest.TestCase):

    def test_normalize(self):
        p = JustGiving(app_id='x')
        self.assertEqual(p.normalize('justgiving.com/page/example-page/'), 'https://www.justgiving.com/page/example-page')
        self.assertIsNone(p.normalize('https://www.justgiving.com/'))
        self.assertIsNone(p.normalize('https://evil.com/page/x'))

    def test_parse(self):
        fig = parse_justgiving(fixture('justgiving_ok.json'))
        self.assertEqual(fig, Figures(Decimal('1010.50'), Decimal('2000.00'), 'GBP', None))
        with self.assertRaises(ParseError):
            parse_justgiving('<html>')

    def test_calls_api_with_app_id(self):
        p = JustGiving(app_id='abc')
        fetch = FakeFetch(fixture('justgiving_ok.json'))
        o = check(p, row(url='https://www.justgiving.com/page/example-page', currency_code='GBP',
                         goal=Decimal('2000'), raised=Decimal('900')), fetch)
        self.assertEqual(o.status, 'ok')
        self.assertEqual(fetch.urls, ['https://api.justgiving.com/abc/v1/fundraising/pages/example-page'])

    def test_not_configured(self):
        self.assertFalse(JustGiving(app_id='').configured())



def ggf_page(sidebar: str) -> str:
    return f'<html><body><div>{sidebar}</div></body></html>'


class GoGetFundingTest(unittest.TestCase):

    def test_reads_figures(self):
        fig = parse_gogetfunding(fixture('gogetfunding_ok.html'))
        self.assertEqual(fig, Figures(Decimal('7352.00'), Decimal('50000.00'), 'USD', 277))

    def test_ignores_scripts_and_styles(self):
        # The fixture's <script> and <style> carry decoy figures.
        self.assertEqual(parse_gogetfunding(fixture('gogetfunding_ok.html')).raised, Decimal('7352.00'))

    def test_currencies_and_no_goal(self):
        self.assertEqual(parse_gogetfunding(ggf_page('£120.50 raised of £500 goal')),
                         Figures(Decimal('120.50'), Decimal('500'), 'GBP', None))
        self.assertEqual(parse_gogetfunding(ggf_page('NZ$1,000 raised')),
                         Figures(Decimal('1000'), None, 'NZD', None))
        self.assertIsNone(parse_gogetfunding(ggf_page('$80 raised of $100 goal')).currency)

    def test_donated_so_far_layout(self):
        # Text shapes from live pages (campaigns 986 and 1109), 2026-10-12.
        page = ggf_page('Story Updates 0 Donors 12 Followers 0 Previous Next US$589.00 Donated So Far --> '
                        'Help this ongoing fundraising campaign')
        self.assertEqual(parse_gogetfunding(page), Figures(Decimal('589.00'), None, 'USD', 12))
        closed = ggf_page('Updates 2 Donors 87 Followers 5 Previous Next US$2,906.00 Donated So Far --> '
                          'Raised offline: $505.00 Total: $3,411.00 The campaign owner has stopped the page '
                          'from accepting further donations.')
        self.assertEqual(parse_gogetfunding(closed), Figures(Decimal('3411.00'), None, 'USD', 87))
        with self.assertRaises(ParseError):
            parse_gogetfunding(ggf_page('US$5.00 Donated So Far Raised offline: £1.00 Total: £6.00'))
        # Ringgit (campaign 1196, 2026-10-12).
        myr = ggf_page('Donors0 RM0.00 Donated So Far Raised offline: RM20,000.00 Total: RM20,000.00')
        self.assertEqual(parse_gogetfunding(myr), Figures(Decimal('20000.00'), None, 'MYR', 0))
        # A word ending in "rm" is not a prefix.
        with self.assertRaises(ParseError):
            parse_gogetfunding(ggf_page('farm 500 raised'))

    def test_story_text_alone_is_not_a_figure(self):
        with self.assertRaises(ParseError):
            parse_gogetfunding(ggf_page('The money raised will cover evacuation and travel expenses.'))

    def test_donor_count_from_tab_bar(self):
        page = ggf_page('US$5 raised of $10 goal <a>Updates 3 Donors 1,204 Followers 9</a>')
        self.assertEqual(parse_gogetfunding(page).donations, 1204)

    def test_rejects_bad_pages(self):
        for page in ('<html>Campaign not found</html>',
                     ggf_page('US$5 raised of $10 goal ... US$6 raised of $10 goal'),
                     ggf_page('£5 raised of $10 goal'),
                     ggf_page('¥500 raised')):
            with self.subTest(page=page), self.assertRaises(ParseError):
                parse_gogetfunding(page)

    def test_normalize(self):
        p = GoGetFunding()
        self.assertEqual(p.normalize('www.gogetfunding.com/help-example-rebuild/'),
                         'https://gogetfunding.com/help-example-rebuild/')
        self.assertIsNone(p.normalize('https://gogetfunding.com/'))
        self.assertIsNone(p.normalize('https://gogetfunding.com.evil.org/x'))
        self.assertIsNone(p.normalize('GoGetFunding campaign removed'))

    def test_bare_dollar_keeps_stored_dollar_currency(self):
        p, page = GoGetFunding(), ggf_page('$80 raised of $100 goal')
        r = row(url='https://gogetfunding.com/x/', currency_code='AUD', goal=Decimal('100'), raised=Decimal('70'))
        o = check(p, r, FakeFetch(page))
        self.assertEqual((o.status, o.figures.currency), ('ok', 'AUD'))
        for code in ('GBP', '', None):
            with self.subTest(code=code):
                self.assertEqual(check(p, row(url=r.url, currency_code=code, raised=None), FakeFetch(page)).status,
                                 'currency_unknown')

    def test_registered_but_not_default(self):
        self.assertEqual([p.name for p in registry.enabled('gogetfunding')], ['gogetfunding'])
        self.assertNotIn('gogetfunding', registry.DEFAULT)


def sf_section(text: str) -> str:
    return f'<div data-testid="amount-raised-section"><span>{text}</span> Donors</div>'


class SpotfundTest(unittest.TestCase):

    def test_reads_figures(self):
        # Trimmed from the live page for fundraiser 682, 2026-10-12.
        self.assertEqual(parse_spotfund(fixture('spotfund_ok.html')),
                         Figures(Decimal('31282'), Decimal('65000'), 'USD', 479))

    def test_no_goal_and_no_currency(self):
        self.assertEqual(parse_spotfund(sf_section('$1,845.50 12')), Figures(Decimal('1845.50'), None, None, 12))

    def test_story_currency_not_donation_currency(self):
        page = (sf_section('$5 of $10 goal 1') + '{"goal":10,"is_zero_fee":false,"currency_code":"CAD"}'
                '{"amount":5,"currency_code":"USD"}')
        self.assertEqual(parse_spotfund(page).currency, 'CAD')

    def test_rejects_bad_pages(self):
        for page in ('<html>Story not found</html>',
                     sf_section('$5 of $10 goal 1') + sf_section('$6 of $10 goal 1'),
                     sf_section('Raised so far 1'),
                     sf_section('$5 1') + '"is_zero_fee":true,"currency_code":"USD"'
                     '"is_zero_fee":true,"currency_code":"CAD"'):
            with self.subTest(page=page), self.assertRaises(ParseError):
                parse_spotfund(page)

    def test_normalize(self):
        p = Spotfund()
        self.assertEqual(p.normalize('spot.fund/AbC123'), 'https://spot.fund/AbC123')
        self.assertEqual(p.normalize('https://www.spotfund.com/story/abc?utm_source=copy_link'),
                         'https://www.spotfund.com/story/abc')
        self.assertIsNone(p.normalize('https://www.spotfund.com/'))
        self.assertIsNone(p.normalize('https://spot.fund.evil.org/x'))
        # A page title stored in the url field (fundraiser 1108).
        self.assertIsNone(p.normalize('Help Example Family Rebuild | Spotfund'))

    def test_registered_but_not_default(self):
        self.assertEqual([p.name for p in registry.enabled('spotfund')], ['spotfund'])
        self.assertNotIn('spotfund', registry.DEFAULT)


def pp_page(**fields) -> str:
    data = {'donationCode': 'USD', 'productId': 'ABCD1234EFGH5', 'campaignTargetAmount': '1000.00',
            'campaignTotalAmountRaised': '250.00', 'campaignTotalNumberOfTxn': 7}
    data.update(fields)
    return '<script>' + json.dumps({k: v for k, v in data.items() if v is not None}) + '</script>'


class PayPalTest(unittest.TestCase):

    def test_reads_figures(self):
        self.assertEqual(parse_paypal(fixture('paypal_ok.html'), 'A4PQB5UYBA7T6'),
                         Figures(Decimal('10952.56'), Decimal('50000.00'), 'USD', 203))

    def test_no_goal(self):
        for goal in ('', '0.00', None):
            with self.subTest(goal=goal):
                self.assertIsNone(parse_paypal(pp_page(campaignTargetAmount=goal), 'ABCD1234EFGH5').goal)

    def test_rejects_bad_pages(self):
        for page in ('<html>Things don\'t appear to be working at the moment.</html>',
                     pp_page(campaignTotalAmountRaised=None),
                     pp_page(campaignTotalAmountRaised='lots'),
                     pp_page(productId='OTHER1234567'),
                     pp_page() + pp_page(campaignTotalAmountRaised='300.00'),
                     pp_page(donationCode='usd')):
            with self.subTest(page=page), self.assertRaises(ParseError):
                parse_paypal(page, 'ABCD1234EFGH5')

    def test_normalize(self):
        p = PayPal()
        self.assertEqual(p.normalize('https://www.paypal.com/donate?campaign_id=6VWXM2SYNUE3N&fbclid=PAZ_x-1'),
                         'https://www.paypal.com/donate/?campaign_id=6VWXM2SYNUE3N')
        self.assertEqual(p.normalize('paypal.com/donate/?campaign_id=YK6DYPRRGHY8U'),
                         'https://www.paypal.com/donate/?campaign_id=YK6DYPRRGHY8U')
        for bad in ('https://www.paypal.com/paypalme/Example', 'https://www.paypal.com/donate/?hosted_button_id=QDSBYBVZ5W8SC',
                    'https://www.paypal.com/donate?campaign_id=', 'https://paypal.com.evil.org/donate?campaign_id=ABCD1234EFGH5'):
            with self.subTest(url=bad):
                self.assertIsNone(p.normalize(bad))

    def test_url_pattern_selects_only_fundraisers(self):
        pattern = re.compile(PayPal.url_pattern, re.IGNORECASE)
        self.assertTrue(pattern.search('https://www.paypal.com/donate?campaign_id=8K2YVE7TV84QY'))
        self.assertTrue(pattern.search('https://www.paypal.com/donate/?locale.x=en_US&campaign_id=8K2YVE7TV84QY'))
        self.assertFalse(pattern.search('https://www.paypal.com/paypalme/Example?locale.x=en_US'))
        self.assertFalse(pattern.search('https://www.paypal.com/donate/?hosted_button_id=BYKYW5TVTR89W'))

    def test_check_fetches_normalized_url(self):
        fetch = FakeFetch(pp_page())
        r = row(url='https://www.paypal.com/donate?campaign_id=ABCD1234EFGH5&fbclid=x', goal=Decimal('1000'),
                raised=Decimal('200'))
        o = check(PayPal(), r, fetch)
        self.assertEqual((o.status, o.figures.raised), ('ok', Decimal('250.00')))
        self.assertEqual(fetch.urls, ['https://www.paypal.com/donate/?campaign_id=ABCD1234EFGH5'])

    def test_registered_but_not_default(self):
        self.assertEqual([p.name for p in registry.enabled('paypal')], ['paypal'])
        self.assertNotIn('paypal', registry.DEFAULT)


def chuffed_body(raised='3088065', goal='3500000', currency='USD', goal_currency=None, count=569):
    return json.dumps({'data': {'campaign': {
        'id': '019cb7e9-75aa-7d58-b470-cd928fb63d90', 'slug': '120680-a-mothers-plea', 'title': 'A Mother',
        'status': 'Completed', 'target': {'amount': goal, 'currency': goal_currency or currency},
        'collected': {'amount': raised, 'currency': currency}, 'donations': {'totalCount': count}}}})


class ChuffedTest(unittest.TestCase):

    def test_reads_figures_in_cents(self):
        # Response shape from fundraiser 120680, fetched from the server on 2026-10-15.
        self.assertEqual(parse_chuffed(chuffed_body()), Figures(Decimal('30880.65'), Decimal('35000'), 'USD', 569))

    def test_zero_decimal_currency_and_no_goal(self):
        self.assertEqual(parse_chuffed(chuffed_body(raised='5000', goal='0', currency='JPY')),
                         Figures(Decimal('5000'), None, 'JPY', 569))

    def test_rejects_bad_responses(self):
        for body in ('<html>challenge</html>', '{"errors":[{"message":"Invalid query"}]}',
                     chuffed_body(goal_currency='AUD'), chuffed_body(raised='lots'),
                     chuffed_body(raised='NaN'), '{"data":{"campaign":{"collected":null}}}'):
            with self.subTest(body=body), self.assertRaises(ParseError):
                parse_chuffed(body)

    def test_unknown_campaign_counts_as_not_found(self):
        p = Chuffed(token='t', sleep=lambda s: None)
        o = check(p, row(url='https://chuffed.org/project/1-x'), FakeFetch('{"data":{"campaign":null}}'))
        self.assertEqual((o.status, o.failed), ('not_found', True))

    def test_unknown_campaign_with_error_counts_as_not_found(self):
        # What Chuffed sends for a number it doesn't know (fundraiser 825, 2026-10-15).
        body = '{"errors":[{"message":"Invalid query"}],"data":{"campaign":null}}'
        o = check(Chuffed(token='t', sleep=lambda s: None), row(url='https://chuffed.org/project/156665-x'), FakeFetch(body))
        self.assertEqual((o.status, o.failed), ('not_found', True))

    def batch_body(self, **nodes):
        return json.dumps({'data': {k: (json.loads(v)['data']['campaign'] if v else None) for k, v in nodes.items()}})

    def test_prefetch_asks_for_many_campaigns_in_one_request(self):
        p = Chuffed(token='t', sleep=lambda s: None)
        fetch = FakeFetch(self.batch_body(c0=chuffed_body(), c1=chuffed_body(raised='100', goal='0'), c2=None))
        urls = ['https://chuffed.org/project/120680', 'https://chuffed.org/project/aid-for-yazan',
                'https://chuffed.org/project/120680', 'https://chuffed.org/project/156665']
        p.prefetch(urls, fetch)
        query = json.loads(fetch.data)['query']
        self.assertIn('c0: campaign(id: "120680")', query)
        self.assertIn('c1: campaign(id: "aid-for-yazan")', query)
        self.assertIn('c2: campaign(id: "156665")', query)
        self.assertNotIn('c3', query)
        self.assertEqual(p.fetch_figures(urls[0], fetch).raised, Decimal('30880.65'))
        self.assertEqual(p.fetch_figures(urls[1], fetch), Figures(Decimal('1'), None, 'USD', 569))
        with self.assertRaises(urllib.error.HTTPError) as e:
            p.fetch_figures(urls[3], fetch)
        self.assertEqual(e.exception.code, 404)
        self.assertEqual(len(fetch.urls), 1)

    def test_prefetch_splits_into_batches_and_paces_them(self):
        waits = []
        now = [0.0]
        p = Chuffed(token='t', clock=lambda: now[0], sleep=lambda s: (waits.append(s), now.__setitem__(0, now[0] + s)))
        urls = [f'https://chuffed.org/project/{n}' for n in range(45)]
        fetch = FakeFetch(*[self.batch_body() for _ in range(3)])
        p.prefetch(urls, fetch)
        self.assertEqual(len(fetch.urls), 3)
        self.assertEqual(waits, [MIN_INTERVAL, MIN_INTERVAL])

    def test_rejected_batch_falls_back_to_single_requests(self):
        p = Chuffed(token='t', sleep=lambda s: None)
        fetch = FakeFetch('{"errors":[{"message":"Invalid query"}],"data":{"c0":null,"c1":null}}', chuffed_body())
        p.prefetch(['https://chuffed.org/project/1', 'https://chuffed.org/project/2'], fetch)
        self.assertEqual(p.fetch_figures('https://chuffed.org/project/1', fetch).raised, Decimal('30880.65'))
        self.assertIn('campaign(id: \\"1\\")', fetch.data.decode())

    def test_run_uses_batch_without_runner_waits(self):
        sleeps, lines = [], []
        rows = [row(id=1, url='https://chuffed.org/project/120680-a', goal=Decimal('35000'), raised=Decimal('30000')),
                row(id=2, url='https://chuffed.org/project/aid-for-yazan', goal=Decimal('35000'), raised=Decimal('30000'))]
        fetch = FakeFetch(self.batch_body(c0=chuffed_body(), c1=chuffed_body()))
        result = run(FakeStore(rows), [Chuffed(token='t', sleep=lambda s: None)], apply=False, fetch=fetch,
                     out=lines.append, sleep=sleeps.append, jitter=lambda: 0)
        self.assertEqual(result.counts['ok'], 2)
        self.assertEqual(len(fetch.urls), 1)
        self.assertEqual(sleeps, [])

    def test_run_falls_back_when_batch_fails(self):
        lines = []
        rows = [row(id=1, url='https://chuffed.org/project/120680', goal=Decimal('35000'), raised=Decimal('30000'))]
        fetch = FakeFetch(OSError('timed out'), chuffed_body())
        result = run(FakeStore(rows), [Chuffed(token='t', sleep=lambda s: None)], apply=False, fetch=fetch,
                     retries=0, out=lines.append, sleep=lambda s: None, jitter=lambda: 0)
        self.assertEqual(result.counts['ok'], 1)
        self.assertIn('chuffed: batch fetch failed (OSError: timed out), fetching one at a time', lines)

    def test_posts_query_with_token(self):
        p, fetch = Chuffed(token=' secret ', sleep=lambda s: None), FakeFetch(chuffed_body())
        r = row(url='https://chuffed.org/project/120680-a-mothers-plea?fbclid=x', goal=Decimal('35000'),
                raised=Decimal('30000'))
        o = check(p, r, fetch)
        self.assertEqual((o.status, o.figures.raised), ('ok', Decimal('30880.65')))
        self.assertEqual(fetch.urls, ['https://chuffed.org/api/graphql'])
        self.assertEqual(fetch.headers['x-chuffed-token'], 'secret')
        self.assertIn('campaign(id: \\"120680\\")', fetch.data.decode())

    def test_not_configured_without_token(self):
        o = check(Chuffed(token=''), row(url='https://chuffed.org/project/120680'), FakeFetch())
        self.assertEqual(o.status, 'not_configured')

    def test_keeps_under_ten_requests_a_minute(self):
        now, waits = [100.0], []
        p = Chuffed(token='t', clock=lambda: now[0], sleep=lambda s: (waits.append(s), now.__setitem__(0, now[0] + s)))
        fetch = FakeFetch(chuffed_body(), chuffed_body())
        p.fetch_figures('https://chuffed.org/project/1', fetch)
        now[0] += 2.0   # a retry 2 s later
        p.fetch_figures('https://chuffed.org/project/1', fetch)
        self.assertEqual(waits, [4.5])
        self.assertLess(60 / MIN_INTERVAL, 10)

    def test_normalize(self):
        p = Chuffed(token='t')
        self.assertEqual(p.normalize('https://chuffed.org/project/146063?fbclid=PAZ'), 'https://chuffed.org/project/146063')
        self.assertEqual(p.normalize('www.chuffed.org/project/120680-a-mothers-plea/'), 'https://chuffed.org/project/120680')
        self.assertEqual(p.normalize('https://chuffed.org/project/aid-for-yazan/'), 'https://chuffed.org/project/aid-for-yazan')
        for bad in ('https://chuffed.org/project/', 'https://chuffed.org/', 'https://chuffed.org/project/1/x', 'https://chuffed.org.evil.org/project/1',
                    'Chuffed campaign for Amira'):
            with self.subTest(url=bad):
                self.assertIsNone(p.normalize(bad))

    def test_registered_but_not_default(self):
        self.assertEqual([p.name for p in registry.enabled('chuffed')], ['chuffed'])
        self.assertNotIn('chuffed', registry.DEFAULT)


def steun_page(raised='3.721', goal='30.000', count='135', link_raised=None):
    # Shaped like the text of fundraiser 1830's page (2026-10-15); markup synthetic.
    link_raised = link_raised or raised
    return (f'<div class="progress"><strong>€ {raised}</strong> van € {goal} opgehaald</div>'
            f'<div><strong>12%</strong>van het doel</div><div><strong>{count}</strong>donaties</div>'
            f'<a href="#donations" title="Donaties"><strong>€ {link_raised}</strong> van € {goal} '
            f'<strong>{count}</strong> donaties 12%</a><h5>{count} donaties</h5>'
            f'<a>Bekijk alle {count} donaties</a>')


class SteunactieTest(unittest.TestCase):

    def test_reads_dutch_amounts(self):
        self.assertEqual(parse_steunactie(steun_page()), Figures(Decimal('3721'), Decimal('30000'), 'EUR', 135))
        self.assertEqual(parse_steunactie(steun_page(raised='1.234,56', goal='2.000.000', count='1.204')),
                         Figures(Decimal('1234.56'), Decimal('2000000'), 'EUR', 1204))

    def test_raised_without_goal(self):
        page = '<p><strong>€ 250</strong> opgehaald</p><p>4 donaties</p>'
        self.assertEqual(parse_steunactie(page), Figures(Decimal('250'), None, 'EUR', 4))

    def test_rejects_missing_or_conflicting_amounts(self):
        for page in ('<html>Pagina niet gevonden</html>', steun_page(link_raised='3.900')):
            with self.subTest(page=page[:40]), self.assertRaises(ParseError):
                parse_steunactie(page)

    def test_normalize(self):
        p = Steunactie()
        self.assertEqual(p.normalize('https://steunactie.be/actie/help-my-family-in-gaza-5/-38978?fbclid=x'),
                         'https://steunactie.be/actie/help-my-family-in-gaza-5/-38978')
        self.assertEqual(p.normalize('www.steunactie.be/actie/x/-1'), 'https://steunactie.be/actie/x/-1')
        for bad in ('https://steunactie.be/', 'https://steunactie.be.evil.org/actie/x', 'Steunactie for Linda'):
            with self.subTest(url=bad):
                self.assertIsNone(p.normalize(bad))

    def test_registered_but_not_default(self):
        self.assertEqual([p.name for p in registry.enabled('steunactie')], ['steunactie'])
        self.assertNotIn('steunactie', registry.DEFAULT)


if __name__ == '__main__':
    unittest.main()
