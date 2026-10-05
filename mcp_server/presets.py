"""18:00 Asia/Taipei open-registration presets (skeleton).

A preset stores a booking intent. ``scripts/run_preset_at_open.py`` waits until
``open_at`` (default 18:00 Asia/Taipei) and then calls the existing
``register_start`` dry-run path. Real POST still needs ``REG_AUTOSUBMIT`` /
``NTUH_REG_AUTOSUBMIT`` plus an NTUH reCAPTCHA v3 token. ``dry_run`` defaults
to true and the runner will not call ``register_submit`` unless the preset
has ``dry_run=0`` and the CLI is passed ``--allow-live``.
"""

from __future__ import annotations

import json
import secrets
from datetime import datetime, timedelta
from typing import Any

from mcp_server.db import TAIPEI, connect
from mcp_server.ntuh_scope import canonical_hospital_id, is_ntuh_hospital

STATUSES = ("pending", "armed", "running", "done", "failed", "cancelled")


def _now() -> datetime:
    return datetime.now(TAIPEI)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def default_open_at(now: datetime | None = None) -> str:
    """Next 18:00 Asia/Taipei (today if still before 18:00, else tomorrow)."""
    now = now or _now()
    target = now.replace(hour=18, minute=0, second=0, microsecond=0)
    if now >= target:
        target = target + timedelta(days=1)
    return _iso(target)


def _parse_open_at(value: str | None) -> str:
    if not value or not str(value).strip():
        return default_open_at()
    raw = str(value).strip()
    if len(raw) == 5 and raw[2] == ":":
        # HH:MM on the next occurrence in Asia/Taipei
        hh, mm = raw.split(":")
        now = _now()
        target = now.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
        if now >= target:
            target = target + timedelta(days=1)
        return _iso(target)
    dt = datetime.fromisoformat(raw)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TAIPEI)
    return _iso(dt.astimezone(TAIPEI))


def _dumps_list(value: Any) -> str | None:
    if value is None or value == "":
        return None
    if isinstance(value, str):
        parts = [p.strip() for p in value.replace("，", ",").split(",") if p.strip()]
        return json.dumps(parts, ensure_ascii=False)
    if isinstance(value, (list, tuple)):
        return json.dumps([str(x) for x in value], ensure_ascii=False)
    return json.dumps([str(value)], ensure_ascii=False)


def _row(r) -> dict[str, Any]:
    d = dict(r)
    for key in ("preferred_dates", "sessions"):
        raw = d.get(key)
        if isinstance(raw, str) and raw:
            try:
                d[key] = json.loads(raw)
            except json.JSONDecodeError:
                pass
    d["dry_run"] = bool(d.get("dry_run"))
    return d


def create_preset(
    *,
    hospital_id: str,
    doctor_name: str | None = None,
    doctor_id: str | None = None,
    dept: str | None = None,
    clinic: str | None = None,
    preferred_dates: Any = None,
    sessions: Any = None,
    open_at: str | None = None,
    dry_run: bool = True,
    slot_id: str | None = None,
    registration_url: str | None = None,
    notes: str | None = None,
) -> dict[str, Any]:
    hid = (hospital_id or "").strip()
    if not is_ntuh_hospital(hid):
        raise ValueError(f"hospital_id must be an NTUH campus, got {hid!r}")
    if not (doctor_name or doctor_id or slot_id):
        raise ValueError("provide doctor_name, doctor_id, or slot_id")
    now = _iso(_now())
    preset_id = "preset_" + secrets.token_hex(6)
    opened = _parse_open_at(open_at)
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO booking_presets (
              preset_id, doctor_name, doctor_id, hospital_id, dept, clinic,
              preferred_dates, sessions, open_at, status, dry_run, slot_id,
              registration_url, created_at, updated_at, notes
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                preset_id,
                (doctor_name or "").strip() or None,
                (doctor_id or "").strip() or None,
                hid,
                (dept or "").strip() or None,
                (clinic or "").strip() or None,
                _dumps_list(preferred_dates),
                _dumps_list(sessions),
                opened,
                "pending",
                1 if dry_run else 0,
                (slot_id or "").strip() or None,
                (registration_url or "").strip() or None,
                now,
                now,
                notes,
            ),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM booking_presets WHERE preset_id = ?", (preset_id,)
        ).fetchone()
    out = _row(row)
    out["canonical_hospital_id"] = canonical_hospital_id(hid)
    return out


def list_presets(
    *,
    status: str | None = None,
    hospital_id: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    lim = max(1, min(int(limit or 50), 200))
    where: list[str] = []
    params: list[Any] = []
    if status:
        where.append("status = ?")
        params.append(status)
    if hospital_id:
        where.append("hospital_id = ?")
        params.append(hospital_id)
    sql = "SELECT * FROM booking_presets"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY created_at DESC LIMIT ?"
    params.append(lim)
    with connect() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [_row(r) for r in rows]


def preset_get(preset_id: str) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM booking_presets WHERE preset_id = ?", (preset_id,)
        ).fetchone()
    return _row(row) if row else None


