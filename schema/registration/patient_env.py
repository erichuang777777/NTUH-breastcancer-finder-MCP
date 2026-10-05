"""Load patient PII strictly from environment variables.

Never commit real values. Prefer a secrets manager / direnv / systemd EnvironmentFile.
"""

from __future__ import annotations

import os
import re

from .base import PatientFields, RegistrationError

# Documented env vars (also listed in docs/NTUH_WEBREG_FLOW.md / REG_MULTIHOSP.md).
REQUIRED_ENV_VARS = (
    "PATIENT_ID",  # full national ID / chart / other — or PATIENT_ID_LAST4 + full via PATIENT_ID
    "PATIENT_BIRTHDATE",  # YYYY-MM-DD Gregorian
)

OPTIONAL_ENV_VARS = (
    "PATIENT_ID_TYPE",  # personID|chartNo|other|newBorn  default personID
    "PATIENT_ID_LAST4",  # optional sanity check against PATIENT_ID
    "PATIENT_NAME",  # may be required on 初診 follow-up page
    "PATIENT_PHONE",  # may be required on 初診 follow-up page
    "PATIENT_NATION",  # default TWN
    "REG_AUTOSUBMIT",  # 1 — global submit gate (all hospitals)
    "NTUH_REG_AUTOSUBMIT",  # 1 to allow real POST (NTUH family)
    "CGMH_REG_AUTOSUBMIT",  # 1 to allow real POST (長庚 family)
    "VGHTPE_REG_AUTOSUBMIT",  # 1 to allow real POST (北榮)
    "MMH_REG_AUTOSUBMIT",  # 1 to allow real POST (馬偕)
    "NCKU_REG_AUTOSUBMIT",  # 1 to allow real POST (成大)
    "KMUH_REG_AUTOSUBMIT",  # 1 to allow real POST (高醫)
    "TSGH_REG_AUTOSUBMIT",  # 1 to allow real POST (三總)
    "FEMH_REG_AUTOSUBMIT",  # 1 to allow real POST (亞東)
    "CMUH_REG_AUTOSUBMIT",  # 1 to allow real POST (中國附醫)
    "VGHTC_REG_AUTOSUBMIT",  # 1 to allow real POST (中榮)
    "CSH_REG_AUTOSUBMIT",  # 1 to allow real POST (中山)
    "SHUANG_HO_REG_AUTOSUBMIT",  # 1 to allow real POST (雙和)
    "WANFANG_REG_AUTOSUBMIT",  # 1 to allow real POST (萬芳)
    "TMUH_REG_AUTOSUBMIT",  # 1 to allow real POST (北醫)
    "EDAH_REG_AUTOSUBMIT",  # 1 to allow real POST (義大)
    "CHENG_HSIN_REG_AUTOSUBMIT",  # 1 to allow real POST (振興)
    "WEISHIN_REG_AUTOSUBMIT",  # 1 to allow real POST (維馨)
    "CGH_REG_AUTOSUBMIT",  # 1 to allow real POST (國泰) — do not set in agents
    "SKH_REG_AUTOSUBMIT",  # 1 to allow real POST (新光)
    "CHIMEI_REG_AUTOSUBMIT",  # 1 to allow real POST (奇美)
    "VGHKS_REG_AUTOSUBMIT",  # 1 to allow real POST (高榮)
    "KOO_FOUNDATION_REG_AUTOSUBMIT",  # 1 to allow real POST (和信) — leave unset
    "CYCH_REG_AUTOSUBMIT",  # 1 to allow real POST (嘉基) — leave unset
    "LOTUNG_POHAI_REG_AUTOSUBMIT",  # 1 to allow real POST (羅東博愛) — leave unset
    "CCH_REG_AUTOSUBMIT",  # 1 to allow real POST (彰基) — leave unset
    "TYH_REG_AUTOSUBMIT",  # 1 to allow real POST (桃園醫院) — leave unset
    "YUAN_GENERAL_REG_AUTOSUBMIT",  # 1 to allow real POST (阮綜合) — leave unset
    "TZUCHI_REG_AUTOSUBMIT",  # 1 to allow real POST (慈濟體系) — leave unset
    "TZUCHI_DALIN_REG_AUTOSUBMIT",  # 1 to allow real POST (大林慈濟) — leave unset
    "TZUCHI_HUALIEN_REG_AUTOSUBMIT",  # 1 to allow real POST (花蓮慈濟) — leave unset
    "TZUCHI_TAIPEI_REG_AUTOSUBMIT",  # 1 to allow real POST (台北慈濟) — leave unset
    "MMH_WEBWORD",  # optional 馬偕網路密碼
    "NTUH_RECAPTCHA_TOKEN",  # optional pre-minted gRecaptcha v3 token
    "NTUH_REG_HOSP_CODE",  # T0|CH|C0|T2|T3|T4|T7|Y0 override
    "NTUH_RECAPTCHA_HEADLESS",  # 0 = headed Chromium for mint
    "NTUH_RECAPTCHA_TIMEOUT_MS",  # Playwright timeout, default 60000
    "CGMH_REG_CAMPUS",  # 3=Linkou|1=Taipei|2=Keelung|6=Chiayi|8=Kaohsiung
    "BREAST_CARE_DB",  # override SQLite path for MCP
)

