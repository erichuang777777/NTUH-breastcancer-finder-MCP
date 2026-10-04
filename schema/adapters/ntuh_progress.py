"""Shared NTUH ClinicCurrentLightNo → LiveProgress helpers (T0/C0/T4/T7).

Public AJAX:
  GET  GetDeptList?vHospitalCode=&RegionCode=
  POST DeptLightTable (HTML cards)
  GET  ClinicCurrentLightNoDetail?ServiceIDSE=&vHospitalCode=

Filter to breast depts (KBRC/KBRV) or name allowlist (T4 SURG).
"""

from __future__ import annotations

import logging
import re
import time
from datetime import date
from typing import Any, Callable

import requests
import urllib3
from bs4 import BeautifulSoup

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

log = logging.getLogger(__name__)

REG_BASE = "https://reg.ntuh.gov.tw/WebReg/WebReg/"
USER_AGENT = "BreastCareResearchBot/0.1 (+public schedule research; polite 1req/s)"

# hosp_code → list of (dept_code, specialty_hint, name_allowlist|None)
PROGRESS_TARGETS: dict[str, list[dict[str, Any]]] = {
    "T0": [{"dept_code": "KBRC", "dept_zh": "乳房醫學中心", "specialty_tag": "breast_surgery"}],
    "C0": [{"dept_code": "KBRC", "dept_zh": "乳房醫學中心", "specialty_tag": "breast_surgery"}],
    "T7": [{"dept_code": "KBRV", "dept_zh": "乳房醫學中心", "specialty_tag": "breast_surgery"}],
    "T4": [
        {
            "dept_code": "SURG",
            "dept_zh": "外科部",
            "specialty_tag": "breast_surgery",
            # When set, keep cards whose doctor name is in this set (optional).
            "name_allowlist": None,  # filled by caller if known
            "require_breast_keyword_in_detail": True,
        }
    ],
}

_SESSION_FROM_AMPM = {"1": "上午", "2": "下午", "3": "夜間"}
_NUM_RE = re.compile(r"(\d+)")


def _soup(html: str):
    return BeautifulSoup(html or "", "html.parser")


def _parse_number_text(text: str) -> int | str | None:
    t = (text or "").strip().replace("\u3000", " ").replace("　", "")
    if not t or set(t) <= {"-", "–", "—", "."}:
        return None
    if t in ("------", "---", "—", "–"):
        return None
    m = _NUM_RE.search(t)
    if m:
        try:
            return int(m.group(1))
        except ValueError:
            return m.group(1)
    return t


def parse_light_detail_html(
    html: str,
    *,
    source_url: str,
    hospital_id: str,
    hosp_code: str,
    clinic_date: date,
    session: str | None,
    specialty_tag: str | None,
    progress_row_fn: Callable[..., dict],
) -> dict | None:
    """Parse ClinicCurrentLightNoDetail → one LiveProgress dict or None."""
    soup = _soup(html)
    room_el = soup.select_one(".room-number")
    room = room_el.get_text(" ", strip=True) if room_el else ""
    # Doctor name often near room
    doc = None
    for sel in (".clinic-doc-name", ".doctor-name", "h5", ".card-body"):
        el = soup.select_one(sel)
        if el:
            t = el.get_text(" ", strip=True)
            # try first CJK name-looking token
            m = re.search(r"([\u4e00-\u9fff·‧]{2,4})", t)
            if m and m.group(1) not in ("目前燈號", "已叫最大", "預計叫號", "乳房醫學", "主治醫師", "住院醫師", "看診中", "未報到", "已報到", "所有燈號"):
                doc = m.group(1)
                break
    # Prefer explicit doc name from list page context if embedded
    now_el = soup.select_one(".now-number .number")
    big_el = soup.select_one(".biggest-number .number")
    next_el = soup.select_one(".next-number .number")
    current = _parse_number_text(now_el.get_text(" ", strip=True) if now_el else "")
    biggest = _parse_number_text(big_el.get_text(" ", strip=True) if big_el else "")
    nxt = _parse_number_text(next_el.get_text(" ", strip=True) if next_el else "")

    # status from progress-number / 看診中
    status_text = None
    prog = soup.select_one(".progress-number")
    if prog:
        status_text = prog.get_text(" ", strip=True)[:80]
    # Do not treat the legend word 看診中 in the page chrome as a live status.

    local_clinic = None
    rm = re.search(r"(\d+)\s*診", room)
    if rm:
        local_clinic = rm.group(1).zfill(2)

    name_zh = doc
    # department from room prefix
    dept_zh = None
    if "乳房" in room:
        dept_zh = "乳房醫學中心"
    elif room:
        dept_zh = room.split()[0] if room else None

    doctor_id = f"{hospital_id}:{name_zh}" if name_zh else None
    if hospital_id == "ntuh_hsinchu" and name_zh:
        doctor_id = f"{hospital_id}:{hosp_code}:{name_zh}"

    return progress_row_fn(
        clinic_date=clinic_date,
        source_url=source_url,
        name_zh=name_zh,
        doctor_id=doctor_id,
        department_zh=dept_zh or room or None,
        session=session,
        local_clinic_code=local_clinic,
        current_number=current,
        next_number=nxt,
        max_number=biggest,
        waiting_count=None,
        status_text=status_text,
        specialty_tag=specialty_tag,
        raw={"hosp_code": hosp_code, "room": room},
    )


