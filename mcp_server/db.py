"""SQLite helpers for data/ntuh.db (NTUH hospitals / all doctors / slots)."""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "data" / "ntuh.db"
TAIPEI = timezone(timedelta(hours=8))

# Canonical booking ids. Legacy OpenOnco rows (nhia_*, h_*) stay in the DB
# when the hospital name is an NTUH campus. See mcp_server.ntuh_scope.
NTUH_FAMILY = (
    "ntuh",
    "ntuh_children",
    "ntuh_cancer",
    "ntuh_hsinchu",
    "ntuh_yunlin",
)

SLOT_COLS = (
    "slot_id",
    "hospital_id",
    "doctor_id",
    "name_zh",
    "department_zh",
    "clinic_date",
    "weekday",
    "session",
    "local_clinic_code",
    "status",
    "specialty_tag",
    "registration_url",
    "progress_url",
    "source_url",
    "scraped_at",
    "notes",
)

DOCTOR_COLS = (
    "d.doctor_id",
    "d.hospital_id",
    "d.name_zh",
    "d.name_en",
    "d.local_doctor_code",
    "d.department_zh",
    "d.specialty_tags_json",
    "d.is_breast_specialist",
    "d.source_url",
    "d.source_type",
    "d.registration_url",
    "d.progress_url",
    "d.notes",
    "d.updated_at",
)


def db_path() -> Path:
    import os

    override = (os.environ.get("NTUH_DB") or os.environ.get("BREAST_CARE_DB") or "").strip()
    return Path(override) if override else DEFAULT_DB


def ntuh_hospital_predicate(column: str = "hospital_id") -> str:
    """SQL fragment: column is an NTUH-system hospital (no user interpolation).

    Matches canonical ``ntuh*`` ids and any hospitals row with
    ``system_family='ntuh'`` or a 臺灣／台灣大學醫學院附設醫院 name.
    Does **not** filter by breast specialty.
    """
    return (
        f"({column} LIKE 'ntuh%' OR {column} IN ("
        "SELECT hospital_id FROM hospitals WHERE "
        "IFNULL(system_family,'') = 'ntuh' "
        "OR name_zh LIKE '%臺灣大學醫學院附設醫院%' "
        "OR name_zh LIKE '%台灣大學醫學院附設醫院%'"
        "))"
    )


