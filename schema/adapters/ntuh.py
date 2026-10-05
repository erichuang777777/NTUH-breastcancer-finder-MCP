"""臺大醫院 (NTUH) adapter — KBRC OPD PDF week template → ClinicSlot.

Primary source (public PDF, monthly refresh on hospital site):
  https://www.ntuh.gov.tw/ckfinder_file/OPD/files/OPD/KBRC.pdf

Roster HTML (optional / secondary):
  https://www.ntuh.gov.tw/BC/Fpage.action?fid=1192&muid=2

Registration hub: https://reg.ntuh.gov.tw/
Progress board (all clinics — must filter before emit):
  https://reg.ntuh.gov.tw/WebReg/WebReg/ClinicCurrentLightNo?vHospCode=T0

Do not invent clinic slots; expand only what the PDF table shows.
"""

from __future__ import annotations

import io
import logging
import re
import time
from datetime import date, datetime, timedelta
from typing import Any

import pdfplumber
import requests
import urllib3
from bs4 import BeautifulSoup

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

from .base import (
    SESSION_AM,
    SESSION_PM,
    STATUS_UNKNOWN,
    AdapterFetchError,
    AdapterParseError,
    AdapterRateLimited,
    HospitalAdapter,
    now_taipei,
    today_taipei,
)

log = logging.getLogger(__name__)

USER_AGENT = "BreastCareResearchBot/0.1 (+public schedule research; polite 1req/s)"

KBRC_PDF_URL = "https://www.ntuh.gov.tw/ckfinder_file/OPD/files/OPD/KBRC.pdf"
ROSTER_URL = "https://www.ntuh.gov.tw/BC/Fpage.action?fid=1192&muid=2"
REG_HUB_URL = "https://reg.ntuh.gov.tw/"
PROGRESS_URL = (
    "https://reg.ntuh.gov.tw/WebReg/WebReg/ClinicCurrentLightNo?vHospCode=T0"
)

_WEEKDAY_COLS = ("一", "二", "三", "四", "五")  # Mon–Fri column order
_WEEKDAY_TO_PY = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6}

_DEPT_TAG = {
    "乳房外科": "breast_surgery",
    "乳房腫瘤": "medical_oncology",
    "乳房重建": "plastic_reconstruction",
    "乳房重建整形": "plastic_reconstruction",
    "婦女保健": "breast_surgery",
}

_DEPT_ALIASES = {
    "乳房重建整形": "乳房重建",
}

# Markers stripped from cell text before name extraction
_MARK_RE = re.compile(r"[○複初約教整菸＃#＊*]+")
_NAME_RE = re.compile(r"([\u4e00-\u9fff·‧]{2,4})")
_WEEK_LIST_RE = re.compile(
    r"(?:第)?([1-5](?:\s*[,，、]\s*[1-5])*)\s*週"
)
_WEEK_SLASH_RE = re.compile(
    r"第\s*([1-5])\s*週?\s*/\s*([1-5](?:\s*[,，、]\s*[1-5])*)\s*週?"
)
_WEEK_SLASH_SHORT_RE = re.compile(
    r"第\s*([1-5])\s*/\s*([1-5])\s*週"
)
_MONTH_RE = re.compile(r"([0-9]{1,2})\s*月")
_ROC_YEAR_RE = re.compile(r"(?<!\d)(1[0-2]\d)\s*年")
_BOOKING_CODE_RE = re.compile(r"^(\d{4,6})$")


def _clean_spaces(s: str) -> str:
    return re.sub(r"\s+", "", (s or "").strip())


def week_of_month(d: date) -> int:
    """Calendar chunk: days 1–7 → 1, 8–14 → 2, … (common TW OPD PDF convention)."""
    return (d.day - 1) // 7 + 1


def roc_to_gregorian_year(roc: int) -> int:
    return roc + 1911


def _parse_week_set(text: str) -> set[int] | None:
    """Return allowed week-of-month numbers, or None if unconstrained."""
    if not text:
        return None
    t = _clean_spaces(text)
    m = _WEEK_SLASH_RE.search(t) or _WEEK_SLASH_SHORT_RE.search(t)
    if m:
        first = {int(m.group(1))}
        rest = {int(x) for x in re.findall(r"[1-5]", m.group(2))}
        return first | rest
    m = _WEEK_LIST_RE.search(t)
    if m:
        return {int(x) for x in re.findall(r"[1-5]", m.group(1))}
    return None


