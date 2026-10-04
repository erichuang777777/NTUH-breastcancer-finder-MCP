"""臺大癌醫中心分院 (NTUH Cancer Center) adapter.

Public sources:

  Hub:     https://www.ntucc.gov.tw/
  Reg C0:  https://reg.ntuh.gov.tw/WebReg/WebReg/BranchIndex?vHospCode=C0
  Schedule: RegDeptSchedule?vhospCode=C0&showBlock=H&vDeptCode=KBRC
            （婦女門診 → 乳房醫學中心；診別為腫瘤外科／腫瘤內科／放射腫瘤等）
  Progress: ClinicCurrentLightNo?vHospCode=C0（全院；stub）
  NHIA:    0401020013；alias hospital_id nhia_0401020013 → ntuh_cancer

網掛開放約兩週內診次；不發明格子。Polite ≥1s/host。
Named breast-team examples on public KBRC board (verbatim orthography):
  林季宏、陳怡君、王明暘、蔡立威、黃祥瑋、黃俊升 …
"""

from __future__ import annotations

import logging
import re
import time
from datetime import date, datetime, timedelta
from typing import Any

import requests
import urllib3
from bs4 import BeautifulSoup

from .base import (
    SESSION_AM,
    SESSION_EVE,
    SESSION_PM,
    STATUS_CANCELLED,
    STATUS_FULL,
    STATUS_OPEN,
    STATUS_UNKNOWN,
    AdapterFetchError,
    AdapterRateLimited,
    HospitalAdapter,
    now_taipei,
    today_taipei,
)

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

log = logging.getLogger(__name__)

USER_AGENT = "BreastCareResearchBot/0.1 (+public schedule research; polite 1req/s)"

HOSPITAL_ID = "ntuh_cancer"
NAME_ZH = "國立臺灣大學醫學院附設醫院癌醫中心分院"
NAME_EN = "National Taiwan University Cancer Center"
NHIA_CODE = "0401020013"
ALIAS_HOSPITAL_IDS = ("nhia_0401020013",)  # legacy society slug

HUB_URL = "https://www.ntucc.gov.tw/"
REG_BASE = "https://reg.ntuh.gov.tw/WebReg/WebReg/"
REG_C0_HUB = f"{REG_BASE}BranchIndex?vHospCode=C0"
PROGRESS_C0 = f"{REG_BASE}ClinicCurrentLightNo?vHospCode=C0"
ROSTER_URL = (
    f"{REG_BASE}RegDeptInfo?vHospCode=C0&showBlock=H"
)  # 婦女門診（乳房醫學中心／婦科）

SCHEDULE_SOURCES: list[dict[str, str]] = [
    {
        "campus_code": "C0",
        "campus_zh": "癌醫中心分院",
        "dept_code": "KBRC",
        "dept_label": "乳房醫學中心",
        "block": "H",
        "url": (
            f"{REG_BASE}RegDeptSchedule?vhospCode=C0"
            f"&showBlock=H&vDeptCode=KBRC&realSubDeptCode="
        ),
        "reg_hub": REG_C0_HUB,
        "progress": PROGRESS_C0,
    },
]

# Public KBRC board names (as published). Used to order roster / notes.
KNOWN_BREAST_TEAM: frozenset[str] = frozenset(
    {
        "林季宏",
        "陳怡君",
        "王明暘",
        "蔡立威",
        "黃祥瑋",
        "黃俊升",
        "張端瑩",
        "李佳真",
        "楊明翰",
        "盧彥伸",
        "謝正彥",
        "黃柏翔",
        "賴詩璠",
        "曾家琳",
        "洪益欣",
    }
)

_WEEKDAY_ZH = ["一", "二", "三", "四", "五", "六", "日"]
_SESSION_MAP = {
    "上午": SESSION_AM,
    "早上": SESSION_AM,
    "早診": SESSION_AM,
    "下午": SESSION_PM,
    "午診": SESSION_PM,
    "夜間": SESSION_EVE,
    "夜診": SESSION_EVE,
    "黃昏": SESSION_EVE,
    "晚上": SESSION_EVE,
}
_ROC_DATE_RE = re.compile(r"(1[0-2]\d)\.(\d{1,2})\.(\d{1,2})")
_NAME_RE = re.compile(r"^([\u4e00-\u9fff·‧]{2,4})")
_BOOKING_RE = re.compile(r"預約碼[：:]\s*(\d+)")
_ROOM_RE = re.compile(r"第\s*(\d+)\s*診")
_CLINIC_RE = re.compile(r"診別\s*([^\n第]+)")
_DEPT_RE = re.compile(r"科別\s*([^\n]+)")
_SESSION_RE = re.compile(r"(上午|下午|夜間|黃昏|早上|早診|午診|夜診|晚上)門診")
_SPEC_RE = re.compile(r"專科:\s*([^\n]+)")


