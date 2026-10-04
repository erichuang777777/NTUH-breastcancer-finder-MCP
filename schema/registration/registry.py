"""hospital_id → NTUH RegistrationAdapter only."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mcp_server.ntuh_scope import (
    CANONICAL_IDS,
    canonical_hospital_id,
    is_ntuh_hospital,
)

if TYPE_CHECKING:
    from .base import RegistrationAdapter

NTUH_FAMILY = frozenset(CANONICAL_IDS)


def get_registration_adapter(hospital_id: str, **kwargs) -> "RegistrationAdapter":
    """Return the NTUH WebReg adapter. Legacy ids are mapped to canonical ones."""
    hid = (hospital_id or "").strip()
    if not is_ntuh_hospital(hid):
        raise KeyError(
            f"no RegistrationAdapter for hospital_id={hid!r}; "
            f"this server is NTUH-only ({sorted(NTUH_FAMILY)})"
        )
    canon = canonical_hospital_id(hid)
    from .ntuh import NtuhRegistrationAdapter

    return NtuhRegistrationAdapter(hospital_id=canon, **kwargs)


def supports_registration(hospital_id: str) -> bool:
    return is_ntuh_hospital(hospital_id)


__all__ = [
    "NTUH_FAMILY",
    "get_registration_adapter",
    "supports_registration",
]
