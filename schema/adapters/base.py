"""Hospital adapter ABC + errors for breast-care war-room schema."""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from datetime import date, datetime, timezone, timedelta
from typing import Any

TAIPEI = timezone(timedelta(hours=8))

# Canonical session labels
SESSION_AM = "上午"
SESSION_PM = "下午"
SESSION_EVE = "夜間"
SESSIONS = frozenset({SESSION_AM, SESSION_PM, SESSION_EVE})

STATUS_OPEN = "可掛號"
STATUS_FULL = "額滿"
STATUS_CANCELLED = "停診"
STATUS_UNKNOWN = "未知"


class AdapterError(Exception):
    """Base adapter error."""

    def __init__(self, hospital_id: str, message: str) -> None:
        self.hospital_id = hospital_id
        super().__init__(f"[{hospital_id}] {message}")


class AdapterFetchError(AdapterError):
    """HTTP/TLS/timeout/blocked."""


class AdapterParseError(AdapterError):
    """Fetch succeeded but parse failed."""


class AdapterRateLimited(AdapterError):
    """Throttled; honor retry_after_sec."""

    def __init__(
        self, hospital_id: str, message: str, *, retry_after_sec: float = 60.0
    ) -> None:
        self.retry_after_sec = retry_after_sec
        super().__init__(hospital_id, message)


class AdapterNotImplemented(AdapterError):
    """Stub method not implemented yet."""



