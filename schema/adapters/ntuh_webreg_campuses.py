"""Thin WebReg progress adapters for campuses without a dedicated schedule scraper.

Roster / week schedule for these campuses come from
``scripts/import_ntuh_webreg_catalog.py``. These adapters exist so
``get_live_number`` can call ``fetch_live_progress`` for every WebReg campus.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from .base import AdapterNotImplemented, HospitalAdapter
from .ntuh_progress import HOSPITAL_HOSP_CODES, REG_BASE, fetch_live_progress


class _NtuhWebRegProgressAdapter(HospitalAdapter):
    """Progress-only adapter; roster/schedule live in the catalog import."""

    hospital_id: str = ""
    name_zh: str = ""
    hosp_codes: list[str] = []

    def __init__(self) -> None:
        self.last_fetch_report: dict[str, Any] = {}

    def fetch_roster(self, *, force: bool = False) -> list[dict]:
        raise AdapterNotImplemented(
            self.hospital_id,
            "roster comes from scripts/import_ntuh_webreg_catalog.py",
        )

    def fetch_week_schedule(
        self,
        *,
        week_start: date | None = None,
        weeks: int = 1,
    ) -> list[dict]:
        raise AdapterNotImplemented(
            self.hospital_id,
            "week schedule comes from scripts/import_ntuh_webreg_catalog.py",
        )

    def fetch_live_progress(
        self,
        *,
        breast_only: bool = False,
        fetch_details: bool = True,
        ampm_codes: list[str] | None = None,
        campus_code: str | None = None,
    ) -> list[dict]:
        codes = list(self.hosp_codes or HOSPITAL_HOSP_CODES.get(self.hospital_id, []))
        rows = fetch_live_progress(
            hospital_id=self.hospital_id,
            hosp_codes=codes,
            progress_row_fn=self.progress_row,
            ampm_codes=ampm_codes,
            fetch_details=fetch_details,
            breast_only=breast_only,
            campus_code=campus_code,
        )
        self.last_fetch_report = {
            **getattr(self, "last_fetch_report", {}),
            "progress_total": len(rows),
            "progress_hosp_codes": codes,
            "progress_filter": "breast" if breast_only else "all_depts",
        }
        return rows

    def fetch_registration_links(self) -> list[dict]:
        code = (self.hosp_codes or [""])[0]
        hub = f"{REG_BASE}BranchIndex?vHospCode={code}"
        progress = f"{REG_BASE}ClinicCurrentLightNo?vHospCode={code}"
        return [
            self.registration_link_row(
                kind="hub",
                url=hub,
                label_zh=f"{self.name_zh}網路掛號",
                timetable_how="ntuh_webreg_catalog",
                source_url=hub,
            ),
            self.registration_link_row(
                kind="progress",
                url=progress,
                label_zh="看診進度（全科燈號）",
                timetable_how="ntuh_clinic_current_light_all_depts",
                source_url=progress,
            ),
        ]

    def normalize(self, raw: Any, *, kind: str) -> list[dict]:
        if kind in ("progress", "registration", "roster", "schedule"):
            return []
        raise ValueError(f"unsupported normalize kind={kind!r}")


class NtuhChildrenAdapter(_NtuhWebRegProgressAdapter):
    hospital_id = "ntuh_children"
    name_zh = "臺大兒童醫院"
    hosp_codes = ["CH"]


class NtuhBeihuAdapter(_NtuhWebRegProgressAdapter):
    hospital_id = "ntuh_beihu"
    name_zh = "臺大醫院北護分院"
    hosp_codes = ["T2"]


class NtuhJinshanAdapter(_NtuhWebRegProgressAdapter):
    hospital_id = "ntuh_jinshan"
    name_zh = "臺大醫院金山分院"
    hosp_codes = ["T3"]


class NtuhYunlinAdapter(_NtuhWebRegProgressAdapter):
    hospital_id = "ntuh_yunlin"
    name_zh = "臺大醫院雲林分院"
    hosp_codes = ["Y0"]


__all__ = [
    "NtuhChildrenAdapter",
    "NtuhBeihuAdapter",
    "NtuhJinshanAdapter",
    "NtuhYunlinAdapter",
]
