#!/usr/bin/env python3
"""Booking / live-number / registration tools for the ntuh-breastcancer-finder MCP.

Registered onto the shared FastMCP instance by ``mcp_server.server``.
Spec: ``docs/MCP.md``. Deprecated entry (warns, then starts the unified server):

  PYTHONPATH=. .venv/bin/python -m mcp_server.booking
"""

from __future__ import annotations

import json
import os
import sys
from datetime import date
from pathlib import Path
from typing import Any

# Ensure repo root on path when launched as script / MCP child process
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from mcp_server.db import (
    get_slot_by_id,
    log_scrape_run,
    search_clinic_slots,
    upsert_live_progress,
)
from mcp_server.tokens import load_confirm_token, mint_confirm_token


def _json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2, default=str)


def search_bookable(
    hospital_id: str | None = None,
    doctor_name_zh: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    session: str | None = None,
    status: str | None = "可掛號",
    limit: int = 50,
) -> str:
    """Search bookable ClinicSlot rows from SQLite (does not invent slots).

    For TW-wide doctor discovery use search_doctors on this same server first.

    Args:
        hospital_id: e.g. ntuh_cancer / ntuh_hsinchu / ntuh; omit = NTUH family
        doctor_name_zh: fuzzy name filter
        date_from / date_to: YYYY-MM-DD (default today .. +14d Asia/Taipei)
        session: 上午 / 下午 / 夜間
        status: default 可掛號 (prefix match)
        limit: max rows (default 50, cap 200)
    """
    rows = search_clinic_slots(
        hospital_id=hospital_id or None,
        doctor_name_zh=doctor_name_zh or None,
        date_from=date_from or None,
        date_to=date_to or None,
        session=session or None,
        status=status if status is not None else "可掛號",
        limit=limit,
    )
    return _json(
        {
            "count": len(rows),
            "filter": {
                "hospital_id": hospital_id or "ntuh* (all NTUH-system rows in ntuh.db)",
                "doctor_name_zh": doctor_name_zh,
                "date_from": date_from,
                "date_to": date_to,
                "session": session,
                "status": status,
                "limit": limit,
            },
            "slots": rows,
        }
    )


def get_live_number(
    hospital_id: str,
    doctor_name_zh: str | None = None,
    session: str | None = None,
    campus_code: str | None = None,
    persist: bool = True,
    from_db: bool = False,
) -> str:
    """Fetch live clinic light-number (breast-filtered) or read the DB snapshot.

    Args:
        hospital_id: any adapter id (ntuh*, cgmh*, vghtpe, kmuh, cheng_hsin, …)
        doctor_name_zh: optional name filter
        session: 上午/下午/夜間; omit = all returned sessions
        campus_code: T0/CH/C0/T2/T3/T4/T7/Y0 hint (sets NTUH_REG_HOSP_CODE for this call)
        persist: upsert into live_progress SQLite table (ignored when from_db)
        from_db: if true, return the latest live_progress rows already stored
            (no network). get_progress_pace always reads live_progress_history.
    """
    from mcp_server.ntuh_scope import canonical_hospital_id, is_ntuh_hospital

    if not is_ntuh_hospital(hospital_id):
        return _json(
            {
                "status": "error",
                "message": f"NTUH-only server; refusing hospital_id={hospital_id!r}",
            }
        )
    hospital_id = canonical_hospital_id(hospital_id)

    if from_db:
        from mcp_server.db import query_live_progress

        rows = query_live_progress(
            hospital_id=hospital_id,
            name_zh=doctor_name_zh,
            session=session,
        )
        return _json(
            {
                "status": "ok",
                "source": "live_progress",
                "hospital_id": hospital_id,
                "count": len(rows),
                "progress": rows,
            }
        )

    from schema.adapters import ADAPTERS

    if hospital_id not in ADAPTERS:
        return _json(
            {
                "status": "error",
                "message": f"unknown hospital_id={hospital_id!r}; known={sorted(ADAPTERS)[:30]}…",
            }
        )

    prev_code = os.environ.get("NTUH_REG_HOSP_CODE")
    if campus_code:
        os.environ["NTUH_REG_HOSP_CODE"] = campus_code.strip().upper()
    try:
        adapter = ADAPTERS[hospital_id]()
        rows = adapter.fetch_live_progress()
    except Exception as e:  # noqa: BLE001
        return _json({"status": "error", "hospital_id": hospital_id, "message": str(e)})
    finally:
        if campus_code:
            if prev_code is None:
                os.environ.pop("NTUH_REG_HOSP_CODE", None)
            else:
                os.environ["NTUH_REG_HOSP_CODE"] = prev_code

    # Normalize clinic_date to str for JSON / DB
    for r in rows:
        cd = r.get("clinic_date")
        if isinstance(cd, date):
            r["clinic_date"] = cd.isoformat()

    if doctor_name_zh:
        rows = [r for r in rows if doctor_name_zh in (r.get("name_zh") or "")]
    if session:
        rows = [r for r in rows if (r.get("session") or "") == session]

    upserted = 0
    if persist and rows:
        try:
            upserted = upsert_live_progress(rows)
        except Exception as e:  # noqa: BLE001
            return _json(
                {
                    "status": "ok_partial",
                    "hospital_id": hospital_id,
                    "count": len(rows),
                    "upserted": 0,
                    "persist_error": str(e),
                    "progress": rows,
                }
            )

    return _json(
        {
            "status": "ok",
            "hospital_id": hospital_id,
            "count": len(rows),
            "upserted": upserted,
            "progress": rows,
        }
    )


