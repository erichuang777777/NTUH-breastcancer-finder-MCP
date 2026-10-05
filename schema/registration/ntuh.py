"""NTUH family WebReg registration adapter (T0/CH/C0/T2/T3/T4/T7/Y0).

Flow (public pages):
  BranchIndex?vHospCode=XX
    → RegShowBlock / RegDeptInfo
    → RegDeptSchedule?...  (ClinicSlot.source_url / registration_url)
    → button.doctor-tag.avaliable onclick → RegForm?newx=<token>
    → POST RegForm (identity + image captcha + gRecaptcha v3)
    → confirm / success page (or 初診 follow-up — name/phone)

Dry-run (default): navigate + fill + solve image captcha; write HTML/payload
artifacts; do NOT POST unless NTUH_REG_AUTOSUBMIT=1.

reCAPTCHA v3: supply NTUH_RECAPTCHA_TOKEN or leave empty (submit will likely
fail server-side). Browser automation that runs grecaptcha.execute is the
reliable path for live submit.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse, parse_qs

import requests
import urllib3
from bs4 import BeautifulSoup

from .base import (
    CaptchaRequired,
    PatientFields,
    RegistrationAdapter,
    RegistrationError,
    RegistrationResult,
    RegistrationSession,
    SubmitBlocked,
)
from .captcha_ddddocr import solve_image_captcha
from .patient_env import autosubmit_enabled

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

log = logging.getLogger(__name__)

TAIPEI = timezone(timedelta(hours=8))
USER_AGENT = "BreastCareResearchBot/0.1 (+proxy registration dry-run; polite 1req/s)"
REG_BASE = "https://reg.ntuh.gov.tw/WebReg/WebReg/"

# hospital_id → default WebReg vHospCode
NTUH_REG_HOSP_CODES: dict[str, str] = {
    "ntuh": "T0",            # 總院
    "ntuh_children": "CH",   # 兒童醫院
    "ntuh_cancer": "C0",     # 癌醫
    "ntuh_beihu": "T2",      # 北護分院
    "ntuh_jinshan": "T3",    # 金山分院
    "ntuh_hsinchu": "T7",    # 生醫／竹北（含竹東）；T4 新竹醫院 via slot notes
    "ntuh_yunlin": "Y0",     # 雲林（斗六／虎尾）
    # legacy OpenOnco ids
    "nhia_0401020013": "C0",
    "nhia_0412040012": "T4",
    "nhia_0439010518": "Y0",
    "h_2f3e46a806": "Y0",
}

# Prefer these schedule URLs when slot lacks a RegForm deep link
DEFAULT_SCHEDULE_URLS: dict[str, str] = {
    "C0": (
        f"{REG_BASE}RegDeptSchedule?vhospCode=C0"
        f"&showBlock=H&vDeptCode=KBRC&realSubDeptCode="
    ),
    "T7": (
        f"{REG_BASE}RegDeptSchedule?vhospCode=T7"
        f"&showBlock=E&vDeptCode=KBRV&realSubDeptCode="
    ),
    "T4": (
        f"{REG_BASE}RegDeptSchedule?vhospCode=T4"
        f"&showBlock=B&vDeptCode=SURG&realSubDeptCode="
    ),
    "T0": f"{REG_BASE}BranchIndex?vHospCode=T0",
    "CH": f"{REG_BASE}BranchIndex?vHospCode=CH",
    "T2": f"{REG_BASE}BranchIndex?vHospCode=T2",
    "T3": f"{REG_BASE}BranchIndex?vHospCode=T3",
    "Y0": f"{REG_BASE}BranchIndex?vHospCode=Y0",
}

_REGFORM_RE = re.compile(
    r"RegForm\?newx=([^'\"\s]+)", re.I
)
_ARTIFACT_DIR = Path(__file__).resolve().parents[2] / "schedules" / "raw" / "ntuh_reg_dryrun"


def _now_tag() -> str:
    return datetime.now(TAIPEI).strftime("%Y%m%d_%H%M%S")


def infer_hosp_code(slot: dict[str, Any], hospital_id: str) -> str:
    notes = slot.get("notes") or ""
    url = (slot.get("registration_url") or slot.get("source_url") or "")
    # Prefer campus encoded in deep link
    for code in ("C0", "T7", "T4", "T0", "CH", "Y0"):
        if f"vHospCode={code}" in url or f"vhospCode={code}" in url:
            return code
    env = (os.environ.get("NTUH_REG_HOSP_CODE") or "").strip().upper()
    if env:
        return env
    if "兒童" in notes:
        return "CH"
    if "雲林" in notes or "斗六" in notes or "虎尾" in notes or "Y0" in notes:
        return "Y0"
    if "竹東" in notes or "竹北" in notes or "生醫" in notes or "T7" in notes:
        return "T7"
    if "新竹醫院" in notes or "T4" in notes:
        return "T4"
    if "癌醫" in notes or "C0" in notes:
        return "C0"
    return NTUH_REG_HOSP_CODES.get(hospital_id, "T0")


_ROC_DATE_RE = re.compile(r"(1\d{2})\.(\d{1,2})\.(\d{1,2})")
_BOOKING_CODE_RE = re.compile(r"預約碼\s*[:：]\s*([0-9A-Za-z]+)")


def _roc_to_iso(text: str) -> str | None:
    m = _ROC_DATE_RE.search(text or "")
    if not m:
        return None
    y = int(m.group(1)) + 1911
    return f"{y:04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"


def _modal_for_button(btn) -> Any:
    parent = btn.parent
    if parent is None:
        return None
    sib = parent.find_next_sibling("div")
    if sib and "modal" in (sib.get("class") or []):
        return sib
    return None


def _button_meta(btn) -> dict[str, str]:
    """Date / session / 預約碼 from the clinic modal next to a doctor-tag."""
    modal = _modal_for_button(btn)
    text = modal.get_text("\n", strip=True) if modal else ""
    meta: dict[str, str] = {}
    iso = _roc_to_iso(text)
    if iso:
        meta["clinic_date"] = iso
    for sess in ("上午", "下午", "夜間"):
        if f"{sess}門診" in text:
            meta["session"] = sess
            break
    m = _BOOKING_CODE_RE.search(text)
    if m:
        meta["booking_code"] = m.group(1)
    return meta


def extract_regform_hrefs(html: str) -> list[tuple[str, str, dict[str, str]]]:
    """Return [(button_label, relative_RegForm_href, meta), ...] for bookable tags.

    ``meta`` may include clinic_date (YYYY-MM-DD), session, and booking_code
    (預約碼) taken from the adjacent clinic modal.
    """
    soup = BeautifulSoup(html or "", "html.parser")
    out: list[tuple[str, str, dict[str, str]]] = []
    for btn in soup.select("button.doctor-tag"):
        oc = btn.get("onclick") or ""
        m = _REGFORM_RE.search(oc)
        if not m:
            continue
        label = btn.get_text(" ", strip=True)
        out.append((label, f"RegForm?newx={m.group(1)}", _button_meta(btn)))
    return out


def match_regform_for_slot(
    hrefs: list[tuple], slot: dict[str, Any]
) -> str | None:
    """Pick RegForm href matching slot doctor, date, session, and clinic code.

    If ``name_zh`` is set, require the doctor name to appear on a bookable
    button label — never silently fall back to another physician's RegForm.
    When the slot has a clinic date and the page modals expose dates, require
    that date. Same for 預約碼 (``local_clinic_code``) when both sides have one.
    """
    name = (slot.get("name_zh") or "").strip()
    clinic = (slot.get("local_clinic_code") or "").strip()
    session = (slot.get("session") or "").strip()
    want_date = (slot.get("clinic_date") or "")[:10]
    parsed: list[tuple[str, str, dict[str, str]]] = []
    for item in hrefs:
        label = item[0]
        href = item[1]
        meta = item[2] if len(item) > 2 and isinstance(item[2], dict) else {}
        parsed.append((label, href, meta))
    page_has_dates = any(m.get("clinic_date") for _, _, m in parsed)
    page_has_codes = any(m.get("booking_code") for _, _, m in parsed)
    scored: list[tuple[int, str]] = []
    for label, href, meta in parsed:
        if name and name not in label:
            continue
        if want_date and page_has_dates and meta.get("clinic_date") and meta["clinic_date"] != want_date:
            continue
        if (
            clinic
            and page_has_codes
            and clinic.isdigit()
            and meta.get("booking_code")
            and meta["booking_code"] != clinic
        ):
            continue
        if session and meta.get("session") and meta["session"] != session:
            continue
        score = 10 if name else 0
        if clinic and (clinic == meta.get("booking_code") or clinic in label):
            score += 4
        if session and (meta.get("session") == session or session in label):
            score += 2
        if want_date and meta.get("clinic_date") == want_date:
            score += 5
        scored.append((score, href))
    if name and not scored:
        return None
    if not scored:
        return parsed[0][1] if parsed else None
    scored.sort(key=lambda x: -x[0])
    return scored[0][1]


class NtuhRegistrationAdapter(RegistrationAdapter):
    """Working path for NTUH C0 (癌醫) / T7 (竹北) RegForm fill + gated submit."""

    hospital_id = "ntuh_cancer"
    name_zh = "臺大醫院網掛（家族）"

    def __init__(
        self,
        *,
        hospital_id: str | None = None,
        session: requests.Session | None = None,
        artifact_dir: Path | None = None,
        min_interval_sec: float = 1.2,
    ) -> None:
        if hospital_id:
            self.hospital_id = hospital_id
        self._session = session or requests.Session()
        self._session.headers.setdefault("User-Agent", USER_AGENT)
        self._session.headers.setdefault(
            "Accept-Language", "zh-TW,zh;q=0.9,en;q=0.5"
        )
        self._last_request_at = 0.0
        self.min_interval_sec = min_interval_sec
        self.artifact_dir = Path(artifact_dir or _ARTIFACT_DIR)
        self.artifact_dir.mkdir(parents=True, exist_ok=True)
        self._tls_insecure = False

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_request_at
        wait = self.min_interval_sec - elapsed
        if wait > 0:
            time.sleep(wait)

    def _request(
        self, method: str, url: str, **kwargs: Any
    ) -> requests.Response:
        self._throttle()
        kwargs.setdefault("timeout", 60)
        verify = kwargs.pop("verify", True)
        try:
            try:
                resp = self._session.request(method, url, verify=verify, **kwargs)
            except requests.exceptions.SSLError:
                log.warning("NTUH TLS verify failed; retry verify=False")
                self._tls_insecure = True
                self._throttle()
                resp = self._session.request(
                    method, url, verify=False, **kwargs
                )
        except requests.RequestException as e:
            raise RegistrationError(self.hospital_id, f"{method} {url}: {e}") from e
        finally:
            self._last_request_at = time.monotonic()
        return resp

    def start_session(self, slot: dict[str, Any]) -> RegistrationSession:
        if not slot:
            raise RegistrationError(self.hospital_id, "slot dict required")
        hosp = infer_hosp_code(slot, self.hospital_id)
        def _pick_schedule_url() -> str:
            candidates = [
                slot.get("registration_url"),
                slot.get("source_url"),
                DEFAULT_SCHEDULE_URLS.get(hosp),
                f"{REG_BASE}BranchIndex?vHospCode={hosp}",
            ]
            # Prefer RegDeptSchedule deep links over BranchIndex hubs
            for u in candidates:
                if u and "RegDeptSchedule" in u:
                    return u
            for u in candidates:
                if u:
                    return u
            return f"{REG_BASE}BranchIndex?vHospCode={hosp}"

        schedule_url = _pick_schedule_url()
        # Direct RegForm deep link already?
        if "RegForm?newx=" in (schedule_url or ""):
            reg_url = schedule_url
            sess = RegistrationSession(
                hospital_id=self.hospital_id,
                hosp_code=hosp,
                slot=slot,
                reg_form_url=reg_url
                if reg_url.startswith("http")
                else urljoin(REG_BASE, reg_url),
                meta={"entry": "direct_regform", "schedule_url": schedule_url},
            )
            # warm cookies
            self._request("GET", f"{REG_BASE}BranchIndex?vHospCode={hosp}")
            sess.cookies_snapshot = self._session.cookies.get_dict()
            return sess

        resp = self._request("GET", schedule_url)
        if resp.status_code >= 400:
            raise RegistrationError(
                self.hospital_id,
                f"schedule HTTP {resp.status_code} for {schedule_url}",
            )
        hrefs = extract_regform_hrefs(resp.text)
        if not hrefs:
            # Maybe landed on BranchIndex — try default dept schedule
            fallback = DEFAULT_SCHEDULE_URLS.get(hosp)
            if fallback and fallback != schedule_url:
                resp = self._request("GET", fallback)
                schedule_url = fallback
                hrefs = extract_regform_hrefs(resp.text)
        if not hrefs:
            raise RegistrationError(
                self.hospital_id,
                f"no bookable RegForm?newx= links on {schedule_url} "
                f"(status may be 額滿/停診 or outside open window)",
            )
        rel = match_regform_for_slot(hrefs, slot)
        if not rel:
            names = sorted({lab.split("|")[0].strip() for lab, _ in hrefs})[:20]
            raise RegistrationError(
                self.hospital_id,
                "could not match RegForm link for slot "
                f"name_zh={slot.get('name_zh')!r}; bookable doctors sample={names}",
            )
        reg_url = urljoin(REG_BASE, rel)
        tag = _now_tag()
        html_path = self.artifact_dir / f"{hosp}_schedule_{tag}.html"
        html_path.write_text(resp.text, encoding="utf-8")
        sess = RegistrationSession(
            hospital_id=self.hospital_id,
            hosp_code=hosp,
            slot=slot,
            reg_form_url=reg_url,
            cookies_snapshot=self._session.cookies.get_dict(),
            html_path=str(html_path),
            meta={
                "entry": "reg_dept_schedule",
                "schedule_url": schedule_url,
                "bookable_count": len(hrefs),
                "matched_rel": rel[:80] + "...",
                "tls_insecure_fallback": self._tls_insecure,
            },
        )
        return sess

    def parse_form(self, session: RegistrationSession) -> RegistrationSession:
        if not session.reg_form_url:
            raise RegistrationError(self.hospital_id, "reg_form_url missing")
        resp = self._request("GET", session.reg_form_url)
        if resp.status_code >= 400:
            raise RegistrationError(
                self.hospital_id,
                f"RegForm HTTP {resp.status_code}",
            )
        tag = _now_tag()
        html_path = self.artifact_dir / f"{session.hosp_code}_regform_{tag}.html"
        html_path.write_text(resp.text, encoding="utf-8")
        soup = BeautifulSoup(resp.text, "html.parser")
        form = soup.find("form", id="RegForm")
        if not form:
            raise RegistrationError(
                self.hospital_id, "RegForm#RegForm not found (session expired?)"
            )
        fields: dict[str, Any] = {}
        for inp in form.find_all(["input", "select", "textarea"]):
            name = inp.get("name")
            if not name:
                continue
            itype = (inp.get("type") or "").lower()
            if itype == "radio":
                fields.setdefault(name, [])
                if isinstance(fields[name], list):
                    fields[name].append(
                        {
                            "value": inp.get("value"),
                            "id": inp.get("id"),
                            "checked": inp.has_attr("checked"),
                        }
                    )
                continue
            if itype == "checkbox":
                fields[name] = inp.get("value") if inp.has_attr("checked") else None
                continue
            fields[name] = inp.get("value") or ""
            if inp.name == "select":
                sel = inp.find("option", selected=True) or inp.find("option")
                fields[name] = sel.get("value") if sel else ""

        # CSRF may appear as name __RequestVerificationToken without being in fields map twice
        token_el = form.find("input", {"name": "__RequestVerificationToken"})
        if token_el:
            fields["__RequestVerificationToken"] = token_el.get("value") or ""

        captcha_img = form.find("img", src=re.compile(r"ValidNumerImage", re.I))
        captcha_src = None
        if captcha_img and captcha_img.get("src"):
            captcha_src = urljoin(resp.url, captcha_img["src"])

        # reCAPTCHA site key from page
        site_key = None
        m = re.search(
            r"grecaptcha\.execute\(\s*'([^']+)'",
            resp.text,
        )
        if m:
            site_key = m.group(1)

        session.form_action = urljoin(resp.url, form.get("action") or "RegForm")
        session.form_fields = fields
        session.captcha = {
            "image_src": captcha_src,
            "recaptcha_site_key": site_key,
            "recaptcha_action": "submit",
            "has_image": bool(captcha_src),
            "has_recaptcha_v3": bool(site_key),
        }
        session.cookies_snapshot = self._session.cookies.get_dict()
        session.html_path = str(html_path)
        session.meta["reg_form_final_url"] = resp.url
        session.meta["clinic_hint"] = fields.get("clinicHint")
        return session

    def solve_captcha(self, session: RegistrationSession) -> RegistrationSession:
        src = (session.captcha or {}).get("image_src")
        if not src:
            raise CaptchaRequired(
                self.hospital_id,
                "ValidNumerImage not found on RegForm",
                captcha_kind="image",
            )
        resp = self._request("GET", src)
        if resp.status_code >= 400 or not resp.content:
            raise CaptchaRequired(
                self.hospital_id,
                f"captcha image HTTP {resp.status_code}",
                captcha_kind="image",
            )
        tag = _now_tag()
        img_path = self.artifact_dir / f"{session.hosp_code}_captcha_{tag}.png"
        img_path.write_bytes(resp.content)
        guess = solve_image_captcha(resp.content)
        session.captcha["guess"] = guess
        session.captcha["image_path"] = str(img_path)
        # Optional pre-minted reCAPTCHA token, else try Playwright when autosubmit
        token = (os.environ.get("NTUH_RECAPTCHA_TOKEN") or "").strip()
        mint_meta: dict[str, Any] = {"source": "env" if token else None}
        if not token and autosubmit_enabled(self.hospital_id):
            # Only attempt browser mint when live submit is gated on; dry-run skips
            try:
                from .recaptcha_playwright import try_mint_recaptcha_token

                page_url = session.reg_form_url or ""
                site_key = (session.captcha or {}).get("recaptcha_site_key")
                minted = try_mint_recaptcha_token(
                    page_url,
                    site_key=site_key,
                    action=(session.captcha or {}).get("recaptcha_action") or "submit",
                )
                mint_meta = {
                    "source": "playwright" if minted.get("ok") else "playwright_failed",
                    "error": minted.get("error"),
                    "token_len": minted.get("token_len") or 0,
                }
                if minted.get("ok") and minted.get("token"):
                    token = minted["token"]
                    # Also stash in env for this process so fill_patient / retries see it
                    os.environ["NTUH_RECAPTCHA_TOKEN"] = token
            except Exception as e:  # noqa: BLE001 — soft-fail; submit gate will block
                mint_meta = {"source": "playwright_error", "error": str(e)}
                log.warning("reCAPTCHA mint skipped: %s", e)
        session.captcha["recaptcha_token_present"] = bool(token)
        session.captcha["recaptcha_token"] = token  # kept in memory only
        session.captcha["recaptcha_mint"] = {
            k: v for k, v in mint_meta.items() if k != "token"
        }
        return session

    def fill_patient(
        self, session: RegistrationSession, patient: PatientFields
    ) -> dict[str, Any]:
        fields = session.form_fields or {}
        try:
            y, m, d = patient.birth_parts()
        except Exception as e:
            raise RegistrationError(
                self.hospital_id, f"bad birthdate: {e}"
            ) from e
        guess = (session.captcha or {}).get("guess") or ""
        if not guess:
            raise CaptchaRequired(
                self.hospital_id,
                "image captcha not solved",
                captcha_kind="image",
            )
        payload: dict[str, Any] = {
            "__RequestVerificationToken": fields.get(
                "__RequestVerificationToken", ""
            ),
            "clinicHint": fields.get("clinicHint", ""),
            "urlNewx": fields.get("urlNewx", ""),
            "radInputType": patient.id_type,
            "usrNationValue": patient.nation or fields.get("usrNationValue") or "TWN",
            "txtInputID": patient.id_value,
            "vHospCode": fields.get("vHospCode") or session.hosp_code,
            "txtBirthday_Year": y,
            "txtBirthday_Month": m,
            "txtBirthday_Day": d,
            "txtVerifyCode": guess,
            "gRecaptchaToken": (session.captcha or {}).get("recaptcha_token") or "",
            "btnAction": "formSubmit",
        }
        # Stash redacted copy on session
        session.meta["patient_redacted"] = patient.redacted()
        session.meta["payload_keys"] = sorted(payload.keys())
        return payload

    def submit(
        self,
        session: RegistrationSession,
        payload: dict[str, Any],
        *,
        autosubmit: bool = False,
    ) -> RegistrationResult:
        tag = _now_tag()
        # Persist redacted payload for review
        redacted = {
            k: ("***" if k in ("txtInputID", "gRecaptchaToken") else v)
            for k, v in payload.items()
        }
        if "txtInputID" in payload:
            idv = str(payload["txtInputID"])
            redacted["txtInputID"] = ("*" * max(0, len(idv) - 4)) + idv[-4:]
        if payload.get("gRecaptchaToken"):
            redacted["gRecaptchaToken"] = f"<present len={len(payload['gRecaptchaToken'])}>"
        else:
            redacted["gRecaptchaToken"] = "<empty>"
        payload_path = (
            self.artifact_dir / f"{session.hosp_code}_payload_{tag}.json"
        )
        payload_path.write_text(
            json.dumps(
                {
                    "redacted_payload": redacted,
                    "session_meta": {
                        k: v
                        for k, v in (session.meta or {}).items()
                        if k != "recaptcha_token"
                    },
                    "captcha": {
                        k: v
                        for k, v in (session.captcha or {}).items()
                        if k != "recaptcha_token"
                    },
                    "slot": {
                        k: session.slot.get(k)
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
                        )
                    },
                    "reg_form_url": session.reg_form_url,
                    "form_action": session.form_action,
                    "cookies_names": sorted(session.cookies_snapshot.keys()),
                    "autosubmit_requested": bool(autosubmit),
                    "autosubmit_env": autosubmit_enabled(self.hospital_id),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        artifacts = [
            p
            for p in (
                session.html_path,
                (session.captcha or {}).get("image_path"),
                str(payload_path),
            )
            if p
        ]

        allow = bool(autosubmit) and autosubmit_enabled(self.hospital_id)
        if not allow:
            return RegistrationResult(
                status="dry_run_filled",
                message=(
                    "Stopped before POST. Set NTUH_REG_AUTOSUBMIT=1 and pass "
                    "autosubmit=True to submit. Artifacts saved for review. "
                    "reCAPTCHA v3 token "
                    + (
                        "present via NTUH_RECAPTCHA_TOKEN."
                        if payload.get("gRecaptchaToken")
                        else "MISSING — live submit needs browser grecaptcha.execute "
                        "or NTUH_RECAPTCHA_TOKEN."
                    )
                ),
                session=session,
                payload_redacted=redacted,
                artifact_paths=artifacts,
            )

        if not payload.get("gRecaptchaToken"):
            raise CaptchaRequired(
                self.hospital_id,
                "NTUH_REG_AUTOSUBMIT=1 but gRecaptchaToken empty — set "
                "NTUH_RECAPTCHA_TOKEN or run browser helper",
                captcha_kind="recaptcha_v3",
                detail={"site_key": (session.captcha or {}).get("recaptcha_site_key")},
            )

        action = session.form_action or urljoin(REG_BASE, "RegForm")
        resp = self._request(
            "POST",
            action,
            data=payload,
            headers={"Referer": session.reg_form_url or action},
        )
        out_html = (
            self.artifact_dir / f"{session.hosp_code}_submit_result_{tag}.html"
        )
        out_html.write_text(resp.text, encoding="utf-8")
        artifacts.append(str(out_html))
        text = BeautifulSoup(resp.text, "html.parser").get_text(" ", strip=True)
        status = "submitted"
        msg = f"POST RegForm → HTTP {resp.status_code}"
        if any(k in text for k in ("掛號成功", "您的掛號", "看診號碼", "預約成功")):
            status = "confirm_page"
            msg = "Likely success / confirm page — inspect artifact HTML"
        elif any(k in text for k in ("驗證碼", "錯誤", "失敗", "重新")):
            status = "error"
            msg = "Response suggests captcha/validation error — inspect HTML"
        return RegistrationResult(
            status=status,
            message=msg,
            session=session,
            payload_redacted=redacted,
            artifact_paths=artifacts,
            raw={"http_status": resp.status_code, "final_url": resp.url},
        )


__all__ = [
    "NtuhRegistrationAdapter",
    "NTUH_REG_HOSP_CODES",
    "DEFAULT_SCHEDULE_URLS",
    "extract_regform_hrefs",
    "match_regform_for_slot",
    "infer_hosp_code",
]
