#!/usr/bin/env python3
"""Export NTUH-system rows from OpenOnco breast_care.db into data/ntuh.db.

Copies every hospital whose id starts with ``ntuh`` or whose name is
國立臺灣／台灣大學醫學院附設醫院 (總院、癌醫、新竹、雲林 legacy nhia/h_ ids),
plus doctors, clinic_slots, leave, live progress, reviews, profiles, and
person links for those hospitals. Does **not** filter by breast specialty.

Also inserts catalog rows for campuses that WebReg has but OpenOnco did not
store as their own id: 兒童醫院 (ntuh_children / CH)、北護 (ntuh_beihu / T2)、
金山 (ntuh_jinshan / T3)、and a canonical 雲林 id (ntuh_yunlin / Y0). 竹東 is
not a separate hospital_id; it is part of 新竹臺大分院生醫 (T7) under ntuh_hsinchu.

Usage:
  PYTHONPATH=. python scripts/export_ntuh_db.py
  SOURCE_DB=/path/to/breast_care.db PYTHONPATH=. python scripts/export_ntuh_db.py
"""

from __future__ import annotations

import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.lib.schema_sql import ensure_schema

TAIPEI = timezone(timedelta(hours=8))
DEFAULT_SRC = Path("/workspace/breast-cancer-doctors/data/breast_care.db")
DST = ROOT / "data" / "ntuh.db"

HOSPITAL_PREDICATE = """
hospital_id LIKE 'ntuh%'
OR name_zh LIKE '%臺灣大學醫學院附設醫院%'
OR name_zh LIKE '%台灣大學醫學院附設醫院%'
OR IFNULL(system_family,'') = 'ntuh'
"""

CHILD_TABLES = (
    "doctors",
    "clinic_slots",
    "leave_notices",
    "live_progress",
    "live_progress_history",
    "doctor_reviews",
    "registration_links",
    "scrape_runs",
    "hospital_aliases",
)


def _now() -> str:
    return datetime.now(TAIPEI).isoformat(timespec="seconds")


def _cols(conn: sqlite3.Connection, table: str) -> list[str]:
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]


def _copy_table(
    src: sqlite3.Connection,
    dst: sqlite3.Connection,
    table: str,
    where_sql: str,
    params: list,
) -> int:
    scols = _cols(src, table)
    dcols = _cols(dst, table)
    cols = [c for c in scols if c in dcols]
    if not cols:
        return 0
    col_sql = ", ".join(cols)
    q = f"SELECT {col_sql} FROM {table} WHERE {where_sql}"
    rows = src.execute(q, params).fetchall()
    if not rows:
        return 0
    ph = ", ".join("?" * len(cols))
    dst.executemany(
        f"INSERT OR REPLACE INTO {table} ({col_sql}) VALUES ({ph})",
        rows,
    )
    return len(rows)


