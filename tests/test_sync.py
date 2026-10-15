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
from bfep_sync.providers.chuffed import Chuffed, parse_chuffed  # noqa: E402
from bfep_sync.providers.gofundme import GoFundMe, parse_gofundme  # noqa: E402
from bfep_sync.providers.gogetfunding import GoGetFunding, parse_gogetfunding  # noqa: E402
from bfep_sync.providers.justgiving import JustGiving, parse_justgiving  # noqa: E402
from bfep_sync.providers.paypal import PayPal, parse_paypal  # noqa: E402
from bfep_sync.providers.spotfund import Spotfund, parse_spotfund  # noqa: E402
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

    def __call__(self, url, headers=None, data=None):
        self.urls.append(url)
        self.headers, self.data = headers, data
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
        self.assertLess(60 / p.delay, 10)

    def test_normalize(self):
        p = Chuffed(token='t')
        self.assertEqual(p.normalize('https://chuffed.org/project/146063?fbclid=PAZ'), 'https://chuffed.org/project/146063')
        self.assertEqual(p.normalize('www.chuffed.org/project/120680-a-mothers-plea/'), 'https://chuffed.org/project/120680')
        for bad in ('https://chuffed.org/project/aid-for-yazan', 'https://chuffed.org/', 'https://chuffed.org.evil.org/project/1',
                    'Chuffed campaign for Amira'):
            with self.subTest(url=bad):
                self.assertIsNone(p.normalize(bad))

    def test_registered_but_not_default(self):
        self.assertEqual([p.name for p in registry.enabled('chuffed')], ['chuffed'])
        self.assertNotIn('chuffed', registry.DEFAULT)


if __name__ == '__main__':
    unittest.main()