def _split_week_name_pairs(note: str, cell: str) -> list[tuple[set[int] | None, str]]:
    """Map ``第1週/2,4週`` + ``甲/乙`` → [( {1}, 甲), ({2,4}, 乙)].

    Falls back to a single (week_set_from_note, full_cell) pair.
    """
    note_c = _clean_spaces(note)
    cell_c = cell.strip()
    m = _WEEK_SLASH_RE.search(note_c) or _WEEK_SLASH_SHORT_RE.search(note_c)
    parts = [p.strip() for p in re.split(r"[/／]", cell_c) if p.strip()]
    if m and len(parts) >= 2:
        first_weeks = {int(m.group(1))}
        rest_weeks = {int(x) for x in re.findall(r"[1-5]", m.group(2))}
        out: list[tuple[set[int] | None, str]] = [(first_weeks, parts[0])]
        for p in parts[1:]:
            out.append((rest_weeks, p))
        return out
    weeks = _parse_week_set(note_c)
    # Multiple names in one cell without week split → each gets same week constraint
    if len(parts) > 1 and weeks is None:
        return [(None, p) for p in parts]
    if len(parts) > 1:
        return [(weeks, p) for p in parts]
    return [(weeks, cell_c)]


def _cell_flags(raw: str) -> list[str]:
    flags: list[str] = []
    if "約" in raw:
        flags.append("約診")
    if "初" in raw:
        flags.append("初診限")
    if "複" in raw:
        flags.append("複診限")
    if "＃" in raw or "#" in raw:
        flags.append("依報到順序")
    if "教" in raw:
        flags.append("教學門診")
    return flags


def _extract_names(raw: str) -> list[str]:
    """Extract Chinese doctor names from a cell (after removing markers)."""
    cleaned = _MARK_RE.sub("", raw)
    cleaned = cleaned.replace(" ", "")
    names: list[str] = []
    for m in _NAME_RE.finditer(cleaned):
        name = m.group(1)
        # Skip department-like tokens
        if name.endswith("科") or name.endswith("部") or name.endswith("診"):
            continue
        if name in ("婦女保健", "乳房外科", "乳房腫瘤", "乳房重建"):
            continue
        names.append(name)
    return names


def _infer_department(note: str, carried: str | None) -> str | None:
    for key in ("乳房重建整形", "乳房重建", "乳房外科", "乳房腫瘤", "婦女保健"):
        if key in (note or ""):
            return _DEPT_ALIASES.get(key, key)
    return carried


def _specialty_for(dept: str | None) -> str:
    if not dept:
        return "breast_surgery"
    return _DEPT_TAG.get(dept, "breast_surgery")


def _roster_dept_map_from_tables(tables: list) -> dict[str, str]:
    """Parse the small「參考病症 / 醫師名單」table → name_zh → department."""
    mapping: dict[str, str] = {}
    for table in tables:
        if not table or len(table) < 2:
            continue
        for row in table[1:]:
            if not row or len(row) < 2:
                continue
            dept_raw = (row[0] or "").strip()
            names_raw = (row[1] or "").strip()
            if not dept_raw or not names_raw:
                continue
            if "參考" in dept_raw:
                continue
            dept = _DEPT_ALIASES.get(dept_raw, dept_raw)
            if dept not in _DEPT_TAG:
                continue
            blob = re.sub(r"(專任|兼任)\s*[:：]?", "\n", names_raw)
            blob = blob.replace("\n", "、")
            for part in re.split(r"[、,，;；/／\s]+", blob):
                part = _MARK_RE.sub("", part)
                part = re.sub(r"[（(][^）)]*[）)]", "", part).strip()
                if not part:
                    continue
                if re.fullmatch(r"[\u4e00-\u9fff·‧]{2,3}", part):
                    mapping.setdefault(part, dept)
                    continue
                for name in _extract_names(part):
                    if 2 <= len(name) <= 3:
                        mapping.setdefault(name, dept)
    return mapping


def _clinic_code(booking: str | None, room: str | None) -> str | None:
    b = _clean_spaces(booking or "")
    r = _clean_spaces(room or "")
    if b and r:
        return f"{b}{r}"
    if b:
        return b
    if r:
        return r
    return None


def _iter_dates_for_weeks(
    *,
    week_start: date,
    weeks: int,
    year: int,
    month_filter: int | None,
) -> list[date]:
    """Mon–Fri dates in ``[week_start, week_start + weeks)`` (half-open)."""
    end = week_start + timedelta(weeks=weeks)
    out: list[date] = []
    d = week_start
    while d < end:
        if d.weekday() < 5:  # Mon–Fri
            if d.year == year and (
                month_filter is None or d.month == month_filter
            ):
                out.append(d)
        d += timedelta(days=1)
    return out