_AUTOSUBMIT_BY_PREFIX: dict[str, str] = {
    "ntuh": "NTUH_REG_AUTOSUBMIT",
    "ntuh_cancer": "NTUH_REG_AUTOSUBMIT",
    "ntuh_hsinchu": "NTUH_REG_AUTOSUBMIT",
    "cgmh": "CGMH_REG_AUTOSUBMIT",
    "cgmh_linkou": "CGMH_REG_AUTOSUBMIT",
    "cgmh_taipei": "CGMH_REG_AUTOSUBMIT",
    "cgmh_keelung": "CGMH_REG_AUTOSUBMIT",
    "cgmh_chiayi": "CGMH_REG_AUTOSUBMIT",
    "cgmh_kaohsiung": "CGMH_REG_AUTOSUBMIT",
    "vghtpe": "VGHTPE_REG_AUTOSUBMIT",
    "mmh": "MMH_REG_AUTOSUBMIT",
    "mmh_taipei": "MMH_REG_AUTOSUBMIT",
    "mmh_tamsui": "MMH_REG_AUTOSUBMIT",
    "ncku": "NCKU_REG_AUTOSUBMIT",
    "kmuh": "KMUH_REG_AUTOSUBMIT",
    "vghtc": "VGHTC_REG_AUTOSUBMIT",
    "cmuh": "CMUH_REG_AUTOSUBMIT",
    "femh": "FEMH_REG_AUTOSUBMIT",
    "tsgh": "TSGH_REG_AUTOSUBMIT",
    "csh": "CSH_REG_AUTOSUBMIT",
    "shuang_ho": "SHUANG_HO_REG_AUTOSUBMIT",
    "wanfang": "WANFANG_REG_AUTOSUBMIT",
    "tmuh": "TMUH_REG_AUTOSUBMIT",
    "edah": "EDAH_REG_AUTOSUBMIT",
    "cheng_hsin": "CHENG_HSIN_REG_AUTOSUBMIT",
    "weishin": "WEISHIN_REG_AUTOSUBMIT",
    "cgh": "CGH_REG_AUTOSUBMIT",
    "skh": "SKH_REG_AUTOSUBMIT",
    "chimei": "CHIMEI_REG_AUTOSUBMIT",
    "vghks": "VGHKS_REG_AUTOSUBMIT",
    "koo_foundation": "KOO_FOUNDATION_REG_AUTOSUBMIT",
    "cych": "CYCH_REG_AUTOSUBMIT",
    "lotung_pohai": "LOTUNG_POHAI_REG_AUTOSUBMIT",
    "cch": "CCH_REG_AUTOSUBMIT",
    "tyh": "TYH_REG_AUTOSUBMIT",
    "yuan_general": "YUAN_GENERAL_REG_AUTOSUBMIT",
    "tzuchi": "TZUCHI_REG_AUTOSUBMIT",
    "tzuchi_dalin": "TZUCHI_DALIN_REG_AUTOSUBMIT",
    "tzuchi_hualien": "TZUCHI_HUALIEN_REG_AUTOSUBMIT",
    "tzuchi_taipei": "TZUCHI_TAIPEI_REG_AUTOSUBMIT",
}