def weekday_zh(d: date) -> str:
    return _WEEKDAY_ZH[d.weekday()]


def roc_to_gregorian(y: int, m: int, d: int) -> date:
    return date(y + 1911, m, d)


def is_breast_center_slot(*, dept_line: str | None, clinic: str | None) -> bool:
    """KBRC page: accept rows under 乳房醫學中心 (clinic names are 腫瘤外科 etc.)."""
    d = (dept_line or "").strip()
    c = (clinic or "").strip()
    if "乳房醫學中心" in d or "乳醫" in d:
        return True
    # Fallback if dept line missing but clinic explicitly breast
    if "乳房" in c or "乳醫" in c:
        return "泌乳" not in c
    return False


def specialty_for_clinic(clinic: str | None, *, spec_note: str | None = None) -> str:
    c = clinic or ""
    note = spec_note or ""
    blob = c + note
    if "放射" in c or "放射腫瘤" in blob:
        return "radiation_oncology"
    if "腫瘤內" in c or ("腫瘤內" in note and "外科" not in c):
        return "medical_oncology"
    if "重建" in blob or "整形" in blob:
        return "plastic_reconstruction"
    if "腫瘤外" in c or "乳房外" in blob or "外科" in c:
        return "breast_surgery"
    if "心臟" in c:
        return "other"
    if "普通" in c or "家醫" in note:
        return "other"
    return "breast_surgery"


def map_status(tag_text: str) -> str:
    t = tag_text or ""
    if "停診" in t or "休診" in t:
        return STATUS_CANCELLED
    if "額滿" in t or "已滿" in t:
        return STATUS_FULL
    if "前往掛號" in t or "可掛" in t:
        return STATUS_OPEN
    if t.strip():
        return STATUS_OPEN
    return STATUS_UNKNOWN


def parse_reg_dept_schedule_html(
    html: str,
    *,
    adapter: "NtuhCancerAdapter",
    source_meta: dict[str, str],
    date_from: date,
    date_to: date,
    scraped_at: datetime,
) -> list[dict]:
    """Parse RegDeptSchedule doctor-tag modals → breast-center ClinicSlot rows."""
    soup = BeautifulSoup(html or "", "html.parser")
    source_url = source_meta["url"]
    campus_code = source_meta["campus_code"]
    campus_zh = source_meta["campus_zh"]
    reg_hub = source_meta["reg_hub"]
    progress = source_meta["progress"]
    out: list[dict] = []
    seen: set[tuple] = set()

    for modal in soup.select("div.modal[id^=largeInfo_]"):
        mid = modal.get("id") or ""
        title_el = modal.select_one(".modal-title")
        title_t = title_el.get_text(" ", strip=True) if title_el else ""
        nm = _NAME_RE.match(title_t.replace(" ", ""))
        if not nm:
            continue
        name = nm.group(1)

        body = modal.select_one(".modal-body") or modal
        text = body.get_text("\n", strip=True)

        cm = _CLINIC_RE.search(text)
        clinic = (cm.group(1).strip() if cm else "") or ""
        dm_line = _DEPT_RE.search(text)
        dept_line = dm_line.group(1).strip() if dm_line else ""
        if not is_breast_center_slot(dept_line=dept_line, clinic=clinic):
            continue

        dm = _ROC_DATE_RE.search(text)
        sm = _SESSION_RE.search(text)
        if not dm or not sm:
            continue
        try:
            clinic_date = roc_to_gregorian(
                int(dm.group(1)), int(dm.group(2)), int(dm.group(3))
            )
        except ValueError:
            continue
        if clinic_date < date_from or clinic_date >= date_to:
            continue

        sess_raw = sm.group(1)
        session = _SESSION_MAP.get(sess_raw)
        if not session:
            continue

        bm = _BOOKING_RE.search(text)
        booking = bm.group(1) if bm else None
        rm = _ROOM_RE.search(text)
        room = rm.group(1) if rm else None
        spec_m = _SPEC_RE.search(text)
        spec_note = spec_m.group(1).strip() if spec_m else None

        btn = soup.select_one(f'button.doctor-tag[data-bs-target="#{mid}"]')
        btn_t = btn.get_text(" ", strip=True) if btn else title_t
        status = map_status(btn_t)

        notes_parts = [campus_zh, "乳房醫學中心", clinic]
        if spec_note:
            notes_parts.append(f"專科:{spec_note}")
        if "代診" in btn_t:
            notes_parts.append("代診不續掛")
        if "到診不續掛" in btn_t:
            notes_parts.append("到診不續掛")
        if "僅存本院初診" in btn_t or "僅存本院初診" in text:
            notes_parts.append("僅存本院初診")
        if booking:
            notes_parts.append(f"預約碼{booking}")

        local_clinic = booking or (f"R{room}" if room else None)

        doctor_id = f"{adapter.hospital_id}:{name}"
        key = (clinic_date, session, name, campus_code, local_clinic or room or "")
        if key in seen:
            continue
        seen.add(key)

        dept_zh = clinic or source_meta.get("dept_label") or "乳房醫學中心"
        # Prefer center label + clinic for UI clarity
        if clinic and "乳房" not in clinic:
            dept_zh = f"乳房醫學中心/{clinic}"

        out.append(
            adapter.slot_row(
                name_zh=name,
                clinic_date=clinic_date,
                session=session,
                status=status,
                specialty_tag=specialty_for_clinic(clinic, spec_note=spec_note),
                source_url=source_url,
                doctor_id=doctor_id,
                local_doctor_code=name,
                department_zh=dept_zh,
                weekday=weekday_zh(clinic_date),
                local_clinic_code=local_clinic,
                registration_url=source_url,  # dept deep link
                progress_url=progress,
                notes="; ".join(notes_parts),
                scraped_at=scraped_at,
            )
        )
    return out


