"""Shared redaction for broker diagnostics; no SDK dependency."""
import os
import re
import json

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


def _secret_names() -> set:
    """Normalized key names whose values are never kept, whatever they contain."""
    names = {re.sub(r"[^a-z0-9]", "", key.lower()) for key in _SECRET_FIELDS}
    names.update({"accountid", "accountnumber", "accountno", "appid",
                  "appsecret", "appkey", "accesstoken", "refreshtoken",
                  "name", "fullname", "email", "phone", "address"})
    return names


# RTDB rejects keys with these characters (and control characters).
_BAD_KEY_CHARS = re.compile(r"[.$#\[\]/\x00-\x1f\x7f]")


def trace_clean(value, *, max_nodes: int = 300, max_str: int = 300, max_depth: int = 8):
    """Bounded, redacted, RTDB-key-safe COPY of ``value`` for the private flight recorder.

    Same secret rules as :func:`broker_diagnostic_json` (credential and identity keys
    are replaced, free text goes through :func:`redact_sensitive_text`), but the result
    stays a nested structure so the trace can be read and queried as data. Keys are made
    legal for Realtime Database; ``None`` stays ``None`` (RTDB drops it); non-finite
    numbers become text; nothing is ever modified in place.
    """
    names = _secret_names()
    budget = [max_nodes]

    def safe_key(raw, used):
        text = _BAD_KEY_CHARS.sub("_", redact_sensitive_text(str(raw))[:100]) or "_"
        candidate, n = text, 1
        while candidate in used:
            n += 1
            candidate = f"{text}~{n}"
        return candidate

    def clean(item, depth):
        budget[0] -= 1
        if budget[0] < 0 or depth > max_depth:
            return "<truncated>"
        if isinstance(item, dict):
            result = {}
            for key, val in item.items():
                if budget[0] <= 0:
                    result["_truncated"] = True
                    break
                name = re.sub(r"[^a-z0-9]", "", str(key).lower())
                result[safe_key(key, result)] = (
                    _REDACTED if name in names else clean(val, depth + 1))
            return result
        if isinstance(item, (list, tuple)):
            result = []
            for val in item:
                if budget[0] <= 0:
                    result.append("<truncated>")
                    break
                result.append(clean(val, depth + 1))
            return result
        if item is None or isinstance(item, (bool, int)):
            return item
        if isinstance(item, float):
            return item if item == item and item not in (float("inf"), float("-inf")) else str(item)
        if isinstance(item, (bytes, bytearray)):
            return f"<bytes {len(item)}>"
        text = redact_sensitive_text(item)
        return text if len(text) <= max_str else text[:max_str] + "…"

    return clean(value, 0)


def broker_diagnostic_json(value) -> str:
    """Bounded, redacted JSON for PRIVATE diagnostics, never a public mirror.

    JSON text also preserves unusual broker keys without RTDB key restrictions.
    Do not rely on redaction to make arbitrary broker extensions public-safe.
    """
    secret_names = _secret_names()
    budget = [160]

    def clean(item, depth=0):
        budget[0] -= 1
        if budget[0] < 0 or depth > 10:
            return "<truncated>"
        if isinstance(item, dict):
            result = {}
            for key, val in item.items():
                if budget[0] <= 0:
                    result["_truncated"] = True
                    break
                name = re.sub(r"[^a-z0-9]", "", str(key).lower())
                safe_key = redact_sensitive_text(key)[:100]
                result[safe_key] = (_REDACTED if name in secret_names
                                    else clean(val, depth + 1))
            return result
        if isinstance(item, (list, tuple)):
            result = []
            for val in item:
                if budget[0] <= 0:
                    result.append("<truncated>")
                    break
                result.append(clean(val, depth + 1))
            return result
        if item is None or isinstance(item, bool):
            return item
        return redact_sensitive_text(item)[:500]

    safe = clean(value)
    encoded = json.dumps(safe, ensure_ascii=True, allow_nan=False)
    if len(encoded) > 16384:
        # Valid JSON even for giant unicode values; no broken string slicing.
        return json.dumps({"truncated": True, "preview": encoded[:2000]})
    return encoded
