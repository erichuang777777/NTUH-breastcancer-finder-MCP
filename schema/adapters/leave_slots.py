"""Derive leave notices from cancelled clinic slots (no invention).

Copied from OpenOnco ``cgmh_progress.leave_from_cancelled_slots`` so the NTUH
schedule adapters do not import the CGMH stack.
"""

from __future__ import annotations

from .base import STATUS_CANCELLED, now_taipei


def leave_from_cancelled_slots(
    slots: list[dict],
    *,
    hospital_id: str,
    leave_notice_row_fn,
    scraped_at=None,
) -> list[dict]:
    """Derive LeaveNotice rows from ClinicSlot status=停診 (no invention)."""
    ts = scraped_at or now_taipei()
    out: list[dict] = []
    for s in slots:
        st = s.get("status") or ""
        if not st.startswith(STATUS_CANCELLED) and "停診" not in st:
            continue
        hid = s.get("hospital_id") or hospital_id
        name = s.get("name_zh")
        row = leave_notice_row_fn(
            leave_date=s.get("clinic_date"),
            name_zh=name,
            doctor_id=s.get("doctor_id") or (f"{hid}:{name}" if name else None),
            session=s.get("session"),
            reason_zh="停診",
            source_url=s.get("source_url") or s.get("registration_url"),
            notes="derived_from_clinic_slots",
            scraped_at=ts,
        )
        row["hospital_id"] = hid
        row["notice_id"] = (
            f"{hid}:{row.get('leave_date') or ''}:{row.get('session') or ''}:"
            f"{row.get('doctor_id') or name or 'unknown'}"
        )
        out.append(row)
    return out