def parse_kbrc_pdf_bytes(
    data: bytes,
    *,
    hospital_id: str,
    source_url: str,
    week_start: date | None = None,
    weeks: int = 2,
    scraped_at: datetime | None = None,
    adapter: "NtuhAdapter | None" = None,
) -> list[dict]:
    """Pure parse of KBRC OPD PDF → ClinicSlot dicts."""
    helper = adapter or NtuhAdapter()
    ts = scraped_at or now_taipei()
    week_start = week_start or today_taipei()
    weeks = max(1, min(int(weeks), 8))

    with pdfplumber.open(io.BytesIO(data)) as pdf:
        if not pdf.pages:
            return []
        page = pdf.pages[0]
        text = page.extract_text() or ""
        tables = page.extract_tables() or []

    # Year: prefer「115 年」in title; else Taipei today.
    roc_m = _ROC_YEAR_RE.search(text.replace(" ", ""))
    if roc_m:
        year = roc_to_gregorian_year(int(roc_m.group(1)))
    else:
        year = today_taipei().year

    doctor_dept = _roster_dept_map_from_tables(tables)

    # Find the wide schedule table (上午|下午 header).
    schedule = None
    for table in tables:
        for row in table:
            joined = " ".join((c or "") for c in row)
            if "星期一" in joined and "上午" not in joined and "預約碼" in joined:
                schedule = table
                break
            if "星期一" in joined and "預約碼" in joined:
                schedule = table
                break
        if schedule is not None:
            break
    if schedule is None:
        # Fallback: largest table
        schedule = max(tables, key=len) if tables else None
    if not schedule:
        return []

    # Locate header row
    header_idx = None
    for i, row in enumerate(schedule):
        cells = [(c or "").replace(" ", "") for c in row]
        if any("星期一" in c for c in cells) and any("預約碼" in c for c in cells):
            header_idx = i
            break
    if header_idx is None:
        return []

    out: list[dict] = []
    seen: set[tuple] = set()
    am_dept: str | None = "乳房外科"
    pm_dept: str | None = "乳房外科"

    for row in schedule[header_idx + 1 :]:
        if not row or len(row) < 18:
            # Pad short rows
            row = list(row) + [""] * (18 - len(row))
        cells = [(c if c is not None else "") for c in row]

        am_note, am_book, am_room = cells[0], cells[1], cells[2]
        am_days = cells[3:8]  # Mon–Fri
        am_remark = cells[8]
        pm_note, pm_book, pm_room = cells[9], cells[10], cells[11]
        pm_days = cells[12:17]
        pm_remark = cells[17]

        am_dept = _infer_department(am_note, am_dept)
        pm_dept = _infer_department(pm_note, pm_dept)
        # If PM note has only week constraint, keep previous pm_dept
        if pm_note and "婦女保健" in _clean_spaces(pm_note):
            pm_dept = "婦女保健"

        for session, note, book, room, day_cells, remark, dept in (
            (SESSION_AM, am_note, am_book, am_room, am_days, am_remark, am_dept),
            (SESSION_PM, pm_note, pm_book, pm_room, pm_days, pm_remark, pm_dept),
        ):
            month_filter = None
            for blob in (note, remark):
                mm = _MONTH_RE.search(_clean_spaces(blob))
                if mm:
                    month_filter = int(mm.group(1))
                    break

            code = _clinic_code(book, room)
            dates = _iter_dates_for_weeks(
                week_start=week_start,
                weeks=weeks,
                year=year,
                month_filter=month_filter,
            )
            # Also allow spilling into adjacent month when PDF covers it
            # (e.g. late-month fetch of next month template) — if month_filter
            # set, dates already filtered.

            for col_i, raw_cell in enumerate(day_cells):
                raw = (raw_cell or "").strip()
                if not raw:
                    continue
                wd = _WEEKDAY_COLS[col_i]
                pairs = _split_week_name_pairs(note, raw)
                for week_set, piece in pairs:
                    flags = _cell_flags(piece) or _cell_flags(raw)
                    names = _extract_names(piece)
                    if not names:
                        continue
                    for name in names:
                        for d in dates:
                            if d.weekday() != _WEEKDAY_TO_PY[wd]:
                                continue
                            if week_set is not None and week_of_month(d) not in week_set:
                                continue
                            # If no explicit month filter but PDF is for a
                            # specific month labelled in title area, still
                            # emit any matching weekday in window.
                            key = (d.isoformat(), session, name, code or "")
                            if key in seen:
                                continue
                            seen.add(key)
                            notes_parts = ["KBRC門診表PDF"]
                            notes_parts.extend(flags)
                            if week_set:
                                notes_parts.append(
                                    "第" + ",".join(str(w) for w in sorted(week_set)) + "週"
                                )
                            if month_filter:
                                notes_parts.append(f"{month_filter}月")
                            if code:
                                notes_parts.append(f"診{code}")
                            dept_zh = doctor_dept.get(name) or dept or "乳房醫學中心"
                            row_dict = helper.slot_row(
                                name_zh=name,
                                clinic_date=d,
                                session=session,
                                status=STATUS_UNKNOWN,
                                specialty_tag=_specialty_for(dept_zh),
                                source_url=source_url,
                                doctor_id=f"{hospital_id}:{name}",
                                local_clinic_code=code,
                                department_zh=dept_zh,
                                weekday=wd,
                                registration_url=REG_HUB_URL,
                                progress_url=PROGRESS_URL,
                                notes=";".join(notes_parts),
                                scraped_at=ts,
                            )
                            out.append(row_dict)
    return out