def required_env_vars() -> list[str]:
    return list(REQUIRED_ENV_VARS)


def optional_env_vars() -> list[str]:
    return list(OPTIONAL_ENV_VARS)


def load_patient_from_env(*, hospital_id: str = "ntuh") -> PatientFields:
    """Read patient fields from env. Raises RegistrationError if required missing."""
    id_value = (os.environ.get("PATIENT_ID") or "").strip()
    birth = (os.environ.get("PATIENT_BIRTHDATE") or "").strip()
    if not id_value:
        raise RegistrationError(
            hospital_id,
            "missing env PATIENT_ID (full ID/chart; never hardcode in repo)",
        )
    if not birth or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", birth):
        raise RegistrationError(
            hospital_id,
            "missing/invalid env PATIENT_BIRTHDATE (expect YYYY-MM-DD)",
        )
    last4 = (os.environ.get("PATIENT_ID_LAST4") or "").strip()
    if last4 and not id_value.endswith(last4):
        raise RegistrationError(
            hospital_id,
            "PATIENT_ID_LAST4 does not match PATIENT_ID suffix",
        )
    id_type = (os.environ.get("PATIENT_ID_TYPE") or "personID").strip()
    if id_type not in ("personID", "chartNo", "other", "newBorn"):
        raise RegistrationError(hospital_id, f"invalid PATIENT_ID_TYPE={id_type!r}")
    return PatientFields(
        id_type=id_type,
        id_value=id_value,
        birthdate=birth,
        name_zh=(os.environ.get("PATIENT_NAME") or "").strip(),
        phone=(os.environ.get("PATIENT_PHONE") or "").strip(),
        nation=(os.environ.get("PATIENT_NATION") or "TWN").strip() or "TWN",
    )



def dry_run_identity(patient: PatientFields) -> dict[str, str | bool]:
    """Identity strings for a dry-run fill.

    Uses PATIENT_ID / PATIENT_BIRTHDATE when present. If either is empty,
    substitutes a synthetic placeholder (national-ID length 10; Gregorian
    1980-01-01) so the live form can be filled. Callers must redact before
    writing artifacts and must not submit.
    """
    idv = (patient.id_value or "").strip()
    birth = (patient.birthdate or "").strip()
    synthetic = False
    if not idv:
        idv = "A000000000"
        synthetic = True
    if len(idv) != 10 and synthetic:
        idv = (idv + "0" * 10)[:10]
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", birth):
        birth = "1980-01-01"
        synthetic = True
    y, m, d = birth.split("-")
    roc = int(y) - 1911
    return {
        "id_value": idv,
        "birthdate": birth,
        "synthetic": synthetic,
        "roc_yyyymmdd": f"{roc:03d}{int(m):02d}{int(d):02d}",
        "roc_unpadded": f"{roc}{int(m)}{int(d)}",
        "gregorian_slash": f"{y}/{int(m):02d}/{int(d):02d}",
        "gregorian_compact": f"{y}{int(m):02d}{int(d):02d}",
    }



def redacted_dry_identity(patient: PatientFields, ident: dict) -> dict[str, str]:
    """Redacted view for artifacts. Synthetic placeholders are not written out."""
    synthetic = bool(ident.get("synthetic"))
    shown_birth = "" if synthetic else str(ident.get("birthdate") or patient.birthdate or "")
    red = PatientFields(
        id_type=patient.id_type,
        id_value=str(ident.get("id_value") or ""),
        birthdate=shown_birth,
        name_zh=patient.name_zh,
        phone=patient.phone,
        nation=patient.nation,
    ).redacted()
    # Blank only the fields that were placeholders, not a real PATIENT_* value.
    if not (patient.birthdate or "").strip():
        red["birthdate"] = ""
    if not (patient.id_value or "").strip():
        red["id_last4"] = ""
    return red