def connect(path: Path | None = None) -> sqlite3.Connection:
    p = path or db_path()
    conn = sqlite3.connect(str(p))
    conn.row_factory = sqlite3.Row
    try:
        from scripts.lib.schema_sql import ensure_schema

        ensure_schema(conn)
    except Exception:
        # Best-effort: history table may be created by migrations elsewhere
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS live_progress_history (
              hist_id INTEGER PRIMARY KEY AUTOINCREMENT,
              progress_id TEXT NOT NULL,
              hospital_id TEXT NOT NULL,
              doctor_id TEXT,
              name_zh TEXT,
              department_zh TEXT,
              clinic_date TEXT NOT NULL,
              session TEXT,
              local_clinic_code TEXT,
              current_number TEXT,
              next_number TEXT,
              max_number TEXT,
              waiting_count INTEGER,
              status_text TEXT,
              specialty_tag TEXT,
              source_url TEXT,
              fetched_at TEXT NOT NULL,
              raw_json TEXT
            )
            """
        )
    return conn


def today_taipei() -> date:
    return datetime.now(TAIPEI).date()


def _parse_tags(raw: Any) -> list[str]:
    if not raw:
        return []
    if isinstance(raw, list):
        return [str(x) for x in raw]
    try:
        val = json.loads(raw)
        if isinstance(val, list):
            return [str(x) for x in val]
    except (TypeError, json.JSONDecodeError):
        pass
    return [str(raw)]


def _doctor_row(r: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    d = dict(r)
    d["specialty_tags"] = _parse_tags(d.pop("specialty_tags_json", None))
    # Drop empty hospital alias fields noise if present
    return d


# ---------------------------------------------------------------------------
# Booking / clinic_slots
# ---------------------------------------------------------------------------


def search_clinic_slots(
    *,
    hospital_id: str | None = None,
    doctor_name_zh: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    session: str | None = None,
    status: str | None = "可掛號",
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Query clinic_slots. Always restricted to NTUH-system hospitals.

    No breast-specialty filter. Omit hospital_id to search every NTUH campus
    present in the local DB.
    """
    d0 = date_from or today_taipei().isoformat()
    d1 = date_to or (today_taipei() + timedelta(days=14)).isoformat()
    lim = max(1, min(int(limit or 50), 200))

    where = ["clinic_date >= ?", "clinic_date <= ?"]
    params: list[Any] = [d0, d1]

    if hospital_id:
        where.append("hospital_id = ?")
        params.append(hospital_id)
    where.append(ntuh_hospital_predicate("hospital_id"))

    if doctor_name_zh:
        where.append("name_zh LIKE ?")
        params.append(f"%{doctor_name_zh}%")

    if session:
        where.append("session = ?")
        params.append(session)

    if status:
        # Allow prefix match for variants like 可掛號(18:30開診)
        where.append("status LIKE ?")
        params.append(f"{status}%")

    sql = (
        f"SELECT {', '.join(SLOT_COLS)} FROM clinic_slots "
        f"WHERE {' AND '.join(where)} "
        f"ORDER BY clinic_date, session, name_zh LIMIT ?"
    )
    params.append(lim)

    with connect() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def get_slot_by_id(slot_id: str) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute(
            f"SELECT {', '.join(SLOT_COLS)} FROM clinic_slots WHERE slot_id = ?",
            (slot_id,),
        ).fetchone()
    return dict(row) if row else None


def upsert_live_progress(rows: list[dict[str, Any]]) -> int:
    """Best-effort upsert into live_progress (keyed by progress_id if present)."""
    if not rows:
        return 0
    n = 0
    with connect() as conn:
        for r in rows:
            pid = r.get("progress_id")
            if not pid:
                # synthesize from hospital + name + date + session + clinic
                parts = [
                    r.get("hospital_id") or "",
                    r.get("name_zh") or "",
                    r.get("clinic_date") or "",
                    r.get("session") or "",
                    r.get("local_clinic_code") or "",
                ]
                pid = "lp:" + "|".join(str(p) for p in parts)
            fetched_at = r.get("fetched_at") or datetime.now(TAIPEI).isoformat(
                timespec="seconds"
            )
            vals = (
                pid,
                r.get("hospital_id"),
                r.get("doctor_id"),
                r.get("name_zh"),
                r.get("department_zh"),
                str(r.get("clinic_date") or ""),
                r.get("session"),
                r.get("local_clinic_code"),
                r.get("current_number"),
                r.get("next_number"),
                r.get("max_number"),
                r.get("waiting_count"),
                r.get("status_text"),
                r.get("specialty_tag"),
                r.get("source_url"),
                fetched_at,
                r.get("raw_json"),
            )
            prev = conn.execute(
                """
                SELECT current_number, next_number, max_number, waiting_count, status_text
                FROM live_progress WHERE progress_id = ?
                """,
                (pid,),
            ).fetchone()
            conn.execute(
                """
                INSERT INTO live_progress (
                  progress_id, hospital_id, doctor_id, name_zh, department_zh,
                  clinic_date, session, local_clinic_code, current_number,
                  next_number, max_number, waiting_count, status_text,
                  specialty_tag, source_url, fetched_at, raw_json
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(progress_id) DO UPDATE SET
                  doctor_id=excluded.doctor_id,
                  name_zh=excluded.name_zh,
                  department_zh=excluded.department_zh,
                  current_number=excluded.current_number,
                  next_number=excluded.next_number,
                  max_number=excluded.max_number,
                  waiting_count=excluded.waiting_count,
                  status_text=excluded.status_text,
                  specialty_tag=excluded.specialty_tag,
                  source_url=excluded.source_url,
                  fetched_at=excluded.fetched_at,
                  raw_json=excluded.raw_json
                """,
                vals,
            )
            # History only when the published number/status actually changes.
            def _norm(v: Any) -> str | None:
                if v is None:
                    return None
                s = str(v).strip()
                return s if s else None

            changed = prev is None or (
                _norm(prev[0]) != _norm(vals[8])
                or _norm(prev[1]) != _norm(vals[9])
                or _norm(prev[2]) != _norm(vals[10])
                or _norm(prev[3]) != _norm(vals[11])
                or _norm(prev[4]) != _norm(vals[12])
            )
            if changed:
                try:
                    conn.execute(
                        """
                        INSERT INTO live_progress_history (
                          progress_id, hospital_id, doctor_id, name_zh, department_zh,
                          clinic_date, session, local_clinic_code, current_number,
                          next_number, max_number, waiting_count, status_text,
                          specialty_tag, source_url, fetched_at, raw_json
                        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                        """,
                        vals,
                    )
                except sqlite3.OperationalError:
                    pass
            n += 1
        conn.commit()
    return n