def parse_roster_from_schedule_html(
    html: str, *, adapter: "NtuhCancerAdapter"
) -> list[dict]:
    """Build Doctor rows from unique names on the KBRC schedule board."""
    soup = BeautifulSoup(html or "", "html.parser")
    by_name: dict[str, dict[str, str]] = {}
    for modal in soup.select("div.modal[id^=largeInfo_]"):
        title_el = modal.select_one(".modal-title")
        title_t = title_el.get_text(" ", strip=True) if title_el else ""
        nm = _NAME_RE.match(title_t.replace(" ", ""))
        if not nm:
            continue
        name = nm.group(1)
        body = modal.select_one(".modal-body") or modal
        text = body.get_text("\n", strip=True)
        cm = _CLINIC_RE.search(text)
        clinic = (cm.group(1).strip() if cm else "") or ""
        dm_line = _DEPT_RE.search(text)
        dept_line = dm_line.group(1).strip() if dm_line else ""
        if not is_breast_center_slot(dept_line=dept_line, clinic=clinic):
            continue
        spec_m = _SPEC_RE.search(text)
        spec_note = spec_m.group(1).strip() if spec_m else None
        tag = specialty_for_clinic(clinic, spec_note=spec_note)
        prev = by_name.get(name)
        if prev is None or (
            prev["specialty_tag"] == "other" and tag != "other"
        ):
            by_name[name] = {
                "clinic": clinic,
                "specialty_tag": tag,
                "spec_note": spec_note or "",
            }

    ordered = [n for n in KNOWN_BREAST_TEAM if n in by_name] + [
        n for n in sorted(by_name) if n not in KNOWN_BREAST_TEAM
    ]
    out: list[dict] = []
    for name in ordered:
        meta = by_name[name]
        clinic = meta["clinic"]
        dept_zh = (
            f"乳房醫學中心/{clinic}" if clinic else "乳房醫學中心"
        )
        out.append(
            adapter.doctor_row(
                name_zh=name,
                specialty_tags=[meta["specialty_tag"]],
                department_zh=dept_zh,
                is_breast_specialist=True
                if meta["specialty_tag"]
                in ("breast_surgery", "medical_oncology", "radiation_oncology")
                else None,
                source_url=SCHEDULE_SOURCES[0]["url"],
                source_type="schedule",
                registration_url=SCHEDULE_SOURCES[0]["url"],
                progress_url=PROGRESS_C0,
                notes=(
                    f"癌醫 C0 KBRC 公開診次名冊"
                    + (f"；專科:{meta['spec_note']}" if meta["spec_note"] else "")
                ),
            )
        )
    return out


