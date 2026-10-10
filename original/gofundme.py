"""GoFundMe page parser: pure functions, no network or database access."""
import json
import re
from dataclasses import dataclass
from decimal import Decimal

NEXT_DATA = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)


class ParseError(Exception):
    pass


@dataclass(frozen=True)
class Figures:
    raised: Decimal
    goal: Decimal | None
    currency: str
    donations: int | None


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

    if raised < 0 or (goal is not None and goal < 0) or not re.fullmatch(r'[A-Z]{3}', currency):
        raise ParseError('implausible values')

    donations = f.get('donationCount')
    return Figures(raised, goal, currency, donations if isinstance(donations, int) else None)