def _resolve_slot(
    *,
    slot_id: str | None,
    registration_url: str | None,
    hospital_id: str | None,
) -> tuple[dict[str, Any], str]:
    """Return (slot_dict, hospital_id)."""
    if not slot_id and not registration_url:
        raise ValueError("provide slot_id or registration_url")
    slot: dict[str, Any] | None = None
    if slot_id:
        slot = get_slot_by_id(slot_id)
        if not slot:
            raise ValueError(f"slot_id not found in DB: {slot_id}")
    else:
        slot = {
            "slot_id": None,
            "hospital_id": hospital_id or "ntuh_cancer",
            "registration_url": registration_url,
            "source_url": registration_url,
            "name_zh": None,
            "status": "可掛號",
        }
    hid = hospital_id or slot.get("hospital_id") or "ntuh_cancer"
    if registration_url:
        slot = dict(slot)
        slot["registration_url"] = registration_url
    return slot, hid


def register_start(
    slot_id: str | None = None,
    registration_url: str | None = None,
    hospital_id: str | None = None,
    patient_ref: str | None = None,
) -> str:
    """Dry-run WebReg (NTUH/CGMH/VGHTPE/MMH/NCKU/KMUH/VGHTC/CMUH/FEMH/TSGH): navigate + fill + OCR if captcha; do NOT POST.

    Patient PII is read from server env (PATIENT_ID, PATIENT_BIRTHDATE, …).
    Do NOT pass full national ID in tool args — optional patient_ref is a label only.

    Returns dry_run_filled + confirm_token for a later register_submit.
    Requires patient authorization for proxy registration; public WebReg only.
    """
    from schema.registration import (
        CaptchaRequired,
        RegistrationError,
        get_registration_adapter,
        load_patient_from_env,
        supports_registration,
    )
    from schema.registration.patient_env import autosubmit_enabled

    try:
        slot, hid = _resolve_slot(
            slot_id=slot_id,
            registration_url=registration_url,
            hospital_id=hospital_id,
        )
    except ValueError as e:
        return _json({"status": "error", "message": str(e)})

    from mcp_server.ntuh_scope import canonical_hospital_id, is_ntuh_hospital

    if not is_ntuh_hospital(hid):
        return _json(
            {
                "status": "error",
                "message": f"NTUH-only server; refusing hospital_id={hid!r}",
            }
        )
    hid = canonical_hospital_id(hid)

    if not supports_registration(hid):
        return _json(
            {
                "status": "error",
                "message": f"no RegistrationAdapter for hospital_id={hid!r}",
                "hint": "Supported: ntuh* / cgmh* / vghtpe / mmh* / ncku / kmuh / vghtc / cmuh / femh / tsgh (+ stubs)",
            }
        )

    try:
        patient = load_patient_from_env(hospital_id=hid)
    except RegistrationError as e:
        return _json(
            {
                "status": "error",
                "message": str(e),
                "hint": "Set PATIENT_ID + PATIENT_BIRTHDATE in MCP server env",
                "patient_ref": patient_ref,
            }
        )

    blockers: list[str] = []
    try:
        reg = get_registration_adapter(hid)
        result = reg.run_dry_or_submit(slot, patient, autosubmit=False)
    except CaptchaRequired as e:
        return _json(
            {
                "status": "error",
                "message": str(e),
                "captcha_kind": e.captcha_kind,
                "detail": e.detail,
            }
        )
    except RegistrationError as e:
        return _json({"status": "error", "message": str(e)})

    captcha = {}
    if result.session and result.session.captcha:
        captcha = {
            k: v
            for k, v in result.session.captcha.items()
            if k != "recaptcha_token"
        }
    if hid.startswith("ntuh") and not captcha.get("recaptcha_token_present"):
        blockers.append(
            "reCAPTCHA v3 token missing — use mint_ntuh_recaptcha or "
            "NTUH_RECAPTCHA_TOKEN before register_submit"
        )
    if not autosubmit_enabled(hid):
        blockers.append(
            "submit gated: set REG_AUTOSUBMIT=1 or hospital "
            "NTUH_/CGMH_/VGHTPE_/MMH_/NCKU_/KMUH_REG_AUTOSUBMIT=1 or REG_AUTOSUBMIT=1"
        )

    confirm = mint_confirm_token(
        hospital_id=hid,
        slot=slot,
        artifact_paths=result.artifact_paths,
        patient_redacted=patient.redacted(),
        captcha=captcha,
    )

    log_scrape_run(
        kind="registration",
        hospital_id=hid,
        status="dry_run",
        message=result.message[:200],
        meta={
            "confirm_token_suffix": confirm[-6:],
            "patient_ref": patient_ref,
            "slot_id": slot.get("slot_id"),
            "status": result.status,
        },
    )

    return _json(
        {
            "status": result.status,
            "message": result.message,
            "confirm_token": confirm,
            "hospital_id": hid,
            "patient_ref": patient_ref,
            "patient_redacted": patient.redacted(),
            "payload_redacted": result.payload_redacted,
            "artifact_paths": result.artifact_paths,
            "captcha": captcha,
            "blockers": blockers,
            "session_meta": (result.session.meta if result.session else {}),
        }
    )


