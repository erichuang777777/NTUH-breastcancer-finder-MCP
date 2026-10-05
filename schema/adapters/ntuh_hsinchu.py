"""新竹臺大分院 (NTUH Hsinchu / Biomedical Park) adapter.

Public sources (separate from main-campus KBRC PDF):

  Hub / OPD PDFs (monthly; often image-scan — not used for slot emit):
    https://www.hch.gov.tw/?aid=103&iid=1  新竹醫院門診表
    https://www.hch.gov.tw/?aid=103&iid=2  生醫醫院（竹北／竹東）門診表
  Roster:  https://www.hch.gov.tw/?aid=51&pid=21  乳房外科醫療陣容
  Reg T4:  https://reg.ntuh.gov.tw/WebReg/WebReg/BranchIndex?vHospCode=T4
           RegDeptSchedule?vhospCode=T4&showBlock=B&vDeptCode=SURG
           （診別含「乳房」者：乳房外科／乳房暨甲狀腺外科）
  Reg T7:  BranchIndex?vHospCode=T7
           RegDeptSchedule?vhospCode=T7&showBlock=E&vDeptCode=KBRV
           （乳房醫學中心；乳房外科門診；略過泌乳門診）
  Progress: ClinicCurrentLightNo?vHospCode=T4｜T7（全院；stub）

網掛開放約兩週內診次；不發明格子。Polite ≥1s/host。
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

HOSPITAL_ID = "ntuh_hsinchu"
NAME_ZH = "國立臺灣大學醫學院附設醫院新竹臺大分院"
NHIA_CODE = "0412040012"

HUB_URL = "https://www.hch.gov.tw/"
ROSTER_URL = "https://www.hch.gov.tw/?aid=51&pid=21"
OPD_PDF_HSINCHU_URL = "https://www.hch.gov.tw/?aid=103&iid=1"
OPD_PDF_BIOMED_URL = "https://www.hch.gov.tw/?aid=103&iid=2"

REG_BASE = "https://reg.ntuh.gov.tw/WebReg/WebReg/"
REG_T4_HUB = f"{REG_BASE}BranchIndex?vHospCode=T4"
REG_T7_HUB = f"{REG_BASE}BranchIndex?vHospCode=T7"
PROGRESS_T4 = f"{REG_BASE}ClinicCurrentLightNo?vHospCode=T4"
PROGRESS_T7 = f"{REG_BASE}ClinicCurrentLightNo?vHospCode=T7"

# Live schedule sources (dated RegDeptSchedule modals)
SCHEDULE_SOURCES: list[dict[str, str]] = [
    {
        "campus_code": "T4",
        "campus_zh": "新竹醫院",
        "dept_code": "SURG",
        "dept_label": "外科部",
        "block": "B",
        "url": (
            f"{REG_BASE}RegDeptSchedule?vhospCode=T4"
            f"&showBlock=B&vDeptCode=SURG&realSubDeptCode="
        ),
        "reg_hub": REG_T4_HUB,
        "progress": PROGRESS_T4,
    },
    {
        "campus_code": "T7",
        "campus_zh": "生醫醫院竹北",
        "dept_code": "KBRV",
        "dept_label": "乳房醫學中心",
        "block": "E",
        "url": (
            f"{REG_BASE}RegDeptSchedule?vhospCode=T7"
            f"&showBlock=E&vDeptCode=KBRV&realSubDeptCode="
        ),
        "reg_hub": REG_T7_HUB,
        "progress": PROGRESS_T7,
    },
]

# Official 乳房外科 roster (hch.gov.tw aid=51&pid=21) — for notes / plastic filter
KNOWN_BREAST_TEAM: frozenset[str] = frozenset(
    {
        "楊銘棋",
        "趙明",
        "謝永雋",
        "黃俊雄",
        "黃凱傑",
        "陳博彥",
        "李玉觀",
        "黃祥瑋",
        "郭文宏",
        "林郁淳",
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
_SESSION_RE = re.compile(r"(上午|下午|夜間|黃昏|早上|早診|午診|夜診|晚上)門診")


def weekday_zh(d: date) -> str:
    return _WEEKDAY_ZH[d.weekday()]


def roc_to_gregorian(y: int, m: int, d: int) -> date:
    return date(y + 1911, m, d)


def is_breast_clinic_name(clinic: str | None) -> bool:
    """Accept breast surgery / breast-thyroid; skip lactation-only clinics."""
    if not clinic:
        return False
    c = clinic.strip()
    if "泌乳" in c:
        return False
    if "乳房" in c or "乳醫" in c:
        return True
    return False


def specialty_for_clinic(clinic: str | None) -> str:
    c = clinic or ""
    if "重建" in c or "整形" in c:
        return "plastic_reconstruction"
    if "腫瘤內" in c or "血液" in c:
        return "medical_oncology"
    return "breast_surgery"


def map_status(tag_text: str) -> str:
    t = tag_text or ""
    if "停診" in t or "休診" in t:
        return STATUS_CANCELLED
    if "額滿" in t or "已滿" in t:
        return STATUS_FULL
    if "前往掛號" in t or "可掛" in t:
        return STATUS_OPEN
    # Default: published on board without 停診／額滿 → open
    if t.strip():
        return STATUS_OPEN
    return STATUS_UNKNOWN


def parse_reg_dept_schedule_html(
    html: str,
    *,
    adapter: "NtuhHsinchuAdapter",
    source_meta: dict[str, str],
    date_from: date,
    date_to: date,
    scraped_at: datetime,
) -> list[dict]:
    """Parse RegDeptSchedule doctor-tag modals → breast ClinicSlot rows."""
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
        # Fallback: clinic often also in title after 診
        if not clinic and "乳房" in title_t:
            for tok in ("乳房暨甲狀腺外科", "乳房外科門診", "乳房外科", "乳房醫學中心"):
                if tok in title_t:
                    clinic = tok
                    break
        if not is_breast_clinic_name(clinic):
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

        btn = soup.select_one(f'button.doctor-tag[data-bs-target="#{mid}"]')
        btn_t = btn.get_text(" ", strip=True) if btn else title_t
        status = map_status(btn_t)

        notes_parts = [campus_zh, clinic]
        if "代診" in btn_t:
            notes_parts.append("代診不續掛")
        if "到診不續掛" in btn_t:
            notes_parts.append("到診不續掛")
        if "僅存本院初診" in btn_t or "僅存本院初診" in text:
            notes_parts.append("僅存本院初診")
        if booking:
            notes_parts.append(f"預約碼{booking}")

        local_clinic = booking or (f"R{room}" if room else None)
        if booking and room:
            local_clinic = f"{booking}"

        doctor_id = f"{adapter.hospital_id}:{campus_code}:{name}"
        key = (clinic_date, session, name, campus_code, local_clinic or room or "")
        if key in seen:
            continue
        seen.add(key)

        out.append(
            adapter.slot_row(
                name_zh=name,
                clinic_date=clinic_date,
                session=session,
                status=status,
                specialty_tag=specialty_for_clinic(clinic),
                source_url=source_url,
                doctor_id=doctor_id,
                local_doctor_code=name,
                department_zh=clinic or source_meta.get("dept_label") or "乳房外科",
                weekday=weekday_zh(clinic_date),
                local_clinic_code=local_clinic,
                registration_url=source_url,  # RegDeptSchedule deep link
                progress_url=progress,
                notes="; ".join(notes_parts),
                scraped_at=scraped_at,
            )
        )
    return out


def parse_roster_html(html: str, *, adapter: "NtuhHsinchuAdapter") -> list[dict]:
    """Parse hch.gov.tw 乳房外科醫療陣容 → Doctor rows."""
    soup = BeautifulSoup(html or "", "html.parser")
    text = soup.get_text("\n", strip=True)
    names: list[str] = []
    seen: set[str] = set()
    for m in re.finditer(r"([\u4e00-\u9fff·‧]{2,4})\s*醫師", text):
        name = m.group(1)
        if name in ("尋找", "主治", "兼任", "專任"):
            continue
        if name in seen:
            continue
        seen.add(name)
        names.append(name)
    # Prefer known team order when present
    ordered = [n for n in KNOWN_BREAST_TEAM if n in seen] + [
        n for n in names if n not in KNOWN_BREAST_TEAM
    ]
    out: list[dict] = []
    for name in ordered:
        out.append(
            adapter.doctor_row(
                name_zh=name,
                specialty_tags=["breast_surgery"],
                department_zh="乳房外科",
                is_breast_specialist=True,
                source_url=ROSTER_URL,
                source_type="hospital_page",
                registration_url=REG_T4_HUB,
                progress_url=PROGRESS_T4,
                notes="新竹臺大分院乳房外科名冊",
            )
        )
    return out


class NtuhHsinchuAdapter(HospitalAdapter):
    """NTUH Hsinchu / Biomedical — RegDeptSchedule breast clinics (T4 SURG + T7 KBRV)."""

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
                    "ntuh_hsinchu TLS verify failed (%s); retry verify=False",
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
        try:
            html = self._get_text(ROSTER_URL)
        except (AdapterFetchError, AdapterRateLimited) as e:
            self.last_fetch_report = {
                "failures": [{"url": ROSTER_URL, "error": str(e)}],
                "total": 0,
            }
            raise
        doctors = parse_roster_html(html, adapter=self)
        self.last_fetch_report = {
            "source": ROSTER_URL,
            "total": len(doctors),
            "failures": [],
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
                                f"0 breast slots after filter "
                                f"({src['campus_zh']} {src['dept_code']})"
                            ),
                            "phase": "schedule",
                        }
                    )
            except (AdapterFetchError, AdapterRateLimited) as e:
                log.warning("ntuh_hsinchu schedule fetch failed: %s", e)
                failures.append(
                    {"url": url, "error": str(e), "phase": "schedule"}
                )
                per_campus[src["campus_code"]] = 0

        # Dedup
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

        # Drop empty-filter noise if other campus yielded slots
        if slots:
            failures = [
                f
                for f in failures
                if "0 breast slots after filter" not in str(f.get("error") or "")
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
            "opd_pdf_pages": [OPD_PDF_HSINCHU_URL, OPD_PDF_BIOMED_URL],
            "opd_pdf_note": "monthly PDFs often image-scan; slots from RegDeptSchedule",
            "tls_insecure_fallback": bool(self._tls_insecure),
            "nhia_code": NHIA_CODE,
        }
        return slots

    def fetch_live_progress(
        self,
        *,
        breast_only: bool = False,
        fetch_details: bool = True,
        ampm_codes: list[str] | None = None,
        campus_code: str | None = None,
    ) -> list[dict]:
        """ClinicCurrentLightNo T4+T7 — all depts by default; optional breast filter."""
        from .ntuh_progress import fetch_live_progress

        allow = {"T4": set(KNOWN_BREAST_TEAM)} if breast_only else None
        rows = fetch_live_progress(
            hospital_id=self.hospital_id,
            hosp_codes=["T4", "T7"],
            progress_row_fn=self.progress_row,
            name_allowlists=allow,
            breast_only=breast_only,
            fetch_details=fetch_details,
            ampm_codes=ampm_codes,
            campus_code=campus_code,
        )
        self.last_fetch_report = {
            **getattr(self, "last_fetch_report", {}),
            "progress_total": len(rows),
            "progress_hosp_codes": ["T4", "T7"],
            "progress_filter": "breast" if breast_only else "all_depts",
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
                url=HUB_URL,
                label_zh="新竹臺大分院官網",
                timetable_how="ntuh_hsinchu_hub",
                source_url=HUB_URL,
            ),
            self.registration_link_row(
                kind="dept",
                url=SCHEDULE_SOURCES[0]["url"],
                label_zh="新竹醫院外科部網掛（乳房診別過濾）",
                timetable_how="ntuh_hsinchu_reg_T4_SURG",
                source_url=SCHEDULE_SOURCES[0]["url"],
            ),
            self.registration_link_row(
                kind="dept",
                url=SCHEDULE_SOURCES[1]["url"],
                label_zh="生醫醫院乳房醫學中心網掛（KBRV）",
                timetable_how="ntuh_hsinchu_reg_T7_KBRV",
                source_url=SCHEDULE_SOURCES[1]["url"],
            ),
            self.registration_link_row(
                kind="roster",
                url=ROSTER_URL,
                label_zh="乳房外科醫療陣容",
                timetable_how="ntuh_hsinchu_roster",
                source_url=ROSTER_URL,
            ),
            self.registration_link_row(
                kind="progress",
                url=PROGRESS_T4,
                label_zh="看診進度 T4／T7（全院；progress stub）",
                timetable_how="ntuh_hsinchu_progress_stub",
                notes="ClinicCurrentLightNo all-clinic; filter before emit",
                source_url=PROGRESS_T4,
            ),
        ]

    def normalize(self, raw: Any, *, kind: str) -> list[dict]:
        if kind == "roster":
            html = raw if isinstance(raw, str) else (raw or {}).get("html") or ""
            return parse_roster_html(html, adapter=self)
        if kind == "schedule":
            if isinstance(raw, dict) and "html" in raw:
                meta = next(
                    (
                        s
                        for s in SCHEDULE_SOURCES
                        if s["campus_code"] == raw.get("campus_code")
                    ),
                    SCHEDULE_SOURCES[0],
                )
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
    "NtuhHsinchuAdapter",
    "SCHEDULE_SOURCES",
    "KNOWN_BREAST_TEAM",
    "parse_reg_dept_schedule_html",
    "is_breast_clinic_name",
]