def upsert_leave_notices(rows: list[dict[str, Any]]) -> int:
    """Best-effort upsert into leave_notices (keyed by notice_id)."""
    if not rows:
        return 0
    n = 0
    with connect() as conn:
        for r in rows:
            nid = r.get("notice_id")
            if not nid:
                parts = [
                    r.get("hospital_id") or "",
                    r.get("leave_date") or "",
                    r.get("session") or "",
                    r.get("doctor_id") or r.get("name_zh") or "",
                ]
                nid = "ln:" + "|".join(str(p) for p in parts)
            conn.execute(
                """
                INSERT INTO leave_notices (
                  notice_id, hospital_id, doctor_id, name_zh, leave_date,
                  session, reason_zh, source_url, scraped_at, notes
                ) VALUES (?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(notice_id) DO UPDATE SET
                  doctor_id=excluded.doctor_id,
                  name_zh=excluded.name_zh,
                  leave_date=excluded.leave_date,
                  session=excluded.session,
                  reason_zh=excluded.reason_zh,
                  source_url=excluded.source_url,
                  scraped_at=excluded.scraped_at,
                  notes=excluded.notes
                """,
                (
                    nid,
                    r.get("hospital_id"),
                    r.get("doctor_id"),
                    r.get("name_zh"),
                    str(r.get("leave_date") or "") or None,
                    r.get("session"),
                    r.get("reason_zh"),
                    r.get("source_url"),
                    r.get("scraped_at")
                    or datetime.now(TAIPEI).isoformat(timespec="seconds"),
                    r.get("notes"),
                ),
            )
            n += 1
        conn.commit()
    return n


def log_scrape_run(
    *,
    kind: str,
    hospital_id: str,
    status: str,
    message: str,
    meta: dict[str, Any] | None = None,
    rows_upserted: int = 0,
) -> None:
    from uuid import uuid4

    now = datetime.now(TAIPEI).isoformat(timespec="seconds")
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO scrape_runs (
              run_id, started_at, finished_at, kind, hospital_id,
              status, rows_upserted, message, meta_json
            ) VALUES (?,?,?,?,?,?,?,?,?)
            """,
            (
                str(uuid4()),
                now,
                now,
                kind,
                hospital_id,
                status,
                rows_upserted,
                message[:500],
                json.dumps(meta or {}, ensure_ascii=False),
            ),
        )
        conn.commit()


# ---------------------------------------------------------------------------
# Doctors discovery (read-only)
# ---------------------------------------------------------------------------


def list_cities() -> list[dict[str, Any]]:
    """Distinct cities from hospitals (non-empty), with hospital counts."""
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT city, COUNT(*) AS hospital_count
            FROM hospitals
            WHERE city IS NOT NULL AND TRIM(city) != ''
              AND (system_family = 'ntuh' OR hospital_id LIKE 'ntuh%' OR name_zh LIKE '%臺灣大學醫學院附設醫院%' OR name_zh LIKE '%台灣大學醫學院附設醫院%')
            GROUP BY city
            ORDER BY hospital_count DESC, city
            """
        ).fetchall()
    return [dict(r) for r in rows]


