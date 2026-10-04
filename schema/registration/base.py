"""RegistrationAdapter ABC — navigate/fill/submit hospital WebReg forms.

Separate from schedule ``HospitalAdapter``. Public pages only; patient PII via
env/secrets. Submit must be explicitly gated by the concrete adapter (e.g.
``NTUH_REG_AUTOSUBMIT=1``).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


class RegistrationError(Exception):
    """Base registration error."""

    def __init__(self, hospital_id: str, message: str) -> None:
        self.hospital_id = hospital_id
        super().__init__(f"[{hospital_id}] {message}")


class CaptchaRequired(RegistrationError):
    """Image / reCAPTCHA must be solved before submit."""

    def __init__(
        self,
        hospital_id: str,
        message: str,
        *,
        captcha_kind: str = "unknown",
        detail: dict[str, Any] | None = None,
    ) -> None:
        self.captcha_kind = captcha_kind
        self.detail = detail or {}
        super().__init__(hospital_id, message)


class SubmitBlocked(RegistrationError):
    """Submit intentionally skipped (dry-run / missing flag / missing token)."""

    def __init__(
        self,
        hospital_id: str,
        message: str,
        *,
        payload: dict[str, Any] | None = None,
        artifact_paths: list[str] | None = None,
    ) -> None:
        self.payload = payload or {}
        self.artifact_paths = artifact_paths or []
        super().__init__(hospital_id, message)


@dataclass
class PatientFields:
    """Patient identity for WebReg fill — never log full ID."""

    id_type: str = "personID"  # personID|chartNo|other|newBorn
    id_value: str = ""
    birthdate: str = ""  # YYYY-MM-DD (Gregorian)
    name_zh: str = ""
    phone: str = ""
    nation: str = "TWN"

    def birth_ymd(self) -> tuple[str, str, str]:
        parts = (self.birthdate or "").strip().split("-")
        if len(parts) != 3:
            raise ValueError(f"birthdate must be YYYY-MM-DD, got {self.birthdate!r}")
        y, m, d = parts
        return y, m.zfill(2).lstrip("0") and m.zfill(2) or m, d.zfill(2)

    def birth_parts(self) -> tuple[str, str, str]:
        y, m, d = (self.birthdate or "").strip().split("-")
        return y, str(int(m)), str(int(d))

    def redacted(self) -> dict[str, str]:
        idv = self.id_value or ""
        masked = ("*" * max(0, len(idv) - 4)) + idv[-4:] if idv else ""
        return {
            "id_type": self.id_type,
            "id_last4": masked[-4:] if masked else "",
            "birthdate": self.birthdate,
            "name_zh": self.name_zh[:1] + "*" if self.name_zh else "",
            "phone_last4": (self.phone or "")[-4:],
            "nation": self.nation,
        }


@dataclass
class RegistrationSession:
    """In-memory WebReg session state (cookies live on requests.Session)."""

    hospital_id: str
    hosp_code: str
    slot: dict[str, Any]
    reg_form_url: str | None = None
    form_action: str | None = None
    form_fields: dict[str, Any] = field(default_factory=dict)
    captcha: dict[str, Any] = field(default_factory=dict)
    cookies_snapshot: dict[str, str] = field(default_factory=dict)
    html_path: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class RegistrationResult:
    """Outcome of fill / optional submit."""

    status: str  # dry_run_filled|submitted|confirm_page|blocked|error
    message: str
    session: RegistrationSession | None = None
    payload_redacted: dict[str, Any] = field(default_factory=dict)
    artifact_paths: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)


class RegistrationAdapter(ABC):
    """Per-hospital (or family) registration driver."""

    hospital_id: str
    name_zh: str

    @abstractmethod
    def start_session(self, slot: dict[str, Any]) -> RegistrationSession:
        """Open schedule / deep-link path; establish cookies; resolve RegForm URL."""

    @abstractmethod
    def parse_form(self, session: RegistrationSession) -> RegistrationSession:
        """GET RegForm; extract fields, captcha image URL, CSRF tokens."""

    @abstractmethod
    def solve_captcha(self, session: RegistrationSession) -> RegistrationSession:
        """Solve image captcha (ddddocr) and/or attach reCAPTCHA token if available."""

    @abstractmethod
    def fill_patient(
        self, session: RegistrationSession, patient: PatientFields
    ) -> dict[str, Any]:
        """Build POST payload from session form + patient fields (no submit)."""

    @abstractmethod
    def submit(
        self,
        session: RegistrationSession,
        payload: dict[str, Any],
        *,
        autosubmit: bool = False,
    ) -> RegistrationResult:
        """POST when autosubmit else stop at confirm/fill artifacts."""

    def run_dry_or_submit(
        self,
        slot: dict[str, Any],
        patient: PatientFields,
        *,
        autosubmit: bool = False,
    ) -> RegistrationResult:
        """Convenience: start → parse → captcha → fill → submit/stop."""
        sess = self.start_session(slot)
        sess = self.parse_form(sess)
        sess = self.solve_captcha(sess)
        payload = self.fill_patient(sess, patient)
        return self.submit(sess, payload, autosubmit=autosubmit)