def _upsert_catalog(dst: sqlite3.Connection) -> None:
    now = _now()
    rows = [
        {
            "hospital_id": "ntuh_children",
            "name_zh": "國立臺灣大學醫學院附設醫院兒童醫院",
            "name_en": "National Taiwan University Children's Hospital",
            "campus": "兒童醫院",
            "city": "臺北市",
            "address": "臺北市中正區中山南路8號",
            "system_family": "ntuh",
            "registration_hub_url": "https://reg.ntuh.gov.tw/WebReg/WebReg/BranchIndex?vHospCode=CH",
            "progress_hub_url": "https://reg.ntuh.gov.tw/WebReg/WebReg/ClinicCurrentLightNo?vHospCode=CH",
            "adapter_id": "ntuh_children",
            "timezone": "Asia/Taipei",
            "notes": (
                "WebReg vHospCode=CH。OpenOnco breast_care.db 沒有兒童醫院名冊或診次；"
                "此列只是院區目錄。全科掛號可帶 registration_url。"
            ),
            "updated_at": now,
        },
        {
            "hospital_id": "ntuh_beihu",
            "name_zh": "國立臺灣大學醫學院附設醫院北護分院",
            "name_en": "National Taiwan University Hospital Bei-Hu Branch",
            "campus": "北護分院",
            "city": "臺北市",
            "address": "臺北市萬華區內江街87號（康定路37號）",
            "system_family": "ntuh",
            "nhia_code": "0401190010",
            "registration_hub_url": "https://reg.ntuh.gov.tw/WebReg/WebReg/BranchIndex?vHospCode=T2",
            "progress_hub_url": "https://reg.ntuh.gov.tw/WebReg/WebReg/ClinicCurrentLightNo?vHospCode=T2",
            "adapter_id": "ntuh_beihu",
            "timezone": "Asia/Taipei",
            "notes": (
                "WebReg vHospCode=T2。官方站 https://www.bh.ntuh.gov.tw/。"
                "OpenOnco breast_care.db 沒有北護名冊；全科以 WebReg 匯入。"
            ),
            "updated_at": now,
        },
        {
            "hospital_id": "ntuh_jinshan",
            "name_zh": "國立臺灣大學醫學院附設醫院金山分院",
            "name_en": "National Taiwan University Hospital Jinshan Branch",
            "campus": "金山分院",
            "city": "新北市",
            "address": "新北市金山區玉爐路7號",
            "system_family": "ntuh",
            "nhia_code": "0431270012",
            "registration_hub_url": "https://reg.ntuh.gov.tw/WebReg/WebReg/BranchIndex?vHospCode=T3",
            "progress_hub_url": "https://reg.ntuh.gov.tw/WebReg/WebReg/ClinicCurrentLightNo?vHospCode=T3",
            "adapter_id": "ntuh_jinshan",
            "timezone": "Asia/Taipei",
            "notes": (
                "WebReg vHospCode=T3。官方站 https://www.js.ntuh.gov.tw/。"
                "OpenOnco breast_care.db 沒有金山名冊；全科以 WebReg 匯入。"
            ),
            "updated_at": now,
        },
        {
            "hospital_id": "ntuh_yunlin",
            "name_zh": "國立臺灣大學醫學院附設醫院雲林分院",
            "name_en": "National Taiwan University Hospital Yunlin Branch",
            "campus": "雲林（斗六／虎尾）",
            "city": "雲林縣",
            "address": "斗六院區：雲林縣斗六市雲林路二段579號；虎尾院區：雲林縣虎尾鎮學府路95號",
            "system_family": "ntuh",
            "registration_hub_url": "https://reg.ntuh.gov.tw/WebReg/WebReg/BranchIndex?vHospCode=Y0",
            "progress_hub_url": "https://reg.ntuh.gov.tw/WebReg/WebReg/ClinicCurrentLightNo?vHospCode=Y0",
            "adapter_id": "ntuh_yunlin",
            "timezone": "Asia/Taipei",
            "notes": (
                "WebReg vHospCode=Y0（斗六／虎尾同一入口）。"
                "OpenOnco 乳癌名冊片段在舊 id nhia_0439010518 與 h_2f3e46a806。"
                "全科診次尚未匯入。"
            ),
            "updated_at": now,
        },
    ]
    for row in rows:
        exists = dst.execute(
            "SELECT 1 FROM hospitals WHERE hospital_id = ?",
            (row["hospital_id"],),
        ).fetchone()
        if exists:
            continue
        cols = ", ".join(row)
        ph = ", ".join("?" * len(row))
        dst.execute(
            f"INSERT INTO hospitals ({cols}) VALUES ({ph})",
            list(row.values()),
        )
    # Annotate copied campuses. 竹東 shares T7 with 生醫, not its own id.
    dst.execute(
        """
        UPDATE hospitals
           SET system_family = 'ntuh',
               campus = '新竹醫院（T4）／生醫（竹北、竹東，T7）',
               notes = TRIM(COALESCE(notes,'') || ' 院區碼 T4=新竹醫院、T7=生醫（竹北，含原竹東院區）。竹東沒有獨立 hospital_id。')
         WHERE hospital_id = 'ntuh_hsinchu'
        """
    )
    dst.execute(
        """
        UPDATE hospitals
           SET system_family = 'ntuh',
               notes = TRIM(COALESCE(notes,'') || ' WebReg vHospCode=T0 總院。')
         WHERE hospital_id = 'ntuh'
           AND IFNULL(notes,'') NOT LIKE '%vHospCode=T0%'
        """
    )
    dst.execute(
        """
        UPDATE hospitals
           SET system_family = 'ntuh',
               notes = TRIM(COALESCE(notes,'') || ' WebReg vHospCode=C0 癌醫。別名 nhia_0401020013。')
         WHERE hospital_id = 'ntuh_cancer'
           AND IFNULL(notes,'') NOT LIKE '%vHospCode=C0%'
        """
    )
    dst.execute(
        """
        UPDATE hospitals
           SET system_family = 'ntuh'
         WHERE hospital_id IN (
            'nhia_0401020013','nhia_0412040012','nhia_0439010518','h_2f3e46a806'
         )
        """
    )