def cancel_preset(preset_id: str) -> dict[str, Any]:
    row = preset_get(preset_id)
    if not row:
        raise ValueError(f"preset not found: {preset_id}")
    if row["status"] == "running":
        raise ValueError("preset is running; not cancelling mid-flight")
    if row["status"] in ("done", "cancelled"):
        return row
    now = _iso(_now())
    with connect() as conn:
        conn.execute(
            """
            UPDATE booking_presets
               SET status = 'cancelled', updated_at = ?, last_message = ?
             WHERE preset_id = ?
            """,
            (now, "cancelled by user", preset_id),
        )
        conn.commit()
    return preset_get(preset_id) or row


def arm_preset(preset_id: str) -> dict[str, Any]:
    row = preset_get(preset_id)
    if not row:
        raise ValueError(f"preset not found: {preset_id}")
    if row["status"] not in ("pending", "failed"):
        raise ValueError(
            f"can only arm pending/failed presets (status={row['status']})"
        )
    now = _iso(_now())
    with connect() as conn:
        conn.execute(
            """
            UPDATE booking_presets
               SET status = 'armed', updated_at = ?, last_message = ?
             WHERE preset_id = ?
            """,
            (now, "armed for open_at", preset_id),
        )
        conn.commit()
    return preset_get(preset_id) or row


def mark_status(
    preset_id: str,
    status: str,
    message: str | None = None,
) -> None:
    if status not in STATUSES:
        raise ValueError(status)
    now = _iso(_now())
    with connect() as conn:
        conn.execute(
            """
            UPDATE booking_presets
               SET status = ?, updated_at = ?, last_message = ?
             WHERE preset_id = ?
            """,
            (status, now, (message or "")[:500], preset_id),
        )
        conn.commit()


def _json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2, default=str)


def preset_create(
    hospital_id: str,
    doctor_name: str | None = None,
    doctor_id: str | None = None,
    dept: str | None = None,
    clinic: str | None = None,
    preferred_dates: str | None = None,
    sessions: str | None = None,
    open_at: str | None = None,
    dry_run: bool = True,
    slot_id: str | None = None,
    registration_url: str | None = None,
    notes: str | None = None,
) -> str:
    """Save a booking intent for the NTUH 18:00 open (dry-run by default).

    Does not contact WebReg. Arm it, then run scripts/run_preset_at_open.py.

    Args:
        hospital_id: ntuh / ntuh_children / ntuh_cancer / ntuh_beihu / ntuh_jinshan / ntuh_hsinchu / ntuh_yunlin
            (legacy nhia_* / h_* ids that belong to those campuses are accepted)
        doctor_name: fuzzy name stored on the preset (or pass doctor_id / slot_id)
        doctor_id: optional doctors.doctor_id
        dept: department label (stored only)
        clinic: clinic label (stored only)
        preferred_dates: comma-separated YYYY-MM-DD
        sessions: comma-separated 上午,下午,夜間
        open_at: ISO datetime or HH:MM; default next 18:00 Asia/Taipei
        dry_run: default true — runner will not POST
        slot_id: optional clinic_slots.slot_id
        registration_url: optional RegForm or schedule URL
        notes: free text
    """
    try:
        row = create_preset(
            hospital_id=hospital_id,
            doctor_name=doctor_name,
            doctor_id=doctor_id,
            dept=dept,
            clinic=clinic,
            preferred_dates=preferred_dates,
            sessions=sessions,
            open_at=open_at,
            dry_run=dry_run,
            slot_id=slot_id,
            registration_url=registration_url,
            notes=notes,
        )
    except ValueError as e:
        return _json({"status": "error", "message": str(e)})
    return _json({"status": "ok", "preset": row})


def preset_list(
    status: str | None = None,
    hospital_id: str | None = None,
    limit: int = 50,
) -> str:
    """List booking presets (pending|armed|running|done|failed|cancelled)."""
    rows = list_presets(status=status or None, hospital_id=hospital_id or None, limit=limit)
    return _json({"count": len(rows), "presets": rows})


def preset_cancel(preset_id: str) -> str:
    """Cancel a preset that is not currently running."""
    try:
        row = cancel_preset(preset_id)
    except ValueError as e:
        return _json({"status": "error", "message": str(e)})
    return _json({"status": "ok", "preset": row})


def preset_arm(preset_id: str) -> str:
    """Mark a pending (or failed) preset armed so the 18:00 runner will fire it."""
    try:
        row = arm_preset(preset_id)
    except ValueError as e:
        return _json({"status": "error", "message": str(e)})
    return _json(
        {
            "status": "ok",
            "preset": row,
            "hint": (
                "Runner: PYTHONPATH=. python scripts/run_preset_at_open.py "
                f"--preset-id {preset_id}  (add --now to skip the wait). "
                "Default is dry-run. Real submit still needs REG_AUTOSUBMIT=1 "
                "and an NTUH reCAPTCHA token, plus --allow-live."
            ),
        }
    )


def register(mcp) -> None:
    """Attach preset tools. Public names: preset_create/list/cancel/arm."""
    for fn in (preset_create, preset_list, preset_cancel, preset_arm):
        mcp.tool()(fn)