def register_submit(
    confirm_token: str,
    autosubmit: bool = False,
    recaptcha_token: str | None = None,
) -> str:
    """Submit registration after register_start. HIGH RISK — double-gated.

    Requires:
      1) autosubmit argument == true
      2) server env REG_AUTOSUBMIT=1 or hospital *_REG_AUTOSUBMIT=1
      3) NTUH only: reCAPTCHA v3 token (arg / env / Playwright mint)

    Re-runs navigate/fill/submit with current env patient PII (confirm_token
    only carries slot + artifacts; newx tokens expire quickly).
    Patient must have authorized proxy booking; public WebReg only.
    """
    from schema.registration import (
        CaptchaRequired,
        RegistrationError,
        get_registration_adapter,
        load_patient_from_env,
        supports_registration,
    )
    from schema.registration.patient_env import autosubmit_enabled

    if not autosubmit:
        return _json(
            {
                "status": "blocked",
                "message": "autosubmit arg must be true (explicit confirm)",
            }
        )

    stored = load_confirm_token(confirm_token)
    if not stored:
        return _json(
            {
                "status": "blocked",
                "message": "confirm_token missing or expired — run register_start again",
            }
        )

    if recaptcha_token:
        os.environ["NTUH_RECAPTCHA_TOKEN"] = recaptcha_token.strip()

    from mcp_server.ntuh_scope import canonical_hospital_id, is_ntuh_hospital

    hid = stored.get("hospital_id") or "ntuh"
    if not is_ntuh_hospital(hid):
        return _json(
            {
                "status": "blocked",
                "message": f"NTUH-only server; refusing hospital_id={hid!r}",
            }
        )
    hid = canonical_hospital_id(hid)
    slot = stored.get("slot") or {}
    if not supports_registration(hid):
        return _json(
            {
                "status": "blocked",
                "message": f"no RegistrationAdapter for hospital_id={hid!r}",
            }
        )
    if not autosubmit_enabled(hid):
        return _json(
            {
                "status": "blocked",
                "message": (
                    "server env REG_AUTOSUBMIT=1 or hospital "
                    "*_REG_AUTOSUBMIT=1 required"
                ),
            }
        )
    try:
        patient = load_patient_from_env(hospital_id=hid)
    except RegistrationError as e:
        return _json({"status": "error", "message": str(e)})

    try:
        reg = get_registration_adapter(hid)
        result = reg.run_dry_or_submit(slot, patient, autosubmit=True)
    except CaptchaRequired as e:
        log_scrape_run(
            kind="registration",
            hospital_id=hid,
            status="blocked",
            message=str(e)[:200],
            meta={"captcha_kind": e.captcha_kind, "confirm_suffix": confirm_token[-6:]},
        )
        return _json(
            {
                "status": "blocked",
                "message": str(e),
                "captcha_kind": e.captcha_kind,
                "detail": e.detail,
            }
        )
    except RegistrationError as e:
        log_scrape_run(
            kind="registration",
            hospital_id=hid,
            status="error",
            message=str(e)[:200],
            meta={"confirm_suffix": confirm_token[-6:]},
        )
        return _json({"status": "error", "message": str(e)})

    captcha = {}
    if result.session and result.session.captcha:
        captcha = {
            k: v
            for k, v in result.session.captcha.items()
            if k != "recaptcha_token"
        }

    log_scrape_run(
        kind="registration",
        hospital_id=hid,
        status=result.status,
        message=result.message[:200],
        meta={
            "confirm_suffix": confirm_token[-6:],
            "artifact_count": len(result.artifact_paths),
        },
    )

    return _json(
        {
            "status": result.status,
            "message": result.message,
            "hospital_id": hid,
            "artifact_paths": result.artifact_paths,
            "payload_redacted": result.payload_redacted,
            "captcha": captcha,
            "raw": result.raw,
        }
    )