class NtuhCancerAdapter(HospitalAdapter):
    """NTUH Cancer Center (C0) — RegDeptSchedule KBRC breast center clinics."""

    hospital_id = HOSPITAL_ID
    name_zh = NAME_ZH
    min_request_interval_sec = 1.2

    def __init__(self, *, session: requests.Session | None = None) -> None:
        self._session = session or requests.Session()
        self._session.headers.setdefault("User-Agent", USER_AGENT)
        self._session.headers.setdefault(
            "Accept-Language", "zh-TW,zh;q=0.9,en;q=0.5"
        )
        self._last_request_at = 0.0
        self._tls_insecure = False
        self.last_fetch_report: dict[str, Any] = {}
        self._last_schedule_html: dict[str, str] = {}

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_request_at
        wait = self.min_request_interval_sec - elapsed
        if wait > 0:
            time.sleep(wait)

    def _get(self, url: str, *, timeout: float = 60.0) -> requests.Response:
        self._throttle()
        try:
            try:
                resp = self._session.get(url, timeout=timeout, verify=True)
            except requests.exceptions.SSLError as ssl_err:
                log.warning(
                    "ntuh_cancer TLS verify failed (%s); retry verify=False",
                    ssl_err,
                )
                self._tls_insecure = True
                self._throttle()
                resp = self._session.get(url, timeout=timeout, verify=False)
        except requests.RequestException as e:
            raise AdapterFetchError(self.hospital_id, f"GET {url}: {e}") from e
        finally:
            self._last_request_at = time.monotonic()
        if resp.status_code == 429:
            raise AdapterRateLimited(
                self.hospital_id, f"429 on {url}", retry_after_sec=60.0
            )
        if resp.status_code >= 400:
            raise AdapterFetchError(
                self.hospital_id, f"HTTP {resp.status_code} for {url}"
            )
        return resp

    def _get_text(self, url: str) -> str:
        resp = self._get(url)
        resp.encoding = resp.apparent_encoding or "utf-8"
        return resp.text

    def fetch_roster(self, *, force: bool = False) -> list[dict]:
        _ = force
        src = SCHEDULE_SOURCES[0]
        url = src["url"]
        try:
            html = self._last_schedule_html.get(src["dept_code"]) or self._get_text(
                url
            )
            self._last_schedule_html[src["dept_code"]] = html
        except (AdapterFetchError, AdapterRateLimited) as e:
            self.last_fetch_report = {
                "failures": [{"url": url, "error": str(e)}],
                "total": 0,
            }
            raise
        doctors = parse_roster_from_schedule_html(html, adapter=self)
        self.last_fetch_report = {
            "source": url,
            "total": len(doctors),
            "failures": [],
            "nhia_code": NHIA_CODE,
            "alias_hospital_ids": list(ALIAS_HOSPITAL_IDS),
        }
        return doctors

    def fetch_week_schedule(
        self,
        *,
        week_start: date | None = None,
        weeks: int = 1,
    ) -> list[dict]:
        scraped_at = now_taipei()
        week_start = week_start or today_taipei()
        weeks = max(1, min(int(weeks), 8))
        date_from = week_start
        date_to = week_start + timedelta(weeks=weeks)
        failures: list[dict[str, str]] = []
        per_campus: dict[str, int] = {}
        raw: list[dict] = []

        for src in SCHEDULE_SOURCES:
            url = src["url"]
            try:
                html = self._get_text(url)
                self._last_schedule_html[src["dept_code"]] = html
                batch = parse_reg_dept_schedule_html(
                    html,
                    adapter=self,
                    source_meta=src,
                    date_from=date_from,
                    date_to=date_to,
                    scraped_at=scraped_at,
                )
                per_campus[src["campus_code"]] = len(batch)
                raw.extend(batch)
                if not batch:
                    failures.append(
                        {
                            "url": url,
                            "error": (
                                f"0 breast-center slots after filter "
                                f"({src['campus_zh']} {src['dept_code']})"
                            ),
                            "phase": "schedule",
                        }
                    )
            except (AdapterFetchError, AdapterRateLimited) as e:
                log.warning("ntuh_cancer schedule fetch failed: %s", e)
                failures.append(
                    {"url": url, "error": str(e), "phase": "schedule"}
                )
                per_campus[src["campus_code"]] = 0

        seen: set[tuple] = set()
        slots: list[dict] = []
        for s in raw:
            key = (
                s["clinic_date"],
                s["session"],
                s["name_zh"],
                s.get("doctor_id") or "",
                s.get("local_clinic_code") or "",
            )
            if key in seen:
                continue
            seen.add(key)
            slots.append(s)

        if slots:
            failures = [
                f
                for f in failures
                if "0 breast-center slots after filter"
                not in str(f.get("error") or "")
            ]

        self.last_fetch_report = {
            "failures": failures,
            "weeks": weeks,
            "total": len(slots),
            "raw_before_dedup": len(raw),
            "scraped_at": scraped_at.isoformat(timespec="seconds"),
            "week_start": week_start.isoformat(),
            "date_from": date_from.isoformat(),
            "date_to": date_to.isoformat(),
            "per_campus": per_campus,
            "sources": [s["url"] for s in SCHEDULE_SOURCES],
            "tls_insecure_fallback": bool(self._tls_insecure),
            "nhia_code": NHIA_CODE,
            "alias_hospital_ids": list(ALIAS_HOSPITAL_IDS),
            "city": "臺北市",
            "address": "臺北市大安區基隆路三段155巷57號",
        }
        return slots

    def fetch_live_progress(self) -> list[dict]:
        """ClinicCurrentLightNo C0 → filter DeptCode=KBRC."""
        from .ntuh_progress import fetch_breast_live_progress

        rows = fetch_breast_live_progress(
            hospital_id=self.hospital_id,
            hosp_codes=["C0"],
            progress_row_fn=self.progress_row,
        )
        self.last_fetch_report = {
            **getattr(self, "last_fetch_report", {}),
            "progress_total": len(rows),
            "progress_hosp_codes": ["C0"],
            "progress_filter": "KBRC",
        }
        return rows

    def fetch_leave_notices(self) -> list[dict]:
        """Derive 停診 from week schedule (NTUH has no separate leave board here)."""
        from .leave_slots import leave_from_cancelled_slots

        slots = self.fetch_week_schedule(weeks=2)
        return leave_from_cancelled_slots(
            slots,
            hospital_id=self.hospital_id,
            leave_notice_row_fn=self.leave_notice_row,
        )

    def fetch_registration_links(self) -> list[dict]:
        return [
            self.registration_link_row(
                kind="hub",
                url=REG_C0_HUB,
                label_zh="癌醫中心分院網掛（C0）",
                timetable_how="ntuh_cancer_reg_C0",
                source_url=REG_C0_HUB,
            ),
            self.registration_link_row(
                kind="dept",
                url=SCHEDULE_SOURCES[0]["url"],
                label_zh="乳房醫學中心網掛（KBRC）",
                timetable_how="ntuh_cancer_reg_C0_KBRC",
                source_url=SCHEDULE_SOURCES[0]["url"],
            ),
            self.registration_link_row(
                kind="hub",
                url=HUB_URL,
                label_zh="癌醫中心分院官網",
                timetable_how="ntuh_cancer_hub",
                source_url=HUB_URL,
            ),
            self.registration_link_row(
                kind="progress",
                url=PROGRESS_C0,
                label_zh="看診進度 C0（全院；progress stub）",
                timetable_how="ntuh_cancer_progress_stub",
                notes="ClinicCurrentLightNo all-clinic; filter before emit",
                source_url=PROGRESS_C0,
            ),
        ]

    def normalize(self, raw: Any, *, kind: str) -> list[dict]:
        if kind == "roster":
            html = raw if isinstance(raw, str) else (raw or {}).get("html") or ""
            return parse_roster_from_schedule_html(html, adapter=self)
        if kind == "schedule":
            if isinstance(raw, dict) and "html" in raw:
                meta = SCHEDULE_SOURCES[0]
                week_start = today_taipei()
                weeks = 2
                if raw.get("week_start"):
                    week_start = date.fromisoformat(str(raw["week_start"]))
                if raw.get("weeks"):
                    weeks = int(raw["weeks"])
                return parse_reg_dept_schedule_html(
                    raw["html"],
                    adapter=self,
                    source_meta=meta,
                    date_from=week_start,
                    date_to=week_start + timedelta(weeks=weeks),
                    scraped_at=now_taipei(),
                )
            if isinstance(raw, str):
                return parse_reg_dept_schedule_html(
                    raw,
                    adapter=self,
                    source_meta=SCHEDULE_SOURCES[0],
                    date_from=today_taipei(),
                    date_to=today_taipei() + timedelta(weeks=2),
                    scraped_at=now_taipei(),
                )
            return []
        if kind == "registration":
            return self.fetch_registration_links()
        return []


__all__ = [
    "NtuhCancerAdapter",
    "SCHEDULE_SOURCES",
    "KNOWN_BREAST_TEAM",
    "parse_reg_dept_schedule_html",
    "is_breast_center_slot",
    "HOSPITAL_ID",
    "NHIA_CODE",
]
