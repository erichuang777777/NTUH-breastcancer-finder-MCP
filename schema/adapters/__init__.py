"""NTUH-family schedule / progress adapters.

Public id of this repo is the MCP server ``ntuh-breastcancer-finder``.
OpenOnco-derived scrapers for ntuh / ntuh_cancer / ntuh_hsinchu still carry
breast-clinic week schedules; live progress for every WebReg campus is
all-department by default (optional breast filter).
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
from .ntuh_webreg_campuses import (
    NtuhBeihuAdapter,
    NtuhChildrenAdapter,
    NtuhJinshanAdapter,
    NtuhYunlinAdapter,
)

ADAPTERS: dict[str, type[HospitalAdapter]] = {
    "ntuh": NtuhAdapter,
    "ntuh_cancer": NtuhCancerAdapter,
    "ntuh_hsinchu": NtuhHsinchuAdapter,
    "ntuh_children": NtuhChildrenAdapter,
    "ntuh_beihu": NtuhBeihuAdapter,
    "ntuh_jinshan": NtuhJinshanAdapter,
    "ntuh_yunlin": NtuhYunlinAdapter,
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
    "NtuhChildrenAdapter",
    "NtuhBeihuAdapter",
    "NtuhJinshanAdapter",
    "NtuhYunlinAdapter",
]
