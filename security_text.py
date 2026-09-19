"""Shared redaction for broker diagnostics; no SDK dependency."""
import os
import re

_SECRET_FIELDS = (
    "x-signature", "signature", "x-access-token", "x-app-key", "app_secret",
    "app_key_secret", "access_token", "account_id", "webull_account_id",
    "authorization", "x-app-secret", "app_key", "token", "refresh_token",
    "account_number", "account_no",
)
_SECRET_PATTERN = re.compile(
    r"(?P<label>%s)(?P<sep>['\"]?(?:\s*[:=]\s*|%%3A|%%3D)"
    r"(?:['\"]|%%22|%%27)?)(?P<value>[^\"',\s}\]&]+)"
    % "|".join(re.escape(f) for f in _SECRET_FIELDS), re.IGNORECASE)
_REDACTED = "<redacted>"


def redact_sensitive_text(value) -> str:
    """Remove credentials/account identity from exceptions and persisted text."""
    text = str(value)
    # Redact the complete value before the generic field matcher can consume
    # only "Bearer" and leave the credential behind.
    text = re.sub(r"(?i)\b(Bearer|Basic)(?:\s+|%20)[^\s\"',}\]&]+",
                  lambda m: m.group(1) + " " + _REDACTED, text)
    text = _SECRET_PATTERN.sub(
        lambda m: f"{m.group('label')}{m.group('sep')}{_REDACTED}", text)
    for name in (
        "WEBULL_APP_KEY",
        "WEBULL_APP_SECRET",
        "WEBULL_ACCOUNT_ID",
    ):
        secret = os.environ.get(name, "")
        if len(secret) >= 4:
            text = text.replace(secret, _REDACTED)
    return text