def get_progress_pace(
    hospital_id: str | None = None,
    doctor_name_zh: str | None = None,
    clinic_date: str | None = None,
    min_points: int = 2,
) -> str:
    """Estimate avg patients/hour from live_progress_history samples.

    Requires prior get_live_number / upsert_live_progress runs so history has
    ≥ min_points numeric current_number samples per doctor key.
    """
    from mcp_server.db import compute_doctor_pace

    rows = compute_doctor_pace(
        hospital_id=hospital_id or None,
        name_zh=doctor_name_zh or None,
        clinic_date=clinic_date or None,
        min_points=min_points,
    )
    return _json(
        {
            "count": len(rows),
            "filter": {
                "hospital_id": hospital_id,
                "doctor_name_zh": doctor_name_zh,
                "clinic_date": clinic_date,
                "min_points": min_points,
            },
            "pace": rows,
        }
    )


def mint_ntuh_recaptcha(
    page_url: str,
    site_key: str | None = None,
    action: str = "submit",
    set_env: bool = True,
) -> str:
    """Mint a reCAPTCHA v3 token by opening NTUH RegForm URL in Playwright Chromium.

    page_url must be an https://reg.ntuh.gov.tw/... RegForm (or schedule) page
    so grecaptcha loads on the correct origin. Returns token length only in
    logs; full token is returned once for the caller to pass to register_submit
    or store as NTUH_RECAPTCHA_TOKEN (set_env=true writes process env).
    """
    from schema.registration.recaptcha_playwright import try_mint_recaptcha_token

    result = try_mint_recaptcha_token(page_url, site_key=site_key, action=action)
    if result.get("ok") and result.get("token") and set_env:
        os.environ["NTUH_RECAPTCHA_TOKEN"] = result["token"]
        result["env_set"] = True
    else:
        result["env_set"] = False
    # Return token to caller (needed for submit); do not log elsewhere
    return _json(result)


_BOOKING_TOOLS = (
    search_bookable,
    get_live_number,
    register_start,
    register_submit,
    get_progress_pace,
    mint_ntuh_recaptcha,
)


def register(mcp) -> None:
    """Attach booking tools to the shared ntuh-breastcancer-finder server."""
    for fn in _BOOKING_TOOLS:
        mcp.tool()(fn)


def main() -> None:
    """Deprecated dual entrypoint: warn and start ntuh-breastcancer-finder."""
    import warnings

    warnings.warn(
        "mcp_server.booking is deprecated; tools live on ntuh-breastcancer-finder "
        "(python -m mcp_server.server).",
        DeprecationWarning,
        stacklevel=2,
    )
    print(
        "DEPRECATED: mcp_server.booking merged into ntuh-breastcancer-finder. "
        "Use: python -m mcp_server.server",
        file=sys.stderr,
    )
    from mcp_server.server import main as server_main

    server_main()


if __name__ == "__main__":
    main()