def autosubmit_enabled(hospital_id: str | None = None) -> bool:
    """Double-gate helper: hospital-specific flag OR global REG_AUTOSUBMIT=1.

    NTUH keeps ``NTUH_REG_AUTOSUBMIT``; CGMH ``CGMH_REG_AUTOSUBMIT``;
    VGHTPE ``VGHTPE_REG_AUTOSUBMIT``; MMH ``MMH_REG_AUTOSUBMIT``;
    NCKU ``NCKU_REG_AUTOSUBMIT``; KMUH ``KMUH_REG_AUTOSUBMIT``.
    Global ``REG_AUTOSUBMIT=1`` enables all.
    When hospital_id is omitted, any of the known flags (incl. legacy NTUH) counts.
    """
    if (os.environ.get("REG_AUTOSUBMIT") or "").strip() == "1":
        return True
    if hospital_id:
        hid = hospital_id.strip()
        env_name = _AUTOSUBMIT_BY_PREFIX.get(hid)
        if env_name is None and hid.startswith("cgmh"):
            env_name = "CGMH_REG_AUTOSUBMIT"
        if env_name is None and hid.startswith("ntuh"):
            env_name = "NTUH_REG_AUTOSUBMIT"
        if env_name is None and hid.startswith("mmh"):
            env_name = "MMH_REG_AUTOSUBMIT"
        if env_name and (os.environ.get(env_name) or "").strip() == "1":
            return True
        return False
    # No hospital context: true if any hospital gate is on (backward compat)
    for key in (
        "NTUH_REG_AUTOSUBMIT",
        "CGMH_REG_AUTOSUBMIT",
        "VGHTPE_REG_AUTOSUBMIT",
        "MMH_REG_AUTOSUBMIT",
        "NCKU_REG_AUTOSUBMIT",
        "KMUH_REG_AUTOSUBMIT",
        "VGHTC_REG_AUTOSUBMIT",
        "CMUH_REG_AUTOSUBMIT",
        "FEMH_REG_AUTOSUBMIT",
        "TSGH_REG_AUTOSUBMIT",
        "CSH_REG_AUTOSUBMIT",
        "SHUANG_HO_REG_AUTOSUBMIT",
        "WANFANG_REG_AUTOSUBMIT",
        "TMUH_REG_AUTOSUBMIT",
        "EDAH_REG_AUTOSUBMIT",
        "CHENG_HSIN_REG_AUTOSUBMIT",
        "WEISHIN_REG_AUTOSUBMIT",
        "CGH_REG_AUTOSUBMIT",
        "SKH_REG_AUTOSUBMIT",
        "CHIMEI_REG_AUTOSUBMIT",
        "VGHKS_REG_AUTOSUBMIT",
        "KOO_FOUNDATION_REG_AUTOSUBMIT",
        "CYCH_REG_AUTOSUBMIT",
        "LOTUNG_POHAI_REG_AUTOSUBMIT",
        "CCH_REG_AUTOSUBMIT",
        "TYH_REG_AUTOSUBMIT",
        "YUAN_GENERAL_REG_AUTOSUBMIT",
        "TZUCHI_REG_AUTOSUBMIT",
        "TZUCHI_DALIN_REG_AUTOSUBMIT",
        "TZUCHI_HUALIEN_REG_AUTOSUBMIT",
        "TZUCHI_TAIPEI_REG_AUTOSUBMIT",
    ):
        if (os.environ.get(key) or "").strip() == "1":
            return True
    return False


__all__ = [
    "REQUIRED_ENV_VARS",
    "OPTIONAL_ENV_VARS",
    "required_env_vars",
    "optional_env_vars",
    "load_patient_from_env",
    "dry_run_identity",
    "redacted_dry_identity",
    "autosubmit_enabled",
]