def export(src_path: Path, dst_path: Path) -> dict[str, int]:
    if not src_path.is_file():
        raise SystemExit(f"source db not found: {src_path}")
    dst_path.parent.mkdir(parents=True, exist_ok=True)
    if dst_path.exists():
        dst_path.unlink()
    src = sqlite3.connect(str(src_path))
    src.row_factory = sqlite3.Row
    dst = sqlite3.connect(str(dst_path))
    ensure_schema(dst)
    dst.execute('PRAGMA foreign_keys = OFF')
    ids = [
        r[0]
        for r in src.execute(f"SELECT hospital_id FROM hospitals WHERE {HOSPITAL_PREDICATE}")
    ]
    if not ids:
        raise SystemExit("no NTUH hospitals matched in source")
    ph = ", ".join("?" * len(ids))
    counts: dict[str, int] = {}
    counts["hospitals"] = _copy_table(
        src, dst, "hospitals", f"hospital_id IN ({ph})", ids
    )
    for table in CHILD_TABLES:
        if table not in {r[0] for r in src.execute("SELECT name FROM sqlite_master WHERE type='table'")}:
            counts[table] = 0
            continue
        if "hospital_id" not in _cols(src, table):
            counts[table] = 0
            continue
        counts[table] = _copy_table(
            src, dst, table, f"hospital_id IN ({ph})", ids
        )
    # profiles follow doctors
    doc_ids = [r[0] for r in dst.execute("SELECT doctor_id FROM doctors")]
    if doc_ids and "doctor_profiles" in {
        r[0] for r in src.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }:
        ph2 = ", ".join("?" * len(doc_ids))
        counts["doctor_profiles"] = _copy_table(
            src, dst, "doctor_profiles", f"doctor_id IN ({ph2})", doc_ids
        )
    else:
        counts["doctor_profiles"] = 0
    # Persons before links so the FK on doctor_person_links can succeed.
    person_ids = [
        r[0]
        for r in src.execute(
            f"SELECT DISTINCT person_id FROM doctor_person_links WHERE hospital_id IN ({ph})",
            ids,
        )
    ]
    if person_ids:
        ph3 = ", ".join("?" * len(person_ids))
        counts["doctor_persons"] = _copy_table(
            src, dst, "doctor_persons", f"person_id IN ({ph3})", person_ids
        )
    else:
        counts["doctor_persons"] = 0
    counts["doctor_person_links"] = _copy_table(
        src, dst, "doctor_person_links", f"hospital_id IN ({ph})", ids
    )
    _upsert_catalog(dst)
    dst.commit()
    print("source:", src_path)
    print("dest:  ", dst_path)
    print("matched hospital_ids:")
    for r in dst.execute(
        """
        SELECT h.hospital_id, h.campus, h.city,
               (SELECT COUNT(*) FROM doctors d WHERE d.hospital_id = h.hospital_id) AS doctors,
               (SELECT COUNT(*) FROM clinic_slots s WHERE s.hospital_id = h.hospital_id) AS slots
          FROM hospitals h
         ORDER BY h.hospital_id
        """
    ):
        print(f"  {r[0]}\tcampus={r[1]}\tcity={r[2]}\tdoctors={r[3]}\tslots={r[4]}")
    print("row counts:", counts)
    src.close()
    dst.close()
    return counts


def main() -> None:
    src = Path(os.environ.get("SOURCE_DB") or DEFAULT_SRC)
    export(src, DST)


if __name__ == "__main__":
    main()
