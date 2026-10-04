"""Short-lived confirm tokens for register_start → register_submit handoff.

Stored as JSON under schedules/raw/ntuh_reg_dryrun/confirm_tokens/.
Never stores full PATIENT_ID — only redacted patient + slot + artifact paths.
"""

from __future__ import annotations

import json
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
TOKEN_DIR = ROOT / "schedules" / "raw" / "ntuh_reg_dryrun" / "confirm_tokens"
TAIPEI = timezone(timedelta(hours=8))
DEFAULT_TTL_MIN = 20


def _ensure_dir() -> Path:
    TOKEN_DIR.mkdir(parents=True, exist_ok=True)
    return TOKEN_DIR


def mint_confirm_token(
    *,
    hospital_id: str,
    slot: dict[str, Any],
    artifact_paths: list[str],
    patient_redacted: dict[str, Any] | None = None,
    captcha: dict[str, Any] | None = None,
    ttl_min: int = DEFAULT_TTL_MIN,
) -> str:
    _ensure_dir()
    token = secrets.token_urlsafe(18)
    now = datetime.now(TAIPEI)
    # Slot subset only — no PII
    slot_safe = {
        k: slot.get(k)
        for k in (
            "slot_id",
            "hospital_id",
            "name_zh",
            "clinic_date",
            "session",
            "status",
            "source_url",
            "registration_url",
            "local_clinic_code",
            "department_zh",
            "notes",
        )
    }
    cap_safe = {
        k: v
        for k, v in (captcha or {}).items()
        if k not in ("recaptcha_token", "guess")  # guess is short-lived OCR
    }
    payload = {
        "confirm_token": token,
        "created_at": now.isoformat(timespec="seconds"),
        "expires_at": (now + timedelta(minutes=ttl_min)).isoformat(timespec="seconds"),
        "hospital_id": hospital_id,
        "slot": slot_safe,
        "artifact_paths": artifact_paths,
        "patient_redacted": patient_redacted or {},
        "captcha": cap_safe,
    }
    path = TOKEN_DIR / f"{token}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return token


def load_confirm_token(token: str) -> dict[str, Any] | None:
    if not token or any(c in token for c in ("/", "..", "\\")):
        return None
    path = TOKEN_DIR / f"{token}.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    exp = data.get("expires_at")
    if exp:
        try:
            exp_dt = datetime.fromisoformat(exp)
            if exp_dt.tzinfo is None:
                exp_dt = exp_dt.replace(tzinfo=TAIPEI)
            if datetime.now(TAIPEI) > exp_dt:
                return None
        except ValueError:
            return None
    return data
