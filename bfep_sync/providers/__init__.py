"""Provider registry.

To add a platform: write a Provider subclass in this package, add it to
PROVIDERS, give it fixture tests, then list it in SYNC_PROVIDERS to turn it on.
"""
from __future__ import annotations

import os

from .base import Blocked, Fetcher, Figures, NotConfigured, ParseError, Provider
from .gofundme import GoFundMe
from .gogetfunding import GoGetFunding
from .justgiving import JustGiving

PROVIDERS: dict[str, type[Provider]] = {
    'gofundme': GoFundMe,
    'gogetfunding': GoGetFunding,
    'justgiving': JustGiving,
}

#: Providers that run when SYNC_PROVIDERS is not set. GoFundMe only, which is
#: exactly what the original script did.
DEFAULT = 'gofundme'


def enabled(names: str | None = None) -> list[Provider]:
    """Instantiates the providers named in `names` or SYNC_PROVIDERS.

    Unknown names raise ValueError so a typo in config fails loudly.
    """
    raw = names if names is not None else os.environ.get('SYNC_PROVIDERS', DEFAULT)
    wanted = [n.strip().lower() for n in raw.split(',') if n.strip()]
    unknown = [n for n in wanted if n not in PROVIDERS]
    if unknown:
        raise ValueError(f'unknown provider(s): {", ".join(unknown)}; known: {", ".join(PROVIDERS)}')
    return [PROVIDERS[n]() for n in dict.fromkeys(wanted)]


__all__ = ['Blocked', 'Fetcher', 'Figures', 'NotConfigured', 'ParseError', 'Provider',
           'PROVIDERS', 'DEFAULT', 'enabled', 'GoFundMe', 'GoGetFunding', 'JustGiving']
