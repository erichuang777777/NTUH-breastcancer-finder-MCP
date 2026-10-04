"""NTUH-family schedule adapters only.

Public id of this repo is the MCP server ``ntuh-breastcancer-finder``.
Schedule adapters still emit whatever their source publishes. The OpenOnco
sources copied here are breast-clinic scrapers for T0/C0/T4/T7; they are not
a full-hospital roster. Search tools do not filter by breast specialty.
"""

from .base import (
    AdapterError,
    AdapterFetchError,
    AdapterNotImplemented,
    AdapterParseError,
    AdapterRateLimited,
    HospitalAdapter,
)
from .ntuh import NtuhAdapter
from .ntuh_cancer import NtuhCancerAdapter
from .ntuh_hsinchu import NtuhHsinchuAdapter

ADAPTERS: dict[str, type[HospitalAdapter]] = {
    "ntuh": NtuhAdapter,
    "ntuh_cancer": NtuhCancerAdapter,
    "ntuh_hsinchu": NtuhHsinchuAdapter,
}

__all__ = [
    "ADAPTERS",
    "AdapterError",
    "AdapterFetchError",
    "AdapterNotImplemented",
    "AdapterParseError",
    "AdapterRateLimited",
    "HospitalAdapter",
    "NtuhAdapter",
    "NtuhCancerAdapter",
    "NtuhHsinchuAdapter",
]