def parse_roster_html(html: str, *, adapter: "NtuhAdapter") -> list[dict]:
    """Parse BC physician intro table → Doctor rows (public HTML)."""
    soup = BeautifulSoup(html, "html.parser")
    out: list[dict] = []
    seen: set[str] = set()
    for table in soup.find_all("table"):
        headers = [th.get_text(strip=True) for th in table.find_all("th")]
        if not headers:
            first = table.find("tr")
            if first:
                headers = [c.get_text(strip=True) for c in first.find_all(["td", "th"])]
        name_idx = next(
            (i for i, h in enumerate(headers) if "姓名" in h or h == "醫師"), None
        )
        if name_idx is None:
            continue
        spec_idx = next((i for i, h in enumerate(headers) if "專長" in h), None)
        title_idx = next((i for i, h in enumerate(headers) if "職稱" in h), None)
        for tr in table.find_all("tr")[1:]:
            cells = [c.get_text(strip=True) for c in tr.find_all(["td", "th"])]
            if len(cells) <= name_idx:
                continue
            name = _clean_spaces(cells[name_idx])
            names = _extract_names(name) or ([name] if 2 <= len(name) <= 4 else [])
            if not names:
                continue
            name = names[0]
            if name in seen:
                continue
            seen.add(name)
            specialty = cells[spec_idx] if spec_idx is not None and spec_idx < len(cells) else ""
            title = cells[title_idx] if title_idx is not None and title_idx < len(cells) else ""
            notes = specialty or title or "乳房醫學中心名冊"
            tag = "breast_surgery"
            blob = specialty + title
            if "重建" in blob or "整形" in blob:
                tag = "plastic_reconstruction"
            elif "腫瘤" in blob or "化學" in blob or "內科" in blob:
                tag = "medical_oncology"
            out.append(
                adapter.doctor_row(
                    name_zh=name,
                    specialty_tags=[tag],
                    department_zh="乳房醫學中心",
                    is_breast_specialist=None,
                    source_url=ROSTER_URL,
                    source_type="hospital_page",
                    registration_url=REG_HUB_URL,
                    progress_url=PROGRESS_URL,
                    notes=notes[:200],
                )
            )
    return out


