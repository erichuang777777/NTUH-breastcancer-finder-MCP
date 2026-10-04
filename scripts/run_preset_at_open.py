#!/usr/bin/env python3
"""Wait until a booking preset's open_at, then dry-run NTUH register_start.

Default is dry-run: this script calls ``register_start`` (no POST) and stores
the redacted result on the preset. It does **not** call ``register_submit``
unless the preset has dry_run=0 AND you pass ``--allow-live``. Even then,
``register_submit`` still requires REG_AUTOSUBMIT=1 (or NTUH_REG_AUTOSUBMIT=1)
and an NTUH reCAPTCHA v3 token. Do not put PATIENT_ID in the repo.

Examples:
  PYTHONPATH=. python scripts/run_preset_at_open.py --preset-id preset_abc --now
  PYTHONPATH=. python scripts/run_preset_at_open.py --all-armed
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mcp_server.db import TAIPEI, search_clinic_slots
from mcp_server.ntuh_scope import canonical_hospital_id
from mcp_server.presets import list_presets, mark_status, preset_get


def _parse_open(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TAIPEI)
    return dt.astimezone(TAIPEI)


def wait_until(open_at: str) -> None:
    target = _parse_open(open_at)
    while True:
        now = datetime.now(TAIPEI)
        remaining = (target - now).total_seconds()
        if remaining <= 0:
            return
        time.sleep(min(30.0, remaining))


def _pick_slot(preset: dict) -> tuple[str | None, str | None]:
    if preset.get("slot_id") or preset.get("registration_url"):
        return preset.get("slot_id"), preset.get("registration_url")
    dates = preset.get("preferred_dates") or []
    sessions = preset.get("sessions") or []
    date_from = dates[0] if isinstance(dates, list) and dates else None
    date_to = dates[-1] if isinstance(dates, list) and dates else None
    session = sessions[0] if isinstance(sessions, list) and sessions else None
    rows = search_clinic_slots(
        hospital_id=preset.get("hospital_id"),
        doctor_name_zh=preset.get("doctor_name"),
        date_from=date_from,
        date_to=date_to,
        session=session,
        status="可掛號",
        limit=5,
    )
    if not rows and preset.get("doctor_name"):
        rows = search_clinic_slots(
            hospital_id=preset.get("hospital_id"),
            doctor_name_zh=preset.get("doctor_name"),
            date_from=date_from,
            date_to=date_to,
            session=None,
            status=None,
            limit=5,
        )
    if not rows:
        return None, None
    top = rows[0]
    return top.get("slot_id"), top.get("registration_url")


def fire(preset: dict, *, allow_live: bool) -> dict:
    from mcp_server.booking import register_start, register_submit

    preset_id = preset["preset_id"]
    mark_status(preset_id, "running", "firing register_start")
    slot_id, registration_url = _pick_slot(preset)
    hid = canonical_hospital_id(preset.get("hospital_id"))
    if not slot_id and not registration_url:
        msg = (
            "no slot_id/registration_url and no matching clinic_slots; "
            "not calling WebReg"
        )
        mark_status(preset_id, "failed", msg)
        return {"status": "failed", "message": msg, "preset_id": preset_id}

    raw = register_start(
        slot_id=slot_id,
        registration_url=registration_url,
        hospital_id=hid,
        patient_ref=preset_id,
    )
    try:
        started = json.loads(raw)
    except json.JSONDecodeError:
        started = {"status": "error", "message": raw[:300]}

    dry = bool(preset.get("dry_run", True))
    if dry or not allow_live:
        ok = started.get("status") in ("ok", "dry_run", "dry_run_filled")
        msg = (
            "dry-run (no POST). Real submit still needs REG_AUTOSUBMIT=1 "
            "or NTUH_REG_AUTOSUBMIT=1, plus reCAPTCHA (mint_ntuh_recaptcha). "
            + str(started.get("message") or started.get("status"))
        )
        mark_status(preset_id, "done" if ok else "failed", msg)
        return {
            "preset_id": preset_id,
            "status": "done" if ok else "failed",
            "dry_run": True,
            "register_start": started,
            "message": msg,
        }

    token = started.get("confirm_token")
    if not token:
        msg = "live requested but register_start did not return confirm_token"
        mark_status(preset_id, "failed", msg)
        return {"preset_id": preset_id, "status": "failed", "register_start": started, "message": msg}
    submitted_raw = register_submit(confirm_token=token, autosubmit=True)
    try:
        submitted = json.loads(submitted_raw)
    except json.JSONDecodeError:
        submitted = {"status": "error", "message": submitted_raw[:300]}
    ok = submitted.get("status") in ("ok", "submitted")
    mark_status(
        preset_id,
        "done" if ok else "failed",
        str(submitted.get("message") or submitted.get("status"))[:500],
    )
    return {
        "preset_id": preset_id,
        "status": "done" if ok else "failed",
        "dry_run": False,
        "register_submit": submitted,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="Fire NTUH booking presets at open_at")
    ap.add_argument("--preset-id", action="append", default=[])
    ap.add_argument("--all-armed", action="store_true")
    ap.add_argument("--now", action="store_true", help="do not wait for open_at")
    ap.add_argument(
        "--allow-live",
        action="store_true",
        help="allow register_submit only when the preset dry_run flag is false",
    )
    args = ap.parse_args()
    presets: list[dict] = []
    if args.all_armed:
        presets.extend(list_presets(status="armed", limit=200))
    for pid in args.preset_id:
        row = preset_get(pid)
        if not row:
            print(json.dumps({"status": "error", "message": f"missing {pid}"}, ensure_ascii=False))
            sys.exit(1)
        presets.append(row)
    if not presets:
        print("no presets selected (pass --preset-id or --all-armed)", file=sys.stderr)
        sys.exit(2)
    results = []
    for preset in presets:
        if preset["status"] == "cancelled":
            results.append({"preset_id": preset["preset_id"], "status": "skipped", "message": "cancelled"})
            continue
        if not args.now:
            print(f"waiting until {preset['open_at']} for {preset['preset_id']}", flush=True)
            wait_until(preset["open_at"])
        results.append(fire(preset, allow_live=args.allow_live))
    print(json.dumps(results, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
