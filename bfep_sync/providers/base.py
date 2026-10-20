"""Provider interface shared by every fundraising platform.

A provider knows three things about one platform: which URLs belong to it,
how to turn a stored URL into a fetchable canonical URL, and how to read the
current figures from that platform. Everything else (safeguards, database
writes, pacing, logging) lives in the runner so every provider gets the same
protections.
"""
from __future__ import annotations

import abc
import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Callable

# fetch(url, headers=None, data=None) -> response body as text; with data
# (bytes) it POSTs. Raises
# urllib.error.HTTPError on HTTP errors and any other exception on network
# failure. Injected so tests never touch the network.
Fetcher = Callable[..., str]


class ParseError(Exception):
    """The page or API response did not contain usable figures."""


class Blocked(Exception):
    """The platform refused us (HTTP 403/429). Stop calling it this run."""


class TotalsHidden(Exception):
    """The platform answered but hides this fundraiser's figures (its
    organiser chose to), so there is nothing to compare or save."""


class NotConfigured(Exception):
    """The provider needs settings (for example an API key) that are missing."""


@dataclass(frozen=True)
class Figures:
    raised: Decimal
    goal: Decimal | None
    currency: str | None   # None: the page showed only a bare "$" (see runner.check())
    donations: int | None = None


def validate_figures(raised: Decimal, goal: Decimal | None, currency: str | None) -> None:
    """Rejects values no real fundraiser would report."""
    bad_code = currency is not None and not re.fullmatch(r'[A-Z]{3}', currency)
    if raised < 0 or (goal is not None and goal < 0) or bad_code:
        raise ParseError('implausible values')


class Provider(abc.ABC):
    #: Short machine name, used in config, logs and the --provider option.
    name: str = ''
    #: PostgreSQL regular expression (case-insensitive) selecting this
    #: provider's rows in campaign_fundraisers.url.
    url_pattern: str = ''
    #: Seconds between requests to this platform, plus 0-2 s of jitter.
    delay: float = 6.0
    #: Set by the runner to its output, for progress notes during prefetch().
    log: Callable[[str], None] | None = None

    def configured(self) -> bool:
        """False when required settings are missing; the runner skips it."""
        return True

    @abc.abstractmethod
    def normalize(self, raw: str | None) -> str | None:
        """Returns a canonical https URL for this platform, or None if invalid."""

    @abc.abstractmethod
    def fetch_figures(self, url: str, fetch: Fetcher) -> Figures:
        """Reads the current figures for an already-normalised URL.

        Raises ParseError when the response has no usable figures and lets
        urllib.error.HTTPError propagate so the runner can classify it.
        """

    def prefetch(self, urls: list[str], fetch: Fetcher) -> str | None:
        """Optionally fetches many already-normalised URLs before the run.

        A provider whose platform can answer for many campaigns in one
        request keeps the answers and serves fetch_figures from them. The
        default does nothing. A returned string is printed as a note in the
        run's output. Errors propagate; the runner reports them and
        fetch_figures then fetches one at a time.
        """

    def matches(self, raw: str | None) -> bool:
        return self.normalize(raw) is not None