def list_hospitals(
    *,
    city: str | None = None,
    q: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    lim = max(1, min(int(limit or 100), 500))
    where: list[str] = []
    params: list[Any] = []
    if city:
        # Match city / campus / address / name (e.g. 竹北 → campus)
        where.append(
            "(h.city LIKE ? OR IFNULL(h.campus,'') LIKE ? "
            "OR IFNULL(h.address,'') LIKE ? OR h.name_zh LIKE ?)"
        )
        like = f"%{city}%"
        params.extend([like, like, like, like])
    if q:
        where.append(
            "(h.hospital_id LIKE ? OR h.name_zh LIKE ? OR IFNULL(h.campus,'') LIKE ?)"
        )
        like = f"%{q}%"
        params.extend([like, like, like])
    sql = """
        SELECT h.hospital_id, h.name_zh, h.name_en, h.campus, h.city, h.address,
               h.system_family, h.registration_hub_url, h.progress_hub_url,
               h.roster_url, h.adapter_id
        FROM hospitals h
    """
    where.append(ntuh_hospital_predicate("h.hospital_id"))
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY h.city, h.name_zh LIMIT ?"
    params.append(lim)
    with connect() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def search_doctors(
    *,
    name: str | None = None,
    city: str | None = None,
    hospital: str | None = None,
    specialty: str | None = None,
    is_specialist: bool | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Search doctors on NTUH campuses only (all departments; specialty optional)."""
    lim = max(1, min(int(limit or 50), 200))
    where: list[str] = []
    params: list[Any] = []

    if name:
        where.append("(d.name_zh LIKE ? OR IFNULL(d.name_en,'') LIKE ?)")
        like = f"%{name}%"
        params.extend([like, like])

    if city:
        where.append(
            "(IFNULL(h.city,'') LIKE ? OR IFNULL(h.campus,'') LIKE ? "
            "OR IFNULL(h.address,'') LIKE ? OR IFNULL(h.name_zh,'') LIKE ?)"
        )
        like = f"%{city}%"
        params.extend([like, like, like, like])

    if hospital:
        where.append(
            "(d.hospital_id LIKE ? OR IFNULL(h.name_zh,'') LIKE ? "
            "OR IFNULL(h.campus,'') LIKE ?)"
        )
        like = f"%{hospital}%"
        params.extend([like, like, like])

    if specialty:
        # tags, department name, or notes (so 放射/麻醉 match 影像醫學部/麻醉部)
        where.append(
            "(IFNULL(d.specialty_tags_json,'') LIKE ? "
            "OR IFNULL(d.department_zh,'') LIKE ? "
            "OR IFNULL(d.notes,'') LIKE ?)"
        )
        like = f"%{specialty}%"
        params.extend([like, like, like])

    if is_specialist is True:
        where.append("d.is_breast_specialist = 1")
    elif is_specialist is False:
        where.append("(d.is_breast_specialist = 0 OR d.is_breast_specialist IS NULL)")

    where.append(ntuh_hospital_predicate("d.hospital_id"))

    sql = f"""
        SELECT {', '.join(DOCTOR_COLS)},
               l.person_id,
               l.is_primary AS link_is_primary,
               h.name_zh AS hospital_name_zh,
               h.campus AS hospital_campus,
               h.city AS hospital_city,
               h.address AS hospital_address,
               h.system_family AS hospital_system_family
        FROM doctors d
        LEFT JOIN hospitals h ON h.hospital_id = d.hospital_id
        LEFT JOIN doctor_person_links l ON l.doctor_id = d.doctor_id
    """
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY d.name_zh, d.hospital_id LIMIT ?"
    params.append(lim)

    with connect() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [_doctor_row(r) for r in rows]


def _fetch_doctor_sites(conn: sqlite3.Connection, doctor_ids: list[str]) -> list[dict[str, Any]]:
    if not doctor_ids:
        return []
    placeholders = ",".join("?" * len(doctor_ids))
    rows = conn.execute(
        f"""
        SELECT {', '.join(DOCTOR_COLS)},
               l.person_id,
               l.is_primary AS link_is_primary,
               h.name_zh AS hospital_name_zh,
               h.campus AS hospital_campus,
               h.city AS hospital_city,
               h.address AS hospital_address,
               h.system_family AS hospital_system_family
        FROM doctors d
        LEFT JOIN hospitals h ON h.hospital_id = d.hospital_id
        LEFT JOIN doctor_person_links l ON l.doctor_id = d.doctor_id
        WHERE d.doctor_id IN ({placeholders})
          AND ntuh_hospital_predicate('d.hospital_id')
        ORDER BY COALESCE(l.is_primary, 0) DESC, d.hospital_id
        """,
        doctor_ids,
    ).fetchall()
    return [_doctor_row(r) for r in rows]


def resolve_person_or_doctor(
    *,
    person_id: str | None = None,
    doctor_id: str | None = None,
) -> dict[str, Any] | None:
    """Resolve to person + all linked site doctor rows (or single doctor)."""
    if not person_id and not doctor_id:
        raise ValueError("provide person_id or doctor_id")

    with connect() as conn:
        pid = person_id
        name_zh = None
        name_en = None
        notes = None

        if doctor_id and not pid:
            link = conn.execute(
                "SELECT person_id FROM doctor_person_links WHERE doctor_id = ?",
                (doctor_id,),
            ).fetchone()
            if link:
                pid = link["person_id"]

        doctor_ids: list[str] = []
        if pid:
            person = conn.execute(
                "SELECT * FROM doctor_persons WHERE person_id = ?",
                (pid,),
            ).fetchone()
            if not person:
                return None
            name_zh = person["name_zh"]
            name_en = person["name_en"]
            notes = person["notes"]
            links = conn.execute(
                "SELECT doctor_id FROM doctor_person_links WHERE person_id = ?",
                (pid,),
            ).fetchall()
            doctor_ids = [r["doctor_id"] for r in links]
        else:
            # single doctor, no person link
            row = conn.execute(
                "SELECT doctor_id, name_zh, name_en FROM doctors WHERE doctor_id = ?",
                (doctor_id,),
            ).fetchone()
            if not row:
                return None
            doctor_ids = [row["doctor_id"]]
            name_zh = row["name_zh"]
            name_en = row["name_en"]

        sites = _fetch_doctor_sites(conn, doctor_ids)
        if not sites and doctor_id:
            # doctor_id given but vanished
            return None

        # profiles (bio) keyed by doctor_id
        profiles: list[dict[str, Any]] = []
        if doctor_ids:
            ph = ",".join("?" * len(doctor_ids))
            for pr in conn.execute(
                f"""
                SELECT doctor_id, bio_zh, specialties_raw, education_zh,
                       experience_zh, photo_url, profile_url, scraped_at
                FROM doctor_profiles WHERE doctor_id IN ({ph})
                """,
                doctor_ids,
            ).fetchall():
                profiles.append(dict(pr))

    return {
        "person_id": pid,
        "name_zh": name_zh,
        "name_en": name_en,
        "notes": notes,
        "sites": sites,
        "profiles": profiles,
        "doctor_ids": doctor_ids,
    }


def upcoming_slots_summary(
    *,
    doctor_ids: list[str] | None = None,
    name_zh: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    limit: int = 40,
) -> list[dict[str, Any]]:
    """Upcoming clinic_slots for given doctor_ids and/or name."""
    d0 = date_from or today_taipei().isoformat()
    d1 = date_to or (today_taipei() + timedelta(days=21)).isoformat()
    lim = max(1, min(int(limit or 40), 200))
    where = ["clinic_date >= ?", "clinic_date <= ?"]
    params: list[Any] = [d0, d1]

    id_clause = ""
    if doctor_ids:
        ph = ",".join("?" * len(doctor_ids))
        id_clause = f"doctor_id IN ({ph})"
        params.extend(doctor_ids)
    name_clause = ""
    if name_zh:
        name_clause = "name_zh LIKE ?"
        params.append(f"%{name_zh}%")

    if id_clause and name_clause:
        where.append(f"({id_clause} OR {name_clause})")
    elif id_clause:
        where.append(id_clause)
    elif name_clause:
        where.append(name_clause)
    else:
        return []

    where.append(ntuh_hospital_predicate("hospital_id"))

    sql = (
        f"SELECT {', '.join(SLOT_COLS)} FROM clinic_slots "
        f"WHERE {' AND '.join(where)} "
        f"ORDER BY clinic_date, session LIMIT ?"
    )
    params.append(lim)
    with connect() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def list_leave_or_stops(
    *,
    doctor_id: str | None = None,
    person_id: str | None = None,
    name_zh: str | None = None,
    hospital_id: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """Leave notices; if none match, fall back to clinic_slots status=停診.

    Returns dict with source, fallback_note, and items.
    """
    d0 = date_from or today_taipei().isoformat()
    d1 = date_to or (today_taipei() + timedelta(days=30)).isoformat()
    lim = max(1, min(int(limit or 50), 200))

    doctor_ids: list[str] = []
    resolved_name = name_zh

    with connect() as conn:
        if person_id:
            links = conn.execute(
                "SELECT doctor_id FROM doctor_person_links WHERE person_id = ?",
                (person_id,),
            ).fetchall()
            doctor_ids = [r["doctor_id"] for r in links]
            if not resolved_name:
                p = conn.execute(
                    "SELECT name_zh FROM doctor_persons WHERE person_id = ?",
                    (person_id,),
                ).fetchone()
                if p:
                    resolved_name = p["name_zh"]
        if doctor_id:
            doctor_ids = list({*doctor_ids, doctor_id})
            if not resolved_name:
                r = conn.execute(
                    "SELECT name_zh FROM doctors WHERE doctor_id = ?",
                    (doctor_id,),
                ).fetchone()
                if r:
                    resolved_name = r["name_zh"]

        # --- leave_notices ---
        where: list[str] = [
            "(leave_date IS NULL OR (leave_date >= ? AND leave_date <= ?))"
        ]
        params: list[Any] = [d0, d1]
        if doctor_ids:
            ph = ",".join("?" * len(doctor_ids))
            if resolved_name:
                where.append(f"(doctor_id IN ({ph}) OR name_zh LIKE ?)")
                params.extend(doctor_ids)
                params.append(f"%{resolved_name}%")
            else:
                where.append(f"doctor_id IN ({ph})")
                params.extend(doctor_ids)
        elif resolved_name:
            where.append("name_zh LIKE ?")
            params.append(f"%{resolved_name}%")
        if hospital_id:
            where.append("hospital_id = ?")
            params.append(hospital_id)
        where.append(ntuh_hospital_predicate("hospital_id"))

        leave_sql = (
            "SELECT notice_id, hospital_id, doctor_id, name_zh, leave_date, "
            "session, reason_zh, source_url, scraped_at, notes "
            f"FROM leave_notices WHERE {' AND '.join(where)} "
            "ORDER BY leave_date, session LIMIT ?"
        )
        params_leave = list(params) + [lim]
        leave_rows = [dict(r) for r in conn.execute(leave_sql, params_leave).fetchall()]

        if leave_rows:
            return {
                "source": "leave_notices",
                "fallback_note": None,
                "date_from": d0,
                "date_to": d1,
                "count": len(leave_rows),
                "items": leave_rows,
            }

        # --- fallback: clinic_slots status=停診 ---
        sw: list[str] = [
            "clinic_date >= ?",
            "clinic_date <= ?",
            "status LIKE ?",
        ]
        sp: list[Any] = [d0, d1, "停診%"]
        if doctor_ids:
            ph = ",".join("?" * len(doctor_ids))
            if resolved_name:
                sw.append(f"(doctor_id IN ({ph}) OR name_zh LIKE ?)")
                sp.extend(doctor_ids)
                sp.append(f"%{resolved_name}%")
            else:
                sw.append(f"doctor_id IN ({ph})")
                sp.extend(doctor_ids)
        elif resolved_name:
            sw.append("name_zh LIKE ?")
            sp.append(f"%{resolved_name}%")
        if hospital_id:
            sw.append("hospital_id = ?")
            sp.append(hospital_id)
        sw.append(ntuh_hospital_predicate("hospital_id"))

        slot_sql = (
            f"SELECT {', '.join(SLOT_COLS)} FROM clinic_slots "
            f"WHERE {' AND '.join(sw)} "
            f"ORDER BY clinic_date, session LIMIT ?"
        )
        sp.append(lim)
        stop_rows = [dict(r) for r in conn.execute(slot_sql, sp).fetchall()]

        items = [
            {
                "notice_id": None,
                "hospital_id": r.get("hospital_id"),
                "doctor_id": r.get("doctor_id"),
                "name_zh": r.get("name_zh"),
                "leave_date": r.get("clinic_date"),
                "session": r.get("session"),
                "reason_zh": None,
                "source_url": r.get("source_url"),
                "scraped_at": r.get("scraped_at"),
                "notes": r.get("notes"),
                "slot_id": r.get("slot_id"),
                "status": r.get("status"),
                "from_clinic_slots": True,
            }
            for r in stop_rows
        ]
        return {
            "source": "clinic_slots_status_停診",
            "fallback_note": (
                "leave_notices 無符合資料；已改以 clinic_slots.status=停診 回傳。"
            ),
            "date_from": d0,
            "date_to": d1,
            "count": len(items),
            "items": items,
        }


def query_live_progress(
    *,
    hospital_id: str | None = None,
    name_zh: str | None = None,
    clinic_date: str | None = None,
    session: str | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    """Latest live_progress snapshot rows (not history). NTUH hospitals only."""
    clauses: list[str] = [ntuh_hospital_predicate("hospital_id")]
    args: list[Any] = []
    if hospital_id:
        clauses.append("hospital_id = ?")
        args.append(hospital_id)
    if name_zh:
        clauses.append("name_zh LIKE ?")
        args.append(f"%{name_zh}%")
    if clinic_date:
        clauses.append("clinic_date = ?")
        args.append(clinic_date)
    if session:
        clauses.append("session = ?")
        args.append(session)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    sql = f"""
        SELECT progress_id, hospital_id, doctor_id, name_zh, department_zh,
               clinic_date, session, local_clinic_code, current_number, next_number,
               max_number, waiting_count, status_text, specialty_tag, source_url,
               fetched_at
        FROM live_progress
        {where}
        ORDER BY fetched_at DESC
        LIMIT ?
    """
    args.append(max(1, min(int(limit), 2000)))
    with connect() as conn:
        rows = conn.execute(sql, args).fetchall()
    return [dict(r) for r in rows]


def query_progress_history(
    *,
    hospital_id: str | None = None,
    name_zh: str | None = None,
    clinic_date: str | None = None,
    limit: int = 500,
) -> list[dict[str, Any]]:
    """Return append-only live_progress_history rows (newest first). NTUH only."""
    clauses: list[str] = [ntuh_hospital_predicate("hospital_id")]
    args: list[Any] = []
    if hospital_id:
        clauses.append("hospital_id = ?")
        args.append(hospital_id)
    if name_zh:
        clauses.append("name_zh LIKE ?")
        args.append(f"%{name_zh}%")
    if clinic_date:
        clauses.append("clinic_date = ?")
        args.append(clinic_date)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    sql = f"""
        SELECT hist_id, progress_id, hospital_id, doctor_id, name_zh, department_zh,
               clinic_date, session, local_clinic_code, current_number, next_number,
               max_number, waiting_count, status_text, specialty_tag, source_url,
               fetched_at
        FROM live_progress_history
        {where}
        ORDER BY fetched_at DESC
        LIMIT ?
    """
    args.append(max(1, min(int(limit), 5000)))
    with connect() as conn:
        rows = conn.execute(sql, args).fetchall()
    return [dict(r) for r in rows]


def compute_doctor_pace(
    *,
    hospital_id: str | None = None,
    name_zh: str | None = None,
    clinic_date: str | None = None,
    min_points: int = 2,
) -> list[dict[str, Any]]:
    """Estimate patients/hour from successive current_number samples per doctor.

    Uses numeric ``current_number`` deltas over ``fetched_at`` elapsed hours.
    Negative deltas (reset / new session) are skipped.
    """
    rows = query_progress_history(
        hospital_id=hospital_id,
        name_zh=name_zh,
        clinic_date=clinic_date,
        limit=5000,
    )
    # Group by progress key
    groups: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        key = "|".join(
            [
                str(r.get("hospital_id") or ""),
                str(r.get("name_zh") or ""),
                str(r.get("clinic_date") or ""),
                str(r.get("session") or ""),
                str(r.get("local_clinic_code") or ""),
            ]
        )
        groups.setdefault(key, []).append(r)

    out: list[dict[str, Any]] = []
    for key, pts in groups.items():
        # oldest → newest
        pts_sorted = sorted(pts, key=lambda x: str(x.get("fetched_at") or ""))
        if len(pts_sorted) < min_points:
            continue
        samples: list[tuple[datetime, float]] = []
        for p in pts_sorted:
            raw = p.get("current_number")
            try:
                num = float(str(raw).strip())
            except (TypeError, ValueError):
                continue
            fa = p.get("fetched_at") or ""
            try:
                ts = datetime.fromisoformat(str(fa).replace("Z", "+00:00"))
            except ValueError:
                continue
            samples.append((ts, num))
        if len(samples) < min_points:
            continue
        deltas: list[float] = []
        hours: list[float] = []
        for (t0, n0), (t1, n1) in zip(samples, samples[1:]):
            dn = n1 - n0
            dt_h = (t1 - t0).total_seconds() / 3600.0
            if dn < 0 or dt_h <= 0:
                continue
            deltas.append(dn)
            hours.append(dt_h)
        if not hours:
            continue
        total_patients = sum(deltas)
        total_hours = sum(hours)
        pace = (total_patients / total_hours) if total_hours > 0 else None
        first = pts_sorted[0]
        out.append(
            {
                "hospital_id": first.get("hospital_id"),
                "name_zh": first.get("name_zh"),
                "clinic_date": first.get("clinic_date"),
                "session": first.get("session"),
                "local_clinic_code": first.get("local_clinic_code"),
                "samples": len(samples),
                "intervals_used": len(hours),
                "patients_delta": total_patients,
                "hours_elapsed": round(total_hours, 4),
                "avg_patients_per_hour": round(pace, 3) if pace is not None else None,
                "first_fetched_at": samples[0][0].isoformat(timespec="seconds"),
                "last_fetched_at": samples[-1][0].isoformat(timespec="seconds"),
                "first_number": samples[0][1],
                "last_number": samples[-1][1],
            }
        )
    out.sort(key=lambda x: (-(x.get("avg_patients_per_hour") or 0), x.get("name_zh") or ""))
    return out
