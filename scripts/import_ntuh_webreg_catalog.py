#!/usr/bin/env python3
"""Import the public NTUH WebReg catalog for every campus and department.

Source (no login): https://reg.ntuh.gov.tw/WebReg/WebReg/RegShowBlock?vHospCode=XX

For each campus code the script reads the department list, then each
``RegDeptSchedule`` page in the currently published registration window, and
upserts doctors + clinic_slots into ``data/ntuh.db``.

This is the bookable roster (physicians who have a clinic in the open window),
not a staff-directory of every employed physician. Departments with no clinic
in the window are recorded on the scrape_runs row but do not invent doctors.

Campus codes (confirmed on WebReg BranchIndex, 2026-10):
  T0 總院 → ntuh
  CH 兒童醫院 → ntuh_children
  C0 癌醫 → ntuh_cancer
  T2 北護分院 → ntuh_beihu
  T3 金山分院 → ntuh_jinshan
  T4 新竹醫院 → ntuh_hsinchu   (same hospital_id as T7; notes carry T4)
  T7 生醫（竹北／竹東）→ ntuh_hsinchu
  Y0 雲林（斗六／虎尾）→ ntuh_yunlin

TLS: reg.ntuh.gov.tw currently fails certificate verification
(Missing Subject Key Identifier). This script falls back to verify=False,
same as schema/registration/ntuh.py. Re-run from a Taiwan IP if the host
blocks this network.

Usage:
  PYTHONPATH=. python scripts/import_ntuh_webreg_catalog.py
  PYTHONPATH=. python scripts/import_ntuh_webreg_catalog.py --campus T0
  PYTHONPATH=. python scripts/import_ntuh_webreg_catalog.py --db data/ntuh.db
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlparse

import requests
import urllib3
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.lib.schema_sql import ensure_schema  # noqa: E402

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

TAIPEI = timezone(timedelta(hours=8))
REG_ORIGIN = "https://reg.ntuh.gov.tw"
REG_BASE = f"{REG_ORIGIN}/WebReg/WebReg/"
USER_AGENT = (
    "NTUHCatalogBot/1.0 (+public WebReg roster; polite; no patient data)"
)

# vHospCode → hospital_id in ntuh.db
CAMPUS_HOSPITAL = {
    "T0": "ntuh",
    "CH": "ntuh_children",
    "C0": "ntuh_cancer",
    "T2": "ntuh_beihu",
    "T3": "ntuh_jinshan",
    "T4": "ntuh_hsinchu",
    "T7": "ntuh_hsinchu",
    "Y0": "ntuh_yunlin",
}

CAMPUS_LABEL = {
    "T0": "總院",
    "CH": "兒童醫院",
    "C0": "癌醫中心分院",
    "T2": "北護分院",
    "T3": "金山分院",
    "T4": "新竹醫院",
    "T7": "生醫（竹北／竹東）",
    "Y0": "雲林（斗六／虎尾）",
}

# WebReg department label → extra search tokens (equivalent specialty names).
DEPT_ALIASES = {
    "影像醫學部": ["放射科", "radiology"],
    "麻醉部": ["麻醉科", "anesthesiology"],
    "復健部": ["復健科", "rehabilitation"],
    "骨科部": ["骨科", "orthopedics"],
    "皮膚部": ["皮膚科", "dermatology"],
    "婦產部": ["婦產科", "obstetrics"],
    "泌尿部": ["泌尿科", "urology"],
    "眼科部": ["眼科", "ophthalmology"],
    "耳鼻喉部": ["耳鼻喉科", "otolaryngology"],
    "神經部": ["神經科", "neurology"],
    "精神部": ["精神科", "psychiatry"],
    "內科部": ["內科", "internal_medicine"],
    "外科部": ["外科", "surgery"],
    "小兒部": ["小兒科", "pediatrics"],
    "家庭醫學部": ["家醫科", "family_medicine"],
    "腫瘤醫學部": ["腫瘤科", "oncology"],
    "核子醫學部": ["核醫科", "nuclear_medicine"],
    "放射腫瘤部": ["放射腫瘤科", "radiation_oncology"],
    "口腔醫學部": ["牙科", "dentistry"],
    "牙科部": ["牙科", "dentistry"],
}

_NAME_RE = re.compile(r"^[\u4e00-\u9fff·‧]{2,5}$")
_ROC_RE = re.compile(r"(1\d{2})\.(\d{1,2})\.(\d{1,2})")
_BOOK_RE = re.compile(r"預約碼\s*[:：]\s*([0-9A-Za-z]+)")
_ROOM_RE = re.compile(r"第\s*(\d+)\s*診")


def _now() -> str:
    return datetime.now(TAIPEI).isoformat(timespec="seconds")


def _iso_from_roc(text: str) -> str | None:
    m = _ROC_RE.search(text or "")
    if not m:
        return None
    y = int(m.group(1)) + 1911
    return f"{y:04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"


def _weekday(iso: str) -> str:
    d = datetime.strptime(iso, "%Y-%m-%d")
    return "一二三四五六日"[d.weekday()]


class Fetcher:
    def __init__(self, interval: float) -> None:
        self.interval = interval
        self.s = requests.Session()
        self.s.headers["User-Agent"] = USER_AGENT
        self.s.headers["Accept-Language"] = "zh-TW,zh;q=0.9"
        self._last = 0.0
        self.verify = True
        self.tls_insecure = False

    def get(self, url: str) -> str:
        wait = self.interval - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        try:
            resp = self.s.get(url, timeout=90, verify=self.verify)
        except requests.exceptions.SSLError:
            self.verify = False
            self.tls_insecure = True
            resp = self.s.get(url, timeout=90, verify=False)
        finally:
            self._last = time.monotonic()
        resp.raise_for_status()
        resp.encoding = resp.apparent_encoding or "utf-8"
        return resp.text


def _q(url: str, key: str) -> str:
    q = parse_qs(urlparse(url).query)
    for k, vals in q.items():
        if k.lower() == key.lower() and vals:
            return vals[0]
    return ""


def absolute(href: str) -> str:
    if href.startswith("http"):
        return href
    if href.startswith("/"):
        return REG_ORIGIN + href
    return urljoin(REG_BASE, href)



def _prefer_named_departments(depts: list[dict]) -> list[dict]:
    """Drop code-only duplicate links (RegDeptInfo sub-filters).

    The block page already links each department's full schedule under a
    Chinese label. RegDeptInfo repeats that department once per sub-clinic
    with the label equal to ``vDeptCode`` and a smaller filtered table.
    Keep a code-only link only when no Chinese-labeled link exists for the
    same campus + dept code. Always keep a Chinese-labeled sub-clinic
    (for example 手術加速康復) even when it shares a dept code.
    """
    named = {
        (d["vhosp"], d["dept_code"])
        for d in depts
        if d["label"] and d["label"] != d["dept_code"]
    }
    out: list[dict] = []
    seen = set()
    for d in depts:
        key = (d["vhosp"], d["dept_code"], d["show_block"], d["sub"])
        if key in seen:
            continue
        if d["label"] == d["dept_code"] and (d["vhosp"], d["dept_code"]) in named:
            continue
        seen.add(key)
        out.append(d)
    return out


def discover_departments(html: str, campus: str) -> list[dict]:
    """Unique RegDeptSchedule targets on a block or dept-info page."""
    soup = BeautifulSoup(html, "html.parser")
    found: dict[tuple, dict] = {}
    for a in soup.find_all("a"):
        href = a.get("href") or ""
        if "RegDeptSchedule" not in href:
            continue
        full = absolute(href)
        hosp = (_q(full, "vHospCode") or campus).upper()
        dept = _q(full, "vDeptCode")
        block = _q(full, "showBlock")
        sub = _q(full, "realSubDeptCode")
        if not dept or dept == "showAll":
            continue
        if hosp not in CAMPUS_HOSPITAL:
            continue
        label = " ".join(a.get_text(" ", strip=True).split())
        label = re.sub(r"\s*掛號\s*$", "", label).strip() or dept
        key = (hosp, dept, block, sub)
        url = (
            f"{REG_BASE}RegDeptSchedule?vHospCode={hosp}&showBlock={block}"
            f"&vDeptCode={dept}&realSubDeptCode={sub}"
        )
        found.setdefault(
            key,
            {
                "vhosp": hosp,
                "dept_code": dept,
                "show_block": block,
                "sub": sub,
                "label": label,
                "url": url,
            },
        )
    return list(found.values())


def _modal_fields(modal) -> dict[str, str]:
    lines = [ln.strip() for ln in modal.get_text("\n").splitlines() if ln.strip()]
    data: dict[str, str] = {}
    keys = {"看診時間", "科別", "診別", "看診地點", "門診備註"}
    i = 0
    while i < len(lines):
        if lines[i] in keys and i + 1 < len(lines) and lines[i + 1] not in keys:
            data[lines[i]] = lines[i + 1]
            i += 2
            continue
        m = _BOOK_RE.search(lines[i])
        if m:
            data["預約碼"] = m.group(1)
        if "看診時間" not in data and _ROC_RE.search(lines[i]):
            data["看診時間"] = lines[i]
        i += 1
    return data


def _session_of(text: str) -> str | None:
    for sess in ("上午", "下午", "夜間"):
        if f"{sess}門診" in text or text.strip() == sess:
            return sess
    return None


def _status_of(btn, blob: str) -> str:
    cls = btn.get("class") or []
    if "avaliable" in cls or "available" in cls:
        return "可掛號"
    if "停診" in blob or "颱風停診" in blob:
        return "停診"
    if "額滿" in blob:
        return "額滿"
    return "未知"


def _clean_name(raw: str) -> str | None:
    name = (raw or "").split("|")[0]
    name = re.sub(r"[（(].*?[）)]", "", name)
    name = re.sub(r"\s+", "", name)
    name = name.replace("醫師", "").replace("醫生", "")
    if _NAME_RE.fullmatch(name):
        return name
    return None


def parse_schedule(html: str, dept: dict) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    rows: list[dict] = []
    for btn in soup.select("button.doctor-tag"):
        doc_el = btn.select_one(".doc-name")
        raw_name = doc_el.get_text(" ", strip=True) if doc_el else ""
        if not raw_name:
            parent_id = (btn.parent.get("id") if btn.parent else "") or ""
            raw_name = parent_id.split("_", 1)[-1] if "_" in parent_id else ""
        name = _clean_name(raw_name)
        if not name:
            continue
        blob = btn.get_text(" ", strip=True)
        parent = btn.parent
        modal = None
        if parent is not None:
            sib = parent.find_next_sibling("div")
            if sib is not None and "modal" in (sib.get("class") or []):
                modal = sib
        fields = _modal_fields(modal) if modal else {}
        modal_text = modal.get_text("\n", strip=True) if modal else ""
        iso = _iso_from_roc(fields.get("看診時間", "") or modal_text)
        if not iso:
            continue
        session = _session_of(fields.get("看診時間", "")) or _session_of(modal_text)
        if not session:
            # session header sits in an ancestor row
            node = btn
            for _ in range(6):
                node = getattr(node, "parent", None)
                if node is None:
                    break
                session = _session_of(node.get_text(" ", strip=True)[:40])
                if session:
                    break
        if not session:
            session = "上午"
        booking = fields.get("預約碼") or ""
        if not booking:
            m = _BOOK_RE.search(modal_text)
            booking = m.group(1) if m else ""
        room_m = _ROOM_RE.search(fields.get("診別", "") + blob)
        room = room_m.group(1) if room_m else ""
        clinic_name = fields.get("診別") or ""
        kebei = fields.get("科別") or dept["label"]
        parts = [p.strip() for p in re.split(r"\s*[-－]\s*", kebei) if p.strip()]
        sub = parts[-1] if parts else dept["label"]
        status = _status_of(btn, blob)
        flags = [
            tok
            for tok in (
                "已逾當日掛號時間",
                "18:00開放",
                "僅存本院初診",
                "本科初診",
                "本科複診",
                "到診不續掛",
                "代診不續掛",
                "代診可續掛",
                "停診",
                "額滿",
            )
            if tok in blob or tok in modal_text
        ]
        rows.append(
            {
                "name_zh": name,
                "clinic_date": iso,
                "weekday": _weekday(iso),
                "session": session,
                "status": status,
                "booking_code": booking,
                "room": room,
                "clinic_name": clinic_name,
                "subdept": sub,
                "kebei": kebei,
                "place": fields.get("看診地點") or "",
                "remark": fields.get("門診備註") or "",
                "flags": flags,
                "dept_label": dept["label"],
                "dept_code": dept["dept_code"],
                "vhosp": dept["vhosp"],
                "schedule_url": dept["url"],
            }
        )
    return rows


def _tags_for(departments: list[str], existing_json: str | None) -> str:
    tags: list[str] = []
    try:
        old = json.loads(existing_json) if existing_json else []
        if isinstance(old, list):
            tags.extend(str(x) for x in old)
    except json.JSONDecodeError:
        pass
    for dept in departments:
        if dept and dept not in tags:
            tags.append(dept)
        for alias_key, extras in DEPT_ALIASES.items():
            if alias_key in dept or dept in extras:
                for ex in extras:
                    if ex not in tags:
                        tags.append(ex)
                if alias_key not in tags:
                    tags.append(alias_key)
        if "乳房" in dept and "breast_surgery" not in tags:
            tags.append("breast_surgery")
    if "webreg" not in tags:
        tags.append("webreg")
    # stable unique
    seen = set()
    out = []
    for t in tags:
        if t and t not in seen:
            seen.add(t)
            out.append(t)
    return json.dumps(out, ensure_ascii=False)


def _slot_id(hospital_id: str, row: dict) -> str:
    disc = row["booking_code"] or row["room"] or re.sub(r"\s+", "", row["clinic_name"])[:24]
    raw = (
        f"{hospital_id}:{row['clinic_date']}:{row['session']}:{row['vhosp']}:"
        f"{row['dept_code']}:{row['name_zh']}:{disc}"
    )
    return raw.replace(" ", "")


def apply(db_path: Path, campuses: list[str], interval: float) -> dict:
    import sqlite3

    fetcher = Fetcher(interval)
    ensure_schema(sqlite3.connect(str(db_path)))  # no-op if present; closes
    # ensure_schema commits via executescript on its own connection — reopen
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)

    summary: dict[str, dict] = {}
    all_slots_by_hospital: dict[str, list[dict]] = defaultdict(list)
    dept_meta_by_hospital: dict[str, list[dict]] = defaultdict(list)
    failures: list[str] = []

    for campus in campuses:
        index_url = f"{REG_BASE}RegShowBlock?vHospCode={campus}"
        try:
            index_html = fetcher.get(index_url)
        except Exception as e:  # noqa: BLE001
            failures.append(f"{campus} index: {e}")
            summary[campus] = {"error": str(e), "depts": 0}
            continue
        depts = discover_departments(index_html, campus)
        # Union RegDeptInfo pages so a dept that is only listed there is kept.
        blocks = sorted(set(re.findall(r"showBlock=([A-Za-z0-9]+)", index_html)))
        for block in blocks:
            info_url = f"{REG_BASE}RegDeptInfo?vHospCode={campus}&showBlock={block}"
            try:
                info_html = fetcher.get(info_url)
            except Exception as e:  # noqa: BLE001
                failures.append(f"{campus} block {block}: {e}")
                continue
            for d in discover_departments(info_html, campus):
                if not any(
                    (x["vhosp"], x["dept_code"], x["show_block"], x["sub"])
                    == (d["vhosp"], d["dept_code"], d["show_block"], d["sub"])
                    for x in depts
                ):
                    depts.append(d)
        depts = _prefer_named_departments(depts)
        campus_rows = []
        empty = []
        errors = []
        for i, dept in enumerate(depts, 1):
            try:
                html = fetcher.get(dept["url"])
            except Exception as e:  # noqa: BLE001
                errors.append(f"{dept['vhosp']}:{dept['dept_code']}: {e}")
                failures.append(f"{dept['url']}: {e}")
                continue
            parsed = parse_schedule(html, dept)
            print(
                f"  {campus} {i}/{len(depts)} {dept['label']} ({dept['dept_code']}) "
                f"slots={len(parsed)}",
                flush=True,
            )
            if not parsed:
                empty.append(f"{dept['vhosp']}:{dept['label']}")
            campus_rows.extend(parsed)
            hid = CAMPUS_HOSPITAL[dept["vhosp"]]
            dept_meta_by_hospital[hid].append(dept)
        summary[campus] = {
            "depts": len(depts),
            "slots_parsed": len(campus_rows),
            "empty_depts": empty,
            "errors": errors,
            "tls_insecure": fetcher.tls_insecure,
        }
        for row in campus_rows:
            all_slots_by_hospital[CAMPUS_HOSPITAL[row["vhosp"]]].append(row)

    now = _now()
    # Replace slots only for hospitals we actually scraped.
    scraped_hospitals = [h for h, rows in all_slots_by_hospital.items() if rows]
    # Also replace when the campus was fetched but every dept was empty — still
    # a successful read. Track fetched hospitals separately.
    fetched_hospitals = set()
    for campus, info in summary.items():
        if info.get("depts"):
            fetched_hospitals.add(CAMPUS_HOSPITAL[campus])

    conn.execute("BEGIN")
    replaced = {}
    for hid in sorted(fetched_hospitals):
        cur = conn.execute(
            "DELETE FROM clinic_slots WHERE hospital_id = ?", (hid,)
        )
        replaced[hid] = cur.rowcount

    doctors_upserted = 0
    slots_inserted = 0
    links_inserted = 0

    # Group slots per doctor
    by_doc: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for hid, rows in all_slots_by_hospital.items():
        for row in rows:
            by_doc[(hid, row["name_zh"])].append(row)

    for (hid, name), rows in by_doc.items():
        dept_counts: dict[str, int] = defaultdict(int)
        for r in rows:
            dept_counts[r["dept_label"]] += 1
        primary = sorted(
            dept_counts.items(),
            key=lambda kv: (
                0 if re.search(r"[\u4e00-\u9fff]", kv[0]) else 1,
                -kv[1],
                kv[0],
            ),
        )[0][0]
        depts = sorted(dept_counts)
        doctor_id = f"{hid}:{name}"
        existing = conn.execute(
            "SELECT * FROM doctors WHERE doctor_id = ?", (doctor_id,)
        ).fetchone()
        tags = _tags_for(
            depts, existing["specialty_tags_json"] if existing else None
        )
        # Prefer a schedule URL on the primary department.
        primary_rows = [r for r in rows if r["dept_label"] == primary]
        sched = (primary_rows or rows)[0]["schedule_url"]
        vhosp_set = sorted({r["vhosp"] for r in rows})
        notes = (
            f"WebReg {','.join(vhosp_set)} depts={ '|'.join(depts) } "
            f"imported {now[:10]}"
        )
        if existing and existing["notes"] and "WebReg" not in (existing["notes"] or ""):
            notes = (existing["notes"] or "").strip() + " | " + notes
        elif existing and existing["notes"] and "乳房" in (existing["notes"] or ""):
            # keep prior non-webreg note prefix once
            pass
        breast_flag = existing["is_breast_specialist"] if existing else None
        dept_zh = primary
        if existing and existing["is_breast_specialist"] == 1 and existing["department_zh"]:
            if "乳房" in (existing["department_zh"] or ""):
                dept_zh = existing["department_zh"]
        progress = (
            f"{REG_BASE}ClinicCurrentLightNo?vHospCode={vhosp_set[0]}"
        )
        if existing:
            conn.execute(
                """
                UPDATE doctors SET
                  department_zh = ?,
                  specialty_tags_json = ?,
                  source_url = ?,
                  source_type = COALESCE(source_type, 'schedule'),
                  registration_url = ?,
                  progress_url = ?,
                  notes = ?,
                  updated_at = ?
                WHERE doctor_id = ?
                """,
                (
                    dept_zh,
                    tags,
                    sched,
                    sched,
                    progress,
                    notes,
                    now,
                    doctor_id,
                ),
            )
        else:
            conn.execute(
                """
                INSERT INTO doctors (
                  doctor_id, hospital_id, name_zh, department_zh,
                  specialty_tags_json, is_breast_specialist, source_url,
                  source_type, registration_url, progress_url, notes, updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    doctor_id,
                    hid,
                    name,
                    dept_zh,
                    tags,
                    breast_flag,
                    sched,
                    "schedule",
                    sched,
                    progress,
                    notes,
                    now,
                ),
            )
        doctors_upserted += 1

        for r in rows:
            sid = _slot_id(hid, r)
            note_bits = [
                f"vhosp={r['vhosp']}",
                f"dept={r['dept_code']}",
                f"label={r['dept_label']}",
                f"sub={r['subdept']}",
                f"clinic={r['clinic_name']}",
                f"room={r['room']}",
                f"booking={r['booking_code']}",
                f"place={r['place']}",
            ]
            if r["flags"]:
                note_bits.append("flags=" + ",".join(r["flags"]))
            if r["remark"]:
                note_bits.append("remark=" + r["remark"][:80])
            tag = "breast_surgery" if "乳房" in (r["dept_label"] + r["subdept"]) else r["dept_code"]
            conn.execute(
                """
                INSERT OR REPLACE INTO clinic_slots (
                  slot_id, hospital_id, doctor_id, name_zh, department_zh,
                  clinic_date, weekday, session, local_clinic_code, status,
                  specialty_tag, registration_url, progress_url, source_url,
                  scraped_at, notes
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    sid,
                    hid,
                    f"{hid}:{name}",
                    name,
                    r["dept_label"],
                    r["clinic_date"],
                    r["weekday"],
                    r["session"],
                    r["booking_code"] or None,
                    r["status"],
                    tag,
                    r["schedule_url"],
                    f"{REG_BASE}ClinicCurrentLightNo?vHospCode={r['vhosp']}",
                    r["schedule_url"],
                    now,
                    " ".join(note_bits),
                ),
            )
            slots_inserted += 1

    for hid, depts in dept_meta_by_hospital.items():
        seen = set()
        for d in depts:
            key = (d["vhosp"], d["dept_code"], d["sub"], d["label"])
            if key in seen:
                continue
            seen.add(key)
            link_id = f"webreg:{d['vhosp']}:{d['dept_code']}:{d['sub'] or '-'}"
            conn.execute(
                """
                INSERT OR REPLACE INTO registration_links (
                  link_id, hospital_id, doctor_id, kind, url, label_zh,
                  timetable_how, source_url, notes
                ) VALUES (?,?,?,?,?,?,?,?,?)
                """,
                (
                    link_id,
                    hid,
                    None,
                    "dept",
                    d["url"],
                    d["label"],
                    "WebReg RegDeptSchedule；掛號鈕為 RegForm?newx=（時效短，不入庫）",
                    d["url"],
                    f"vhosp={d['vhosp']} showBlock={d['show_block']} vDeptCode={d['dept_code']}",
                ),
            )
            links_inserted += 1

    # Annotate hospital catalog rows.
    for campus in campuses:
        hid = CAMPUS_HOSPITAL[campus]
        conn.execute(
            """
            UPDATE hospitals
               SET system_family = 'ntuh',
                   notes = TRIM(COALESCE(notes,'') || ?)
             WHERE hospital_id = ?
               AND IFNULL(notes,'') NOT LIKE ?
            """,
            (
                f" 全科名冊來源 WebReg vHospCode={campus}（{CAMPUS_LABEL[campus]}），匯入 {now[:10]}。",
                hid,
                f"%vHospCode={campus}%匯入 {now[:10]}%",
            ),
        )

    # Legacy OpenOnco ids ntuh_hsinchu:T4:姓名 / :T7:姓名 duplicate the
    # canonical ntuh_hsinchu:姓名 row. Keep the canonical row and repoint links.
    for legacy, canon, person, hosp, primary, notes, updated in conn.execute(
        """
        SELECT l.doctor_id, ('ntuh_hsinchu:' || d.name_zh), l.person_id,
               l.hospital_id, l.is_primary, l.notes, l.updated_at
          FROM doctor_person_links l
          JOIN doctors d ON d.doctor_id = l.doctor_id
         WHERE l.doctor_id LIKE 'ntuh_hsinchu:T4:%'
            OR l.doctor_id LIKE 'ntuh_hsinchu:T7:%'
        """
    ).fetchall():
        if conn.execute(
            "SELECT 1 FROM doctors WHERE doctor_id=?", (canon,)
        ).fetchone() and not conn.execute(
            "SELECT 1 FROM doctor_person_links WHERE doctor_id=?", (canon,)
        ).fetchone():
            conn.execute(
                """
                INSERT INTO doctor_person_links (
                  doctor_id, person_id, hospital_id, is_primary, notes, updated_at
                ) VALUES (?,?,?,?,?,?)
                """,
                (canon, person, hosp, primary, (notes or "") + " | repointed", updated),
            )
        conn.execute("DELETE FROM doctor_person_links WHERE doctor_id=?", (legacy,))
    conn.execute(
        """
        DELETE FROM doctors
         WHERE hospital_id='ntuh_hsinchu'
           AND (doctor_id LIKE 'ntuh_hsinchu:T4:%' OR doctor_id LIKE 'ntuh_hsinchu:T7:%')
           AND EXISTS (
             SELECT 1 FROM doctors d2
              WHERE d2.hospital_id='ntuh_hsinchu'
                AND d2.name_zh = doctors.name_zh
                AND d2.doctor_id = 'ntuh_hsinchu:' || doctors.name_zh
           )
        """
    )

    conn.execute(
        """
        INSERT INTO scrape_runs (
          run_id, started_at, finished_at, kind, hospital_id, status,
          rows_upserted, message, meta_json
        ) VALUES (?,?,?,?,?,?,?,?,?)
        """,
        (
            f"webreg-catalog-{now.replace(':','')}",
            now,
            _now(),
            "schedule",
            ",".join(campuses),
            "partial" if failures else "ok",
            slots_inserted,
            f"doctors={doctors_upserted} slots={slots_inserted} links={links_inserted} failures={len(failures)}",
            json.dumps(
                {"summary": summary, "failures": failures[:40], "replaced_slots": replaced},
                ensure_ascii=False,
            ),
        ),
    )
    conn.commit()
    conn.close()
    return {
        "doctors_upserted": doctors_upserted,
        "slots_inserted": slots_inserted,
        "links_inserted": links_inserted,
        "replaced_old_slots": replaced,
        "summary": summary,
        "failures": failures,
        "tls_insecure": fetcher.tls_insecure,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=str(ROOT / "data" / "ntuh.db"))
    ap.add_argument(
        "--campus",
        action="append",
        choices=sorted(CAMPUS_HOSPITAL),
        help="repeatable; default all campuses",
    )
    ap.add_argument("--interval", type=float, default=0.7)
    args = ap.parse_args()
    campuses = args.campus or ["T0", "CH", "C0", "T2", "T3", "T4", "T7", "Y0"]
    print("campuses", campuses, "db", args.db, flush=True)
    result = apply(Path(args.db), campuses, args.interval)
    print(json.dumps({k: v for k, v in result.items() if k != "summary"}, ensure_ascii=False, indent=2))
    for campus, info in result["summary"].items():
        print(
            campus,
            "depts",
            info.get("depts"),
            "slots",
            info.get("slots_parsed"),
            "empty",
            len(info.get("empty_depts") or []),
            "errors",
            len(info.get("errors") or []),
        )


if __name__ == "__main__":
    main()