class NtuhAdapter(HospitalAdapter):
    """NTUH breast center — KBRC PDF schedule + optional roster HTML."""

    hospital_id = "ntuh"
    name_zh = "國立臺灣大學醫學院附設醫院"
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

    def _get_bytes(self, url: str) -> bytes:
        """GET bytes; NTUH TLS may fail OpenSSL SKI check — retry once unverified."""
        self._throttle()
        verify: bool | str = True
        try:
            try:
                resp = self._session.get(url, timeout=60, verify=verify)
            except requests.exceptions.SSLError as ssl_err:
                # TWCA / *.ntuh.gov.tw: "Missing Subject Key Identifier" on
                # newer OpenSSL. Public PDF only; log and retry without verify.
                log.warning(
                    "ntuh TLS verify failed (%s); retrying with verify=False",
                    ssl_err,
                )
                self._tls_insecure = True
                verify = False
                self._throttle()
                resp = self._session.get(url, timeout=60, verify=False)
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
        return resp.content

    def _get_text(self, url: str) -> str:
        data = self._get_bytes(url)
        # Prefer UTF-8; NTUH pages are usually UTF-8
        for enc in ("utf-8", "big5", "cp950"):
            try:
                return data.decode(enc)
            except UnicodeDecodeError:
                continue
        return data.decode("utf-8", errors="replace")

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
        """Download KBRC.pdf and expand weekday template into dated slots."""
        scraped_at = now_taipei()
        week_start = week_start or today_taipei()
        weeks = max(1, min(int(weeks), 8))
        failures: list[dict[str, str]] = []

        try:
            pdf_bytes = self._get_bytes(KBRC_PDF_URL)
        except (AdapterFetchError, AdapterRateLimited) as e:
            log.warning("ntuh KBRC fetch failed: %s", e)
            failures.append({"url": KBRC_PDF_URL, "error": str(e)})
            self.last_fetch_report = {
                "failures": failures,
                "weeks": weeks,
                "total": 0,
                "scraped_at": scraped_at.isoformat(timespec="seconds"),
                "source": KBRC_PDF_URL,
            }
            return []

        try:
            slots = parse_kbrc_pdf_bytes(
                pdf_bytes,
                hospital_id=self.hospital_id,
                source_url=KBRC_PDF_URL,
                week_start=week_start,
                weeks=weeks,
                scraped_at=scraped_at,
                adapter=self,
            )
        except Exception as e:
            log.exception("ntuh KBRC parse failed")
            failures.append({"url": KBRC_PDF_URL, "error": f"parse: {e}"})
            slots = []
            if not isinstance(e, AdapterParseError):
                # Surface as empty + failure rather than crash the runner
                pass

        if not slots and not failures:
            failures.append(
                {"url": KBRC_PDF_URL, "error": "parsed 0 slots from KBRC PDF"}
            )

        self.last_fetch_report = {
            "failures": failures,
            "weeks": weeks,
            "total": len(slots),
            "scraped_at": scraped_at.isoformat(timespec="seconds"),
            "source": KBRC_PDF_URL,
            "week_start": week_start.isoformat(),
            "per_campus": {self.hospital_id: len(slots)},
            "tls_insecure_fallback": bool(self._tls_insecure),
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
        """ClinicCurrentLightNo T0 — all depts by default; optional breast filter."""
        from .ntuh_progress import fetch_live_progress

        rows = fetch_live_progress(
            hospital_id=self.hospital_id,
            hosp_codes=["T0"],
            progress_row_fn=self.progress_row,
            client=None,
            breast_only=breast_only,
            fetch_details=fetch_details,
            ampm_codes=ampm_codes,
            campus_code=campus_code,
        )
        self.last_fetch_report = {
            **getattr(self, "last_fetch_report", {}),
            "progress_total": len(rows),
            "progress_hosp_codes": ["T0"],
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
                url=REG_HUB_URL,
                label_zh="臺大醫院網路掛號",
                timetable_how="ntuh_kbrc_pdf",
                source_url=KBRC_PDF_URL,
            ),
            self.registration_link_row(
                kind="dept",
                url=KBRC_PDF_URL,
                label_zh="乳房醫學中心門診時間表（KBRC PDF）",
                timetable_how="ntuh_kbrc_pdf",
                source_url=KBRC_PDF_URL,
            ),
            self.registration_link_row(
                kind="progress",
                url=PROGRESS_URL,
                label_zh="看診進度（全科燈號）",
                timetable_how="ntuh_clinic_current_light_all_depts",
                notes=(
                    "ClinicCurrentLightNo?vHospCode=T0; default fetch is all departments"
                ),
                source_url=PROGRESS_URL,
            ),
        ]

    def normalize(self, raw: Any, *, kind: str) -> list[dict]:
        if kind == "schedule":
            if isinstance(raw, (bytes, bytearray)):
                return parse_kbrc_pdf_bytes(
                    bytes(raw),
                    hospital_id=self.hospital_id,
                    source_url=KBRC_PDF_URL,
                    adapter=self,
                )
            if isinstance(raw, dict) and "pdf_bytes" in raw:
                return parse_kbrc_pdf_bytes(
                    raw["pdf_bytes"],
                    hospital_id=raw.get("hospital_id") or self.hospital_id,
                    source_url=raw.get("source_url") or KBRC_PDF_URL,
                    week_start=raw.get("week_start"),
                    weeks=int(raw.get("weeks") or 2),
                    adapter=self,
                )
            raise ValueError("schedule normalize expects pdf bytes or dict with pdf_bytes")
        if kind == "roster":
            html = raw if isinstance(raw, str) else (raw or {}).get("html", "")
            return parse_roster_html(html, adapter=self)
        if kind in ("progress", "registration"):
            return []
        raise ValueError(f"unknown kind: {kind!r}")