def optional_count(value: Any) -> int | None:
    """Non-negative integer if published; None otherwise. Does not invent."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, float):
        if value < 0 or not value.is_integer():
            return None
        return int(value)
    s = str(value).strip().replace(",", "")
    if re.fullmatch(r"\d+", s):
        return int(s)
    return None


def now_taipei() -> datetime:
    return datetime.now(TAIPEI)


def today_taipei() -> date:
    return now_taipei().date()


class HospitalAdapter(ABC):
    """Per-hospital (or system-family) adapter → canonical dicts (SCHEMA.md)."""

    hospital_id: str
    name_zh: str
    min_request_interval_sec: float = 1.0

    @abstractmethod
    def fetch_roster(self, *, force: bool = False) -> list[dict]:
        """→ Doctor rows."""

    @abstractmethod
    def fetch_week_schedule(
        self,
        *,
        week_start: date | None = None,
        weeks: int = 1,
    ) -> list[dict]:
        """→ ClinicSlot rows (dated sessions)."""

    @abstractmethod
    def fetch_live_progress(self) -> list[dict]:
        """→ LiveProgress rows (breast-filtered only)."""

    def fetch_registration_links(self) -> list[dict]:
        """→ RegistrationLink rows. Default: empty."""
        return []

    def fetch_leave_notices(self) -> list[dict]:
        """→ LeaveNotice rows (停診／請假). Default: empty list.

        Prefer public leave boards; may also derive from schedule rows with
        status=停診. Never invent notices.
        """
        return []

    @abstractmethod
    def normalize(self, raw: Any, *, kind: str) -> list[dict]:
        """raw → canonical list; kind in roster|schedule|progress|registration."""

    # --- helpers for subclasses ---

    def _not_impl(self, method: str) -> list[dict]:
        raise AdapterNotImplemented(self.hospital_id, f"{method} not implemented")

    def doctor_row(
        self,
        *,
        name_zh: str,
        specialty_tags: list[str],
        local_doctor_code: str | None = None,
        department_zh: str | None = None,
        is_breast_specialist: bool | None = None,
        source_url: str | None = None,
        source_type: str | None = None,
        registration_url: str | None = None,
        progress_url: str | None = None,
        notes: str | None = None,
        name_en: str | None = None,
    ) -> dict:
        code = local_doctor_code or name_zh
        return {
            "doctor_id": f"{self.hospital_id}:{code}",
            "hospital_id": self.hospital_id,
            "name_zh": name_zh,
            "name_en": name_en,
            "local_doctor_code": local_doctor_code,
            "department_zh": department_zh,
            "specialty_tags": specialty_tags,
            "is_breast_specialist": is_breast_specialist,
            "source_url": source_url,
            "source_type": source_type,
            "registration_url": registration_url,
            "progress_url": progress_url,
            "notes": notes,
        }

    def slot_row(
        self,
        *,
        name_zh: str,
        clinic_date: date | str,
        session: str,
        status: str = STATUS_UNKNOWN,
        specialty_tag: str,
        source_url: str,
        doctor_id: str | None = None,
        local_doctor_code: str | None = None,
        department_zh: str | None = None,
        weekday: str | None = None,
        local_clinic_code: str | None = None,
        registration_url: str | None = None,
        progress_url: str | None = None,
        notes: str | None = None,
        booked_count: int | None = None,
        max_quota: int | None = None,
        remaining_count: int | None = None,
        scraped_at: datetime | None = None,
    ) -> dict:
        if session not in SESSIONS:
            raise ValueError(f"session must be one of {SESSIONS}, got {session!r}")
        d = clinic_date if isinstance(clinic_date, str) else clinic_date.isoformat()
        did = doctor_id or f"{self.hospital_id}:{local_doctor_code or name_zh}"
        ts = scraped_at or now_taipei()
        return {
            "slot_id": f"{self.hospital_id}:{d}:{session}:{did}",
            "hospital_id": self.hospital_id,
            "doctor_id": did,
            "name_zh": name_zh,
            "department_zh": department_zh,
            "clinic_date": d,
            "weekday": weekday,
            "session": session,
            "local_clinic_code": local_clinic_code,
            "status": status,
            "specialty_tag": specialty_tag,
            "registration_url": registration_url,
            "progress_url": progress_url,
            "source_url": source_url,
            "scraped_at": ts.isoformat(),
            "notes": notes,
            "booked_count": optional_count(booked_count),
            "max_quota": optional_count(max_quota),
            "remaining_count": optional_count(remaining_count),
        }

    def progress_row(
        self,
        *,
        clinic_date: date | str,
        source_url: str,
        name_zh: str | None = None,
        doctor_id: str | None = None,
        department_zh: str | None = None,
        session: str | None = None,
        local_clinic_code: str | None = None,
        current_number: int | str | None = None,
        next_number: int | str | None = None,
        max_number: int | str | None = None,
        waiting_count: int | None = None,
        status_text: str | None = None,
        specialty_tag: str | None = None,
        fetched_at: datetime | None = None,
        raw: Any = None,
    ) -> dict:
        d = clinic_date if isinstance(clinic_date, str) else clinic_date.isoformat()
        key = local_clinic_code or doctor_id or name_zh or "unknown"
        ts = fetched_at or now_taipei()
        return {
            "progress_id": f"{self.hospital_id}:{d}:{session or ''}:{key}",
            "hospital_id": self.hospital_id,
            "doctor_id": doctor_id,
            "name_zh": name_zh,
            "department_zh": department_zh,
            "clinic_date": d,
            "session": session,
            "local_clinic_code": local_clinic_code,
            "current_number": current_number,
            "next_number": next_number,
            "max_number": max_number,
            "waiting_count": waiting_count,
            "status_text": status_text,
            "specialty_tag": specialty_tag,
            "source_url": source_url,
            "fetched_at": ts.isoformat(),
            "raw": raw,
        }

    def registration_link_row(
        self,
        *,
        kind: str,
        url: str,
        doctor_id: str | None = None,
        label_zh: str | None = None,
        timetable_how: str | None = None,
        source_url: str | None = None,
        notes: str | None = None,
    ) -> dict:
        key = doctor_id or "hub"
        return {
            "link_id": f"{self.hospital_id}:{key}:{kind}",
            "hospital_id": self.hospital_id,
            "doctor_id": doctor_id,
            "kind": kind,
            "url": url,
            "label_zh": label_zh,
            "timetable_how": timetable_how,
            "valid_from": None,
            "valid_to": None,
            "source_url": source_url,
            "notes": notes,
        }

    def leave_notice_row(
        self,
        *,
        leave_date: date | str | None,
        name_zh: str | None = None,
        doctor_id: str | None = None,
        session: str | None = None,
        reason_zh: str | None = None,
        source_url: str | None = None,
        notes: str | None = None,
        scraped_at: datetime | None = None,
    ) -> dict:
        d = (
            leave_date
            if leave_date is None or isinstance(leave_date, str)
            else leave_date.isoformat()
        )
        key = doctor_id or name_zh or "unknown"
        ts = scraped_at or now_taipei()
        return {
            "notice_id": f"{self.hospital_id}:{d or ''}:{session or ''}:{key}",
            "hospital_id": self.hospital_id,
            "doctor_id": doctor_id,
            "name_zh": name_zh,
            "leave_date": d,
            "session": session,
            "reason_zh": reason_zh,
            "source_url": source_url,
            "scraped_at": ts.isoformat(),
            "notes": notes,
        }
