"""NTUH WebReg registration (代掛號). Patient PII comes only from env."""

from .base import (
    CaptchaRequired,
    PatientFields,
    RegistrationAdapter,
    RegistrationError,
    RegistrationResult,
    RegistrationSession,
    SubmitBlocked,
)
from .patient_env import autosubmit_enabled, load_patient_from_env, required_env_vars
from .registry import (
    NTUH_FAMILY,
    get_registration_adapter,
    supports_registration,
)

__all__ = [
    "CaptchaRequired",
    "PatientFields",
    "RegistrationAdapter",
    "RegistrationError",
    "RegistrationResult",
    "RegistrationSession",
    "SubmitBlocked",
    "autosubmit_enabled",
    "load_patient_from_env",
    "required_env_vars",
    "NTUH_FAMILY",
    "get_registration_adapter",
    "supports_registration",
]