def parse_dept_light_cards(html: str) -> list[dict[str, str]]:
    """Extract card list: room, name, detail_href, clinic_type."""
    soup = _soup(html)
    out: list[dict[str, str]] = []
    for card in soup.select(".clinic-room-number"):
        a = card.find("a", href=True)
        room_el = card.select_one(".room-number")
        name_el = card.select_one(".clinic-doc-name")
        typ_el = card.select_one(".clinic-type")
        out.append(
            {
                "room": room_el.get_text(" ", strip=True) if room_el else "",
                "name_zh": name_el.get_text(" ", strip=True) if name_el else "",
                "clinic_type": typ_el.get_text(" ", strip=True) if typ_el else "",
                "detail_href": a["href"] if a else "",
            }
        )
    return out


class NtuhProgressClient:
    """Polite requests client for ClinicCurrentLightNo family."""

    def __init__(
        self,
        *,
        session: requests.Session | None = None,
        min_interval_sec: float = 1.2,
    ) -> None:
        self._session = session or requests.Session()
        self._session.headers.setdefault("User-Agent", USER_AGENT)
        self._session.headers.setdefault(
            "Accept-Language", "zh-TW,zh;q=0.9,en;q=0.5"
        )
        self.min_interval_sec = min_interval_sec
        self._last = 0.0
        self._tls_insecure = False

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last
        wait = self.min_interval_sec - elapsed
        if wait > 0:
            time.sleep(wait)

    def _req(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        self._throttle()
        kwargs.setdefault("timeout", 45)
        try:
            try:
                resp = self._session.request(method, url, verify=True, **kwargs)
            except requests.exceptions.SSLError:
                self._tls_insecure = True
                self._throttle()
                resp = self._session.request(method, url, verify=False, **kwargs)
        finally:
            self._last = time.monotonic()
        return resp

    def fetch_token(self, hosp_code: str) -> str:
        url = f"{REG_BASE}ClinicCurrentLightNo?vHospCode={hosp_code}"
        resp = self._req("GET", url)
        resp.raise_for_status()
        soup = _soup(resp.text)
        el = soup.find("input", {"name": "__RequestVerificationToken"})
        if not el or not el.get("value"):
            raise RuntimeError("missing __RequestVerificationToken on progress page")
        return el["value"]

    def dept_light_table(
        self,
        *,
        hosp_code: str,
        dept_code: str,
        ampm_code: str,
        token: str,
        region_code: str = "",
    ) -> str:
        resp = self._req(
            "POST",
            f"{REG_BASE}DeptLightTable",
            data={
                "__RequestVerificationToken": token,
                "vHospitalCode": hosp_code,
                "DeptCode": dept_code,
                "RegionCode": region_code,
                "AmpmCode": ampm_code,
            },
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        resp.raise_for_status()
        return resp.text

    def detail(self, href: str) -> str:
        url = href if href.startswith("http") else f"https://reg.ntuh.gov.tw{href}"
        resp = self._req("GET", url)
        resp.raise_for_status()
        return resp.text


def fetch_breast_live_progress(
    *,
    hospital_id: str,
    hosp_codes: list[str],
    progress_row_fn: Callable[..., dict],
    clinic_date: date | None = None,
    ampm_codes: list[str] | None = None,
    name_allowlists: dict[str, set[str]] | None = None,
    client: NtuhProgressClient | None = None,
    fetch_details: bool = True,
) -> list[dict]:
    """Fetch + filter LiveProgress rows for NTUH family hosp codes."""
    from schema.adapters.base import today_taipei

    clinic_date = clinic_date or today_taipei()
    ampm_codes = ampm_codes or ["1", "2", "3"]
    name_allowlists = name_allowlists or {}
    client = client or NtuhProgressClient()
    out: list[dict] = []
    seen: set[tuple] = set()

    for hosp in hosp_codes:
        targets = PROGRESS_TARGETS.get(hosp, [])
        if not targets:
            continue
        try:
            token = client.fetch_token(hosp)
        except Exception as e:
            log.warning("progress token %s failed: %s", hosp, e)
            continue
        for tgt in targets:
            dept = tgt["dept_code"]
            allow = name_allowlists.get(hosp) or tgt.get("name_allowlist")
            for ampm in ampm_codes:
                session = _SESSION_FROM_AMPM.get(ampm)
                try:
                    html = client.dept_light_table(
                        hosp_code=hosp,
                        dept_code=dept,
                        ampm_code=ampm,
                        token=token,
                    )
                except Exception as e:
                    log.warning(
                        "DeptLightTable %s %s ampm=%s: %s", hosp, dept, ampm, e
                    )
                    continue
                cards = parse_dept_light_cards(html)
                for card in cards:
                    name = card.get("name_zh") or ""
                    if name in {
                        "主治醫師",
                        "住院醫師",
                        "總醫師",
                        "醫師",
                        "看診中",
                    }:
                        name = ""
                    if allow and name and name not in allow:
                        continue
                    # T4 SURG: optional breast keyword gate on detail only
                    detail_href = card.get("detail_href") or ""
                    source = (
                        f"https://reg.ntuh.gov.tw{detail_href}"
                        if detail_href.startswith("/")
                        else detail_href
                        or f"{REG_BASE}ClinicCurrentLightNo?vHospCode={hosp}"
                    )
                    key = (hosp, clinic_date.isoformat(), session, name, card.get("room"))
                    if key in seen:
                        continue
                    seen.add(key)

                    if fetch_details and detail_href:
                        try:
                            dhtml = client.detail(detail_href)
                            # T4: skip non-breast clinics if flag set
                            if tgt.get("require_breast_keyword_in_detail"):
                                if "乳房" not in dhtml and "乳醫" not in dhtml:
                                    # still allow if name on allowlist
                                    if not (allow and name in allow):
                                        continue
                            row = parse_light_detail_html(
                                dhtml,
                                source_url=source,
                                hospital_id=hospital_id,
                                hosp_code=hosp,
                                clinic_date=clinic_date,
                                session=session,
                                specialty_tag=tgt.get("specialty_tag"),
                                progress_row_fn=progress_row_fn,
                            )
                            if row:
                                # Prefer card doctor name if detail parse weak
                                if not row.get("name_zh") and name:
                                    row["name_zh"] = name
                                    row["doctor_id"] = (
                                        f"{hospital_id}:{hosp}:{name}"
                                        if hospital_id == "ntuh_hsinchu"
                                        else f"{hospital_id}:{name}"
                                    )
                                out.append(row)
                            continue
                        except Exception as e:
                            log.warning("detail fetch failed %s: %s", detail_href, e)

                    # Card-only stub row (no current number)
                    local = None
                    rm = re.search(r"(\d+)\s*診", card.get("room") or "")
                    if rm:
                        local = rm.group(1).zfill(2)
                    did = None
                    if name:
                        did = (
                            f"{hospital_id}:{hosp}:{name}"
                            if hospital_id == "ntuh_hsinchu"
                            else f"{hospital_id}:{name}"
                        )
                    out.append(
                        progress_row_fn(
                            clinic_date=clinic_date,
                            source_url=source,
                            name_zh=name or None,
                            doctor_id=did,
                            department_zh=tgt.get("dept_zh"),
                            session=session,
                            local_clinic_code=local,
                            current_number=None,
                            next_number=None,
                            max_number=None,
                            status_text=card.get("clinic_type") or None,
                            specialty_tag=tgt.get("specialty_tag"),
                            raw={"hosp_code": hosp, "card_only": True},
                        )
                    )
    return out


__all__ = [
    "PROGRESS_TARGETS",
    "NtuhProgressClient",
    "fetch_breast_live_progress",
    "parse_dept_light_cards",
    "parse_light_detail_html",
]
