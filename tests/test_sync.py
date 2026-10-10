"""Tests for the sync service. Standard library only; no network, no database.

    python -m unittest discover -s tests -v
"""
from __future__ import annotations

import io
import pathlib
import sys
import unittest
import urllib.error
from decimal import Decimal

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'original'))

from bfep_sync import providers as registry  # noqa: E402
from bfep_sync.providers import Blocked, Figures, ParseError  # noqa: E402
from bfep_sync.providers.gofundme import GoFundMe, parse_gofundme  # noqa: E402
from bfep_sync.providers.justgiving import JustGiving, parse_justgiving  # noqa: E402
from bfep_sync.runner import Row, check, run, shown_status  # noqa: E402

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

    def __call__(self, url, headers=None):
        self.urls.append(url)
        item = self.responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


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


class CheckRulesTest(unittest.TestCase):
    """The original sync.py decision rules, one case each."""

    def setUp(self):
        self.p = GoFundMe()
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
        self.assertEqual((o.status, o.failed), ('parse_error', True))

    def test_network_error(self):
        o = self.outcome(row(), OSError('reset'))
        self.assertEqual((o.status, o.failed), ('error', False))

    def test_currency_unknown(self):
        self.assertEqual(self.outcome(row(currency_code=''), self.ok).status, 'currency_unknown')
        # No stored currency and nothing raised yet: adopt the platform's.
        self.assertEqual(self.outcome(row(currency_code=None, raised=None), self.ok).status, 'ok')

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

    def test_not_found_threshold(self):
        o = self.outcome(row(), http_error(404))
        self.assertEqual(shown_status(o, 0), ('not_found_pending', 1))
        self.assertEqual(shown_status(o, 1), ('not_found_pending', 2))
        self.assertEqual(shown_status(o, 2), ('not_found', 3))
        err = self.outcome(row(), http_error(502))
        self.assertEqual(shown_status(err, 2), ('http_502', 2))


class RetryTest(unittest.TestCase):

    def test_retries_transient_errors_only(self):
        p, ok = GoFundMe(), fixture('gofundme_ok.html')
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

    def record_problem(self, rid, status, failures):
        self.written.append((rid, status, failures))


class RunTest(unittest.TestCase):

    def run_sync(self, store, responses, apply=True, providers=None):
        lines = []
        result = run(store, providers or [GoFundMe()], apply=apply, fetch=FakeFetch(*responses),
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

    def test_block_stops_the_provider(self):
        store = FakeStore([row(id=1), row(id=2), row(id=3)])
        result, lines = self.run_sync(store, [fixture('gofundme_ok.html'), http_error(429)])
        self.assertEqual(result.exit_code, 2)
        self.assertEqual([w[0] for w in store.written], [1])
        self.assertIn('BLOCKED by gofundme (HTTP 429), skipping its remaining campaigns', lines)

    def test_unconfigured_provider_is_skipped(self):
        store = FakeStore([])
        _, lines = self.run_sync(store, [], providers=[GoFundMe(), JustGiving(app_id='')])
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


if __name__ == '__main__':
    unittest.main()
