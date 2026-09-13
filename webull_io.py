"""Webull OpenAPI adapter: fail-closed parsing, cached clients, token upkeep."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import posixpath
import re
import sys
import tempfile
import time
import uuid
import tick_runtime
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from datetime import datetime, timezone

from lego_one_row import Config
from lego_orders import PROD, UAT

logger = logging.getLogger(__name__)

UAT_ENDPOINT = "th-api.uat.webullbroker.com"
PROD_ENDPOINT = "api.webull.co.th"
DEFAULT_TOKEN_DIR = "/tmp/webull_token"
TOKEN_FILE = "token.txt"

# 408/429 belong here as much as any 5xx, and are likelier: every call this
# adapter makes sits under a small per-endpoint Webull limit (account 10/30s,
# market data 60/60s, order query 40/2s). Treating a throttled read as a hard
# failure turned a two-second wait into a missed slot.
_TRANSIENT_HTTP = {408, 429, 500, 502, 503, 504}
_TRANSIENT_CODES = {
    "GATEWAY_TIMEOUT", "TIMEOUT", "SERVICE_UNAVAILABLE",
    "RATE_LIMIT", "RATE_LIMIT_EXCEEDED", "TOO_MANY_REQUESTS",
    "REQUEST_LIMIT_EXCEEDED", "FREQUENCY_LIMIT",
    # Pinned SDK ClientException codes for transport failures. These are only
    # consumed by the read/preview wrapper; place_order deliberately bypasses
    # retries because a timeout cannot prove whether money moved.
    "SDK.HTTPERROR", "SDK.UNKNOWNSERVERERROR", "SDK.ENDPOINTRESOLVINGERROR",
}
# Any of these present-and-truthy means the broker rejected the preview.
_PREVIEW_ERROR_KEYS = ("error", "error_code", "errorCode")
# webull/data/common/category.py of the pinned SDK 2.0.15.
CATEGORIES = ("US_STOCK", "US_ETF", "US_OPTION", "US_CRYPTO", "US_FUTURES",
              "US_EVENT", "HK_STOCK", "HK_ETF", "HK_FUTURES", "CN_STOCK")
# The order payload below is deliberately US EQUITY. Accepting every market
# data enum here could price one instrument family and submit it as another.
EXECUTION_CATEGORIES = ("US_STOCK", "US_ETF")


class WebullConfigError(ValueError):
    """A deployment value is unsafe or unsupported."""


class IncompleteOpenOrdersError(RuntimeError):
    """The broker's open-order pagination could not prove the scan complete."""


class MarketDataForbidden(RuntimeError):
    """403 from a market-data call: an entitlement problem, not a bad symbol.

    OpenAPI market data is a separate subscription from the one in the Webull
    app, so this is the one error here that no amount of retrying or code fixing
    resolves. It says so instead of arriving as a bare 403.
    """


@dataclass(frozen=True)
class InstrumentCapability:
    symbol: str
    status: str
    category: str
    currency: str
    lot_size: Decimal
    fractionable: bool

    @property
    def quantity_increment(self) -> Decimal:
        # The current profile contract exposes lot_size but no independent
        # fractional increment. Using the documented lot is conservative and
        # never invents support the broker did not advertise.
        return self.lot_size


def parse_instrument_capability(payload, symbol: str) -> InstrumentCapability:
    items = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        raise WebullConfigError("instrument profile ไม่มี data list")
    wanted = symbol.strip().upper()
    matches = [x for x in items if isinstance(x, dict)
               and str(x.get("symbol", "")).upper() == wanted]
    if len(matches) != 1:
        raise WebullConfigError(
            f"instrument profile ต้องพบ {wanted} หนึ่งรายการ; พบ {len(matches)}")
    item = matches[0]
    if str(item.get("status", "")).upper() != "OC":
        raise WebullConfigError(f"{wanted} ไม่อยู่สถานะ tradable (OC)")
    if str(item.get("currency", "")).upper() != "USD":
        raise WebullConfigError(f"{wanted} ไม่ใช่ USD instrument")
    try:
        lot = Decimal(str(item["lot_size"]))
    except (KeyError, InvalidOperation) as exc:
        raise WebullConfigError("instrument lot_size ไม่ถูกต้อง") from exc
    if not lot.is_finite() or lot <= 0:
        raise WebullConfigError("instrument lot_size ต้อง finite และ > 0")
    category = str(item.get("category", "")).upper()
    if category != "US_STOCK":
        raise WebullConfigError(f"instrument category {category!r} ไม่รองรับ")
    return InstrumentCapability(
        symbol=wanted,
        status="OC",
        category=category,
        currency="USD",
        lot_size=lot,
        fractionable=item.get("fractionable") is True,
    )


def fetch_instrument_capability(trade_client, symbol: str) -> InstrumentCapability:
    """Fetch the current v3 stock profile through the pinned SDK signer.

    SDK 2.0.15 does not expose the new profile method, but its ApiRequest and
    signer are still used; this avoids a second signature implementation.
    """
    from webull.core.request import ApiRequest

    request = ApiRequest(
        "/trading/instruments/stocks/profiles/list",
        version="v3", method="GET", query_params={})
    request.add_query_param("category", "US_STOCK")
    request.add_query_param("symbols", symbol.strip().upper())
    api_client = trade_client.trade_instrument.client
    response = _retry_transient(lambda: api_client.get_response(request).json())
    return parse_instrument_capability(response, symbol)


def _http_status(exc: Exception) -> int | None:
    status = getattr(exc, "http_status", None)
    if status is None:
        status = getattr(exc, "status_code", None)
    try:
        return int(status) if status is not None else None
    except (TypeError, ValueError):
        return None


def is_transient_exception(exc: Exception) -> bool:
    code = str(getattr(exc, "error_code", "") or "").upper()
    return _http_status(exc) in _TRANSIENT_HTTP or code in _TRANSIENT_CODES


def _retry_transient(fn, attempts: int = 3, base_delay: float = 2.0):
    last = None
    for i in range(attempts):
        tick_runtime.require_budget()
        try:
            return fn()
        except Exception as exc:
            if not is_transient_exception(exc):
                raise
            last = exc
            if i < attempts - 1:
                delay = base_delay * (2 ** i)
                tick_runtime.require_budget(delay + 2.0)
                time.sleep(delay)
    raise last


def load_config() -> Config:
    return Config(
        symbol=os.environ["LEGO_SYMBOL"],
        fix_c=float(os.environ["LEGO_FIX_C"]),
        diff=float(os.environ.get("LEGO_DIFF", "0")),
        dna_code=os.environ.get("LEGO_DNA_CODE", "bypass:100"),
        strategy_id=os.environ.get("LEGO_STRATEGY_ID", "shannon_demon_lego"),
        decimal_precision=int(os.environ.get("LEGO_DECIMAL_PRECISION", "5")),
    )


def market_category() -> str:
    """The Category the snapshot is requested under.

    Hard-coding US_STOCK priced every symbol as a stock. The strategy is usually
    run on leveraged ETFs, and category is a query parameter of the snapshot
    endpoint, so the wrong one is answered by the broker, not by this code.
    Default is unchanged; an unknown value fails closed rather than reaching the
    API as a typo.
    """
    value = os.environ.get("LEGO_MARKET_CATEGORY", "US_STOCK").strip().upper()
    if value not in CATEGORIES:
        raise WebullConfigError(
            f"LEGO_MARKET_CATEGORY={value!r} ไม่อยู่ใน Category ของ SDK — fail closed "
            f"(ค่าที่ใช้ได้: {', '.join(CATEGORIES)})")
    if value not in EXECUTION_CATEGORIES:
        raise WebullConfigError(
            f"LEGO_MARKET_CATEGORY={value!r} ยังไม่รองรับใน money path — "
            "order payload ปัจจุบันส่งได้เฉพาะ US EQUITY; fail closed "
            f"(ค่าที่รองรับ: {', '.join(EXECUTION_CATEGORIES)})")
    return value


def environment_label() -> str:
    value = os.environ.get("WEBULL_ENV", "UAT").strip().upper()
    if value == "UAT":
        return UAT
    if value in {"PROD", "PRODUCTION"}:
        return PROD
    raise WebullConfigError(
        f"WEBULL_ENV={value!r} ไม่รองรับ — ใช้ได้เฉพาะ UAT, PROD หรือ PRODUCTION")


def runtime_identity_fingerprint() -> str:
    """Opaque account/environment identity used to guard a persisted chain."""
    account_id = os.environ.get("WEBULL_ACCOUNT_ID", "").strip()
    if not account_id:
        raise WebullConfigError("WEBULL_ACCOUNT_ID ว่างหรือไม่ได้ตั้งค่า")
    raw = f"webull-runtime-v1\0{environment_label()}\0{account_id}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _endpoint() -> str:
    return UAT_ENDPOINT if environment_label() == UAT else PROD_ENDPOINT


def token_dir() -> str:
    return os.environ.get("WEBULL_TOKEN_DIR", DEFAULT_TOKEN_DIR)


def token_file_path() -> str:
    return os.path.join(token_dir(), TOKEN_FILE)


def token_dir_is_ephemeral() -> bool:
    """True when the token cannot survive an instance recycle.

    /tmp is the only writable path on Cloud Functions and also the first thing
    the runtime throws away. The SDK reacts to a missing token by creating a new
    one, which needs a human to approve 2FA in the app within 300 seconds; there
    is nobody to do that on a scheduler, so it ends in ERROR_INIT_TOKEN and the
    bot stops until someone notices.
    """
    raw_dir = token_dir()
    # Deployment configuration is a POSIX path even when validation/tests run
    # on Windows.  Do this check before os.path.abspath can turn "/tmp" into a
    # drive-relative Windows path.
    posix_dir = posixpath.normpath(raw_dir.replace("\\", "/"))
    if posix_dir == "/tmp" or posix_dir.startswith("/tmp/"):
        return True

    # Also recognize the host's real temporary directory without accepting a
    # lexical prefix such as C:\Temp-durable.
    native_dir = os.path.normcase(os.path.abspath(raw_dir))
    native_tmp = os.path.normcase(os.path.abspath(tempfile.gettempdir()))
    try:
        return os.path.commonpath((native_dir, native_tmp)) == native_tmp
    except ValueError:                    # paths live on different Windows drives
        return False


def _expires_datetime(raw) -> datetime | None:
    """The SDK stores `expires` as Unix time; the API documents milliseconds."""
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    if value > 1e11:                        # milliseconds, per the API docs
        value /= 1000.0
    try:
        return datetime.fromtimestamp(value, timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def read_local_token() -> dict | None:
    """The three lines TokenManager writes: token, expires, status."""
    try:
        with open(token_file_path(), "r", encoding="utf-8") as handle:
            token = handle.readline().strip()
            expires = handle.readline().strip()
            status = handle.readline().strip()
    except OSError:
        return None
    if not token:
        return None
    return {"token": token, "expires": expires, "status": status,
            "expires_at": _expires_datetime(expires)}


def _write_local_token(token: str, expires, status: str) -> None:
    path = token_file_path()
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(f"{token}\n{expires}\n{status}\n")


def hydrate_token_from_secret(secret_client=None) -> dict:
    """Materialize the deployment-bound token secret into the SDK cache.

    The secret payload uses the SDK's exact three-line format. It is copied to
    /tmp on cold start; Secret Manager is the durable source, not that file.
    No payload or account identity is ever returned or logged.
    """
    resource = os.environ.get("WEBULL_TOKEN_SECRET", "").strip()
    if not resource:
        return {"configured": False, "hydrated": False}
    if read_local_token() is not None:
        return {"configured": True, "hydrated": False, "already_present": True}
    if not resource.startswith("projects/") or "/secrets/" not in resource:
        raise WebullConfigError(
            "WEBULL_TOKEN_SECRET ต้องเป็น Secret Manager resource name")
    if "/versions/" not in resource:
        resource = f"{resource}/versions/latest"
    if secret_client is None:
        from google.cloud import secretmanager
        secret_client = secretmanager.SecretManagerServiceClient()
    tick_runtime.require_budget()
    options = ({} if tick_runtime.remaining() is None else
               {"timeout": min(5.0, tick_runtime.remaining() - 1.0), "retry": None})
    response = secret_client.access_secret_version(request={"name": resource}, **options)
    payload = bytes(response.payload.data)
    if len(payload) > 4096:
        raise WebullConfigError("token secret ใหญ่ผิดปกติ")
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise WebullConfigError("token secret ไม่ใช่ UTF-8") from exc
    lines = text.splitlines()
    if len(lines) != 3 or not lines[0].strip():
        raise WebullConfigError("token secret ต้องเป็น token/expires/status จำนวน 3 บรรทัด")
    if _expires_datetime(lines[1]) is None or lines[2].strip() != "NORMAL":
        raise WebullConfigError("token secret expiry/status ไม่ถูกต้อง")
    path = token_file_path()
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    temporary = f"{path}.{uuid.uuid4().hex}.tmp"
    try:
        with open(temporary, "x", encoding="utf-8") as handle:
            handle.write(f"{lines[0].strip()}\n{lines[1].strip()}\nNORMAL\n")
        os.replace(temporary, path)
    finally:
        try:
            if os.path.exists(temporary):
                os.unlink(temporary)
        except OSError:
            pass
    return {"configured": True, "hydrated": True, "resource": resource}


def _refresh_margin_days() -> float:
    return float(os.environ.get("LEGO_TOKEN_REFRESH_MARGIN_DAYS", "3"))


def ephemeral_token_dir_accepted() -> bool:
    """True when the operator has signed off on a token dir that does not last.

    Cloud Functions gives the container exactly one writable path, /tmp, so
    `token_dir_is_ephemeral()` is True on every stock deployment. It is a real
    risk — the token dies with the instance and only a human with the app can
    replace it — but it is a risk about the *next* recycle, not a statement that
    the token in hand cannot authenticate right now. Folding it into `ok` made
    the AUTO_SUBMIT gate unsatisfiable on the platform the bot actually runs on:
    every row committed READY_BUY/READY_SELL and not one of them ever became an
    order intent, so the broker position never moved.

    Default False keeps the strict reading. Setting it to true is the operator
    saying 'I know the token is on /tmp and I accept re-doing 2FA after a
    recycle' — the warning is still emitted on every slot either way.
    """
    return os.environ.get("LEGO_ALLOW_EPHEMERAL_TOKEN_DIR", "false").lower() == "true"


def token_health(now: datetime | None = None) -> dict:
    """What the stored token says, without spending a request to ask the broker.

    Purely local so it is free to call on every slot. `ok` is False for anything
    that ends with the bot silently unable to authenticate: no token file, a
    non-NORMAL status, an expiry inside the refresh margin, or a token kept
    somewhere that does not survive the container.

    `ready` answers the narrower question the order gate needs — 'can this token
    sign a request now?' — and so it ignores the durability warning once
    LEGO_ALLOW_EPHEMERAL_TOKEN_DIR accepts it. Every other reason still closes
    both. With the flag unset the two are always equal, which is why `ok`,
    `reasons` and the warning text are byte-for-byte what they were.

    `durability_risk_only` names the third state the first two could not express:
    blocked, but only by reasons about the *next* container — not one word about
    the token in hand. It is what lets a caller holding independent proof that
    this token just signed a request (lego_preflight's `token_proved_live`) tell
    "the token is unusable" apart from "the token works and its directory will
    not survive a recycle". False whenever any reason is about signing now, so it
    can never forgive a missing, rejected or expiring token.

    `live_proof_supersedable` is the wider of the two exemptions and answers a
    different question: is every recorded reason one that *inspecting a local
    file* raised, and that direct evidence of a signed broker request settles
    better? It adds exactly one reason to the durability set — a token file that
    is not there at all — because on this deployment that is not a fault:

        ClientInitializer.init_token() asks the broker whether token checking is
        enabled and, when the answer is no, returns before TokenManager is ever
        constructed. Nothing writes token.txt, nothing ever will, and the SDK
        signs every request with HMAC alone. The UAT app the chain runs under
        answers `_check_token_enable result is False` on every single call, so
        `found` was permanently False, `ready` permanently False, and the
        AUTO_SUBMIT gate permanently shut: rows committed READY_BUY/READY_SELL
        for days, no intent was ever created, and the broker position — the
        `จำนวนถือครอง (หุ้น)` column — never moved.

    The inference the exemption rests on is one-directional and safe. If token
    checking *were* enabled and no usable token existed, TokenManager.init_token
    raises ERROR_INIT_TOKEN inside build_clients(), so the caller never reaches
    the snapshot that earns the proof; and a create/refresh that did succeed
    writes the file. A missing file plus an authenticated broker read therefore
    means the broker is not asking for a token — not that we lost one.

    Deliberately still excluded: an unreadable expiry, a non-NORMAL status, and a
    token inside the refresh margin. Those are the broker's or the file's own
    verdict on the token, and a call that happened to succeed a moment ago does
    not overturn them.
    """
    now = now or datetime.now(timezone.utc)
    info = {
        "token_dir": token_dir(),
        "ephemeral_token_dir": token_dir_is_ephemeral(),
        "found": False,
        "status": None,
        "expires_at": None,
        "days_left": None,
        "ok": True,
        "ready": True,
        "durability_risk_only": False,
        "live_proof_supersedable": False,
        "reasons": [],
    }
    if _token_check_disabled():
        info.update({"authentication_mode": "SIGNED_WITHOUT_TOKEN",
                     "token_check_enabled": False})
        return info
    info["authentication_mode"] = "TOKEN_OR_UNVERIFIED"
    durability_reasons: list[str] = []
    supersedable_reasons: list[str] = []

    def fail(reason: str, *, blocks_now: bool = True,
             durability_only: bool = False,
             live_proof_supersedable: bool = False) -> None:
        """Record a reason.

        `blocks_now` False marks it as already forgiven for `ready`.
        `durability_only` marks it as saying nothing about whether the token in
        hand can sign a request — the two are independent, because the operator
        flag decides the first and the nature of the reason decides the second.
        `live_proof_supersedable` marks it as a local-file finding that direct
        evidence of a signed broker request answers; durability reasons are
        always such a finding, so they never have to say so twice.
        """
        info["ok"] = False
        if blocks_now:
            info["ready"] = False
        if durability_only:
            durability_reasons.append(reason)
        if durability_only or live_proof_supersedable:
            supersedable_reasons.append(reason)
        info["reasons"].append(reason)

    def seal() -> dict:
        info["durability_risk_only"] = bool(info["reasons"]) and (
            len(durability_reasons) == len(info["reasons"]))
        info["live_proof_supersedable"] = bool(info["reasons"]) and (
            len(supersedable_reasons) == len(info["reasons"]))
        return info

    if info["ephemeral_token_dir"]:
        fail(f"token dir {token_dir()} อยู่บน storage ที่หายเมื่อ instance ถูกรีไซเคิล — "
             "ตั้ง WEBULL_TOKEN_DIR ไปยัง volume ที่คงอยู่ (เช่น GCS FUSE mount)",
             blocks_now=not ephemeral_token_dir_accepted(),
             durability_only=True)
    local = read_local_token()
    if local is None:
        fail(f"ไม่พบ token file ที่ {token_file_path()} — ตรวจว่า application ต้องใช้ token หรือไม่",
             live_proof_supersedable=True)
        return seal()
    info["found"] = True
    info["status"] = local["status"] or None
    expires_at = local["expires_at"]
    if expires_at is None:
        fail("token file ไม่มีวันหมดอายุที่อ่านได้")
        return seal()
    days_left = (expires_at - now).total_seconds() / 86400.0
    info["expires_at"] = expires_at.strftime("%Y-%m-%dT%H:%M:%SZ")
    info["days_left"] = round(days_left, 3)
    if local["status"] and local["status"] != "NORMAL":
        fail(f"token status={local['status']} (ต้องเป็น NORMAL)")
    if days_left <= _refresh_margin_days():
        fail(f"token เหลืออีก {days_left:.2f} วันก่อนหมดอายุ")
    return seal()


def ensure_token_fresh(api_client) -> dict:
    """Renew the token before it expires, because nothing in the SDK will.

    /openapi/auth/token/refresh is wrapped by TokenOperation.refresh_token and
    called from nowhere: TokenManager.init_token reads `expires` out of the file
    and discards it. The token simply dies at 15 days, mid-session, and recovery
    needs 2FA that only a human can give. Refreshing while the token is still
    valid keeps that from ever being the failure mode.

    Never raises. A refresh that fails leaves a token that is still good for the
    whole margin, so blocking the slot over it would cause the outage it exists
    to prevent.
    """
    health = token_health()
    out = {"refreshed": False, **health}
    if os.environ.get("WEBULL_TOKEN_SECRET", "").strip():
        # Runtime only reads secret versions. Rotation is an audited admin
        # operation; rotating only /tmp could invalidate the durable token and
        # strand sibling/cold instances on an older credential.
        out["refresh_skipped"] = "Secret Manager token rotates via bootstrap-auth only"
        return out
    if not health["found"] or health["days_left"] is None:
        return out                       # nothing to refresh from
    if health["days_left"] > _refresh_margin_days():
        return out
    if health["ephemeral_token_dir"]:
        # A refresh rotates *the* account token, and on an ephemeral dir every
        # container keeps its own copy of it. Rotating from here would buy
        # nothing — the new token dies with this container too — while a sibling
        # container mid-slot could be left holding the token we just replaced.
        # The health warning already names the fix; refreshing starts working
        # the moment the token lives somewhere durable.
        out["refresh_skipped"] = ("token dir ไม่คงอยู่ข้าม container จึงไม่ refresh "
                                  "(เสี่ยงหมุน token ทิ้งให้ instance อื่นค้าง) — "
                                  "ย้าย WEBULL_TOKEN_DIR ก่อน")
        logger.warning("token refresh skipped: %s", out["refresh_skipped"])
        return out
    local = read_local_token()
    try:
        from webull.core.http.initializer.token.token_operation import TokenOperation

        response = TokenOperation(api_client).refresh_token(local["token"]).json()
    except Exception as exc:             # noqa: BLE001 - see docstring
        # A durable token dir can be shared, so the refresh may have failed
        # because another instance rotated the token first. That token is ours
        # as much as one we fetched: adopt it rather than report a failure.
        adopted = read_local_token()
        if adopted and adopted["token"] != local["token"]:
            api_client.set_token(adopted["token"])
            logger.info("adopted a token refreshed by another instance")
            return {"refreshed": False, "adopted_external_refresh": True,
                    **token_health()}
        out["refresh_error"] = (
            f"{type(exc).__name__}: {redact_sensitive_text(exc)}")
        logger.warning("webull token refresh failed: %s", out["refresh_error"])
        return out
    token = (response or {}).get("token")
    expires = (response or {}).get("expires")
    if not token or not expires:
        out["refresh_error"] = "refresh response ไม่มี token/expires — เก็บ token เดิมไว้"
        logger.warning("webull token refresh returned an unusable payload")
        return out
    status = str((response or {}).get("status") or "NORMAL")
    try:
        _write_local_token(token, expires, status)
    except OSError as exc:
        out["refresh_error"] = f"เขียน token file ไม่ได้: {exc}"
        logger.warning("webull token refresh could not be stored: %s", exc)
        return out
    api_client.set_token(token)
    # Re-read rather than patch the old answer: a refresh fixes the expiry and
    # nothing else, and an ephemeral token dir is still worth saying out loud.
    out = {"refreshed": True, **token_health()}
    logger.info("webull token refreshed, expires_at=%s", out["expires_at"])
    return out


_CLIENTS: tuple | None = None
_AUTH_PROFILE: tuple | None = None


def _client_identity() -> tuple:
    return (_endpoint(), os.environ.get("WEBULL_APP_KEY", ""), token_dir(),
            hashlib.sha256(os.environ.get("WEBULL_APP_SECRET", "").encode()).hexdigest(),
            os.environ.get("WEBULL_ACCOUNT_ID", ""),
            os.environ.get("WEBULL_TOKEN_SECRET", ""))


def _token_check_disabled() -> bool:
    if _AUTH_PROFILE is None:
        return False
    key, verified_at, enabled = _AUTH_PROFILE
    return (enabled is False and key == _client_identity()
            and time.monotonic() - verified_at < _client_cache_ttl())


def _bounded_api_class(base):
    """Use SDK request timeout setters; keep signing and retry policy untouched."""
    class BoundedApiClient(base):
        def get_response(self, request):
            tick_runtime.require_budget()
            left = tick_runtime.remaining()
            if left is not None:
                usable = left - 1.0
                request.set_connect_timeout(min(2.0, usable / 2))
                request.set_read_timeout(min(5.0, usable / 2))
            response = super().get_response(request)
            action = request.get_action_name()
            if action == "/openapi/config" and response.status_code == 200:
                payload = response.json()
                enabled = payload.get("token_check_enabled") if isinstance(payload, dict) else None
                # The pinned SDK defaults a missing flag to False. Do not turn
                # missing/ambiguous config into permission to skip token checks.
                if type(enabled) is not bool:
                    raise WebullConfigError("token_check_enabled must be an explicit boolean")
                self._lego_token_check_enabled = enabled
            if left is not None and "/auth/token/" in action:
                payload = response.json()
                if isinstance(payload, dict) and payload.get("status") == "PENDING":
                    raise WebullConfigError("token requires verification outside the scheduled tick")
            return response
    return BoundedApiClient


def _client_cache_ttl() -> float:
    return float(os.environ.get("LEGO_CLIENT_CACHE_TTL_SECONDS", "3600"))


def reset_clients() -> None:
    """Drop the cached clients so the next call re-authenticates."""
    global _CLIENTS, _AUTH_PROFILE
    _CLIENTS = None
    _AUTH_PROFILE = None


def _sdk_log_level() -> int:
    name = os.environ.get("LEGO_WEBULL_LOG_LEVEL", "INFO").strip().upper()
    return getattr(logging, name, logging.INFO)


_SECRET_FIELDS = (
    "x-signature", "signature", "x-access-token", "x-app-key", "app_secret",
    "app_key_secret", "access_token", "account_id", "webull_account_id",
    "authorization",
)
_SECRET_PATTERN = re.compile(
    r"(?P<label>%s)(?P<sep>['\"]?(?:\s*[:=]\s*|%%3A|%%3D)"
    r"(?:['\"]|%%22|%%27)?)(?P<value>[^\"',\s}\]&]+)"
    % "|".join(re.escape(f) for f in _SECRET_FIELDS), re.IGNORECASE)
_REDACTED = "<redacted>"


def redact_sensitive_text(value) -> str:
    """Remove credentials/account identity from exceptions and persisted text."""
    text = str(value)
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


class _RedactSecrets(logging.Filter):
    """Keep the SDK's error logs from carrying credentials into Cloud Logging.

    On any non-2xx the SDK logs json.dumps(vars(request)), and the signer writes
    the signature straight back onto the request it signed, so that record
    contains x-signature and x-app-key in full. At DEBUG the signature composer
    also logs string_to_sign and the signature on their own. None of it is the
    app secret — that never leaves the signing function — but logging a
    credential of any kind is the one thing the order flow forbids outright.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            text = record.getMessage()
        except Exception:                # a broken format string is not our call
            return True
        redacted = redact_sensitive_text(text)
        if redacted != text:
            record.msg, record.args = redacted, ()
        return True


def _install_redaction(logger_name: str = "webull.core") -> None:
    """Filters run on handlers, not on ancestor loggers, so attach them there."""
    sdk_logger = logging.getLogger(logger_name)
    for handler in sdk_logger.handlers:
        if not any(isinstance(f, _RedactSecrets) for f in handler.filters):
            handler.addFilter(_RedactSecrets())


def _configure_sdk_stream_logger(api) -> None:
    """Install one process-wide SDK stream handler and reuse it on rebuilds."""
    level = _sdk_log_level()
    sdk_logger = logging.getLogger("webull.core")
    managed = [handler for handler in sdk_logger.handlers
               if getattr(handler, "_lego_webull_stream", False)]
    if managed:
        sdk_logger.setLevel(level)
        for handler in managed:
            handler.setLevel(level)
        # TradeClient/DataClient otherwise install their own console *and file*
        # handlers on every newly constructed ApiClient.
        api._stream_logger_set = True
    else:
        before = set(sdk_logger.handlers)
        api.set_stream_logger(
            log_level=level, stream=sys.stdout,
            format_string="%(asctime)s %(name)s %(levelname)s %(message)s")
        for handler in sdk_logger.handlers:
            if handler not in before:
                handler._lego_webull_stream = True
    _install_redaction()


def build_clients():
    """Build (or reuse) the SDK clients for this instance.

    Two things the SDK does at construction time make a fresh pair per
    invocation the wrong default:

    * TradeClient and DataClient each run ClientInitializer.initializer on the
      same ApiClient, and each run is a config call plus a create_token call. One
      build_clients() is therefore four auth requests, and token create is capped
      at 10 per 30 seconds — with the order worker also building clients, a busy
      slot can spend the whole budget on setup.
    * With no logger configured they install a TimedRotatingFileHandler writing
      into the working directory. On Cloud Functions everything outside /tmp is
      read-only, so that is an OSError at construction; where it does succeed it
      adds another pair of handlers to the shared 'webull.core' logger on every
      call. Setting a stream logger first makes the SDK skip that branch and
      sends the same records to stdout, which is what Cloud Logging reads.

    Warm instances therefore reuse one authenticated pair, and the TTL forces a
    rebuild often enough that a long-lived instance never runs on a token whose
    state it has stopped checking.
    """
    global _CLIENTS, _AUTH_PROFILE
    cache_key = _client_identity()
    if _CLIENTS is not None:
        key, built_at, trade, data = _CLIENTS
        if key == cache_key and (time.monotonic() - built_at) < _client_cache_ttl():
            return trade, data

    from webull.core.client import ApiClient
    from webull.trade.trade_client import TradeClient
    from webull.data.data_client import DataClient

    hydrate_token_from_secret()
    api = _bounded_api_class(ApiClient)(
        os.environ["WEBULL_APP_KEY"], os.environ["WEBULL_APP_SECRET"], "th")
    api.add_endpoint("th", _endpoint())
    api.set_token_dir(token_dir())
    _configure_sdk_stream_logger(api)
    trade, data = TradeClient(api), DataClient(api)
    _AUTH_PROFILE = (cache_key, time.monotonic(),
                     getattr(api, "_lego_token_check_enabled", None))
    ensure_token_fresh(api)
    _CLIENTS = (cache_key, time.monotonic(), trade, data)
    return trade, data


def clients_endpoint(trade_client, data_client) -> str | None:
    """Return the endpoint bound to this exact cached authenticated pair.

    Operational smoke tools must prove their UAT/Production label describes the
    clients they will actually call.  Reading the environment twice is not such
    a proof because it can change between validation and client construction.
    """
    if _CLIENTS is None:
        return None
    cache_key, _built_at, cached_trade, cached_data = _CLIENTS
    if cached_trade is not trade_client or cached_data is not data_client:
        return None
    return str(cache_key[0])


def fetch_holdings(trade_client, cfg: Config) -> float:
    """Shares of cfg.symbol the broker says the account holds, right now.

    Split out of fetch_snapshot for the post-execution read: confirming a fill
    needs the position and nothing else, and going through the snapshot would
    also spend a market-data call — the one call that can answer 403 for a
    subscription reason that has nothing to do with the fill being confirmed.
    """
    account_id = os.environ["WEBULL_ACCOUNT_ID"]
    positions = _retry_transient(
        lambda: trade_client.account_v2.get_account_position(account_id).json())
    return float(_extract_qty(positions, cfg.symbol))


def parse_buying_power(payload, currency: str = "USD") -> Decimal:
    """Read the documented v3 currency buying power without shape guesses."""
    if not isinstance(payload, dict):
        raise WebullConfigError("account balance response ไม่ใช่ object")
    assets = payload.get("account_currency_assets")
    if not isinstance(assets, list):
        raise WebullConfigError("account balance ไม่มี account_currency_assets list")
    wanted = currency.strip().upper()
    matches = [item for item in assets if isinstance(item, dict)
               and str(item.get("currency") or "").upper() == wanted]
    if len(matches) != 1:
        raise WebullConfigError(f"account balance ต้องมี {wanted} asset หนึ่งรายการ")
    raw = matches[0].get("buying_power")
    if not isinstance(raw, str):
        raise WebullConfigError("buying_power ต้องเป็น decimal string ตาม contract")
    try:
        value = Decimal(raw)
    except InvalidOperation as exc:
        raise WebullConfigError("buying_power ไม่ใช่ decimal") from exc
    if not value.is_finite() or value < 0:
        raise WebullConfigError("buying_power ต้อง finite และ >= 0")
    return value


def fetch_buying_power(trade_client, currency: str = "USD") -> Decimal:
    account_id = os.environ["WEBULL_ACCOUNT_ID"]
    payload = _retry_transient(
        lambda: trade_client.account_v2.get_account_balance(account_id).json())
    return parse_buying_power(payload, currency)


def validate_preview_funding(*, side: str, quantity: object,
                             holdings: object, preview: dict,
                             buying_power: Decimal | None = None) -> dict:
    """Fail closed before Place on inventory or Preview-estimated cash needs."""
    try:
        qty = Decimal(str(quantity))
        held = Decimal(str(holdings))
    except InvalidOperation as exc:
        raise WebullConfigError("quantity/holdings ไม่ใช่ decimal") from exc
    if not qty.is_finite() or not held.is_finite() or qty <= 0 or held < 0:
        raise WebullConfigError("quantity/holdings ต้อง finite และอยู่ในช่วงที่อนุญาต")
    normalized = side.strip().upper()
    if normalized == "SELL":
        if qty > held:
            raise WebullConfigError("SELL quantity เกิน position ที่ขายได้")
        return {"sellable_quantity": str(held), "required_cash": "0"}
    if normalized != "BUY" or buying_power is None:
        raise WebullConfigError("BUY ต้องมี buying power ที่ตรวจสอบได้")
    try:
        estimated_cost = Decimal(preview["estimated_cost"])
        estimated_fee = Decimal(preview["estimated_transaction_fee"])
    except (KeyError, InvalidOperation) as exc:
        raise WebullConfigError("Preview funding fields ไม่ถูกต้อง") from exc
    required = estimated_cost + estimated_fee
    if not required.is_finite() or required < 0:
        raise WebullConfigError("Preview required cash ต้อง finite และ >= 0")
    if required > buying_power:
        raise WebullConfigError("Preview cost+fee เกิน buying power")
    return {"buying_power": str(buying_power), "required_cash": str(required)}


def fetch_snapshot(trade_client, data_client, cfg: Config) -> dict:
    holdings = fetch_holdings(trade_client, cfg)
    category = market_category()
    try:
        snap = _retry_transient(
            lambda: data_client.market_data.get_snapshot(
                cfg.symbol.upper(), category,
                extend_hour_required=False, overnight_required=False).json())
    except Exception as exc:
        if _http_status(exc) == 403:
            raise MarketDataForbidden(
                "403 จาก market data — OpenAPI ต้องซื้อ subscription (LV1/LV2) แยกจาก "
                "แอป Webull; ตรวจสิทธิ์ที่ /app/subscriptions/list") from exc
        raise
    price = _extract_price(snap, cfg.symbol)
    if not (price and price > 0):
        raise ValueError(f"snapshot price ไม่ถูกต้อง ({price}) — fail closed")
    quote_time = _extract_quote_time(snap, cfg.symbol)
    if quote_time is None:
        raise ValueError(
            "snapshot ไม่มี last_trade_time ที่ตรวจสอบได้ — fail closed ไม่ใช้เวลารับ response แทนเวลา quote")
    return {
        "captured_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "quote_time": quote_time.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "price": float(price),
        "holdings": float(holdings),
    }


def quantity_string(qty: float, precision: int) -> str:
    """Format an order quantity, stripping trailing zeros only after a point.

    At LEGO_DECIMAL_PRECISION=0 (whole shares — a documented, allowed setting)
    the formatted quantity has no '.' to stop rstrip('0'), so 20 shares became
    '2' and 100 became '1'. Nothing downstream could catch it: the 17-column
    ledger is theoretical and never reads the filled quantity, so the dashboard
    would show a healthy chain while the real position was 10% of target.

    A quantity that rounds away to zero is refused rather than sent as '0'.
    """
    text = f"{qty:.{precision}f}"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    if not text or float(text) <= 0:
        raise ValueError(
            f"quantity {qty!r} ปัดที่ {precision} ทศนิยมแล้วเหลือ 0 — fail closed ไม่ส่ง")
    return text


def build_order_payload(cfg: Config, side: str, qty: float, client_order_id: str) -> list[dict]:
    return [{
        "combo_type": "NORMAL",
        "client_order_id": client_order_id,
        "symbol": cfg.symbol.upper(),
        "instrument_type": "EQUITY",
        "market": "US",
        "order_type": "MARKET",
        "quantity": quantity_string(qty, cfg.decimal_precision),
        "side": side,
        "time_in_force": "DAY",
        "entrust_type": "QTY",
        "support_trading_session": "CORE",
    }]


def preview_market_order(trade_client, order: list[dict]) -> bool:
    try:
        preview_market_order_result(trade_client, order, strict=False)
        return True
    except WebullConfigError:
        return False


def preview_market_order_result(
        trade_client, order: list[dict], *, strict: bool = True) -> dict:
    """Return a validated Preview result; never reinterpret it as acceptance."""
    account_id = os.environ["WEBULL_ACCOUNT_ID"]
    pr = _retry_transient(lambda: trade_client.order_v3.preview_order(account_id, order).json())
    if not pr:
        raise WebullConfigError("Preview response ว่าง")
    if isinstance(pr, list):
        pr = pr[0] if pr else {}
    if not isinstance(pr, dict):
        raise WebullConfigError("Preview response ไม่ใช่ object")
    if any(pr.get(key) for key in _PREVIEW_ERROR_KEYS):
        raise WebullConfigError("Preview ถูก broker ปฏิเสธ")
    if strict:
        for field in ("estimated_cost", "estimated_transaction_fee"):
            value = pr.get(field)
            if not isinstance(value, str):
                raise WebullConfigError(f"Preview ไม่มี {field} string ตาม contract")
            try:
                number = Decimal(value)
            except InvalidOperation as exc:
                raise WebullConfigError(f"Preview {field} ไม่ใช่ decimal") from exc
            if not number.is_finite() or number < 0:
                raise WebullConfigError(f"Preview {field} ต้อง finite และ >= 0")
    return dict(pr)


def canonical_payload_hash(order: list[dict]) -> str:
    encoded = json.dumps(order, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(encoded.encode()).hexdigest()


def validate_place_response(payload, client_order_id: str) -> dict:
    if isinstance(payload, list):
        payload = payload[0] if len(payload) == 1 else None
    if not isinstance(payload, dict):
        raise WebullConfigError("Place response shape ไม่รู้จัก; outcome เป็น unknown")
    returned_client_id = str(payload.get("client_order_id") or "")
    order_id = str(payload.get("order_id") or "")
    if returned_client_id != str(client_order_id) or not order_id:
        raise WebullConfigError(
            "Place response ไม่มีหรือขัดแย้ง client_order_id/order_id; outcome เป็น unknown")
    return dict(payload)


def validate_order_detail_identity(payload, *, client_order_id: str,
                                   symbol: str) -> dict:
    fields = payload
    if isinstance(fields, list):
        fields = fields[0] if len(fields) == 1 else None
    if isinstance(fields, dict):
        for key in ("data", "items", "orders"):
            nested = fields.get(key)
            if isinstance(nested, list) and len(nested) == 1:
                fields = nested[0]
                break
            if isinstance(nested, dict):
                fields = nested
                break
    if not isinstance(fields, dict):
        raise WebullConfigError("Order detail shape ไม่รู้จัก")
    if str(fields.get("client_order_id") or "") != str(client_order_id):
        raise WebullConfigError("Order detail client_order_id ไม่ตรง intent")
    if str(fields.get("symbol") or "").upper() != symbol.strip().upper():
        raise WebullConfigError("Order detail symbol ไม่ตรง intent")
    return dict(payload)


def place_market_order(trade_client, order: list[dict]) -> dict:
    """Send the order exactly once.

    This used to retry on a transient error, which is the one retry the flow
    cannot afford: a timeout or a 502 says nothing about whether the broker
    accepted the order, so re-sending is a blind second attempt at real money.
    The caller records PLACING_UNKNOWN before this call, and the worker resolves
    it by asking for the same client_order_id — that is the sanctioned recovery,
    and it works whether or not the first attempt landed.
    """
    account_id = os.environ["WEBULL_ACCOUNT_ID"]
    return trade_client.order_v3.place_order(account_id, order).json()


def fetch_order_detail(trade_client, client_order_id: str) -> dict:
    account_id = os.environ["WEBULL_ACCOUNT_ID"]
    return _retry_transient(
        lambda: trade_client.order_v3.get_order_detail(account_id, client_order_id).json())


def _open_order_items(res) -> list:
    if isinstance(res, list):
        return res
    if isinstance(res, dict):
        for key in ("orders", "items", "data"):
            if key in res:
                items = res.get(key) or []
                if not isinstance(items, list):
                    raise ValueError("open-orders items ต้องเป็น list — fail closed")
                return items
    raise ValueError("open-orders response shape ไม่รู้จัก — fail closed")


def _page_cursor(items: list) -> str | None:
    """The client_order_id the next page continues from.

    Group orders arrive as a wrapper whose own id may be absent while the legs
    underneath carry theirs, so the last leg answers when the wrapper cannot.
    Returning None ends the walk, which is the safe direction: one page short is
    the behaviour we already had, an endless loop is not.
    """
    for entry in reversed(items):
        if not isinstance(entry, dict):
            continue
        if entry.get("client_order_id"):
            return str(entry["client_order_id"])
        legs = entry.get("items")
        if isinstance(legs, list):
            for leg in reversed(legs):
                if isinstance(leg, dict) and leg.get("client_order_id"):
                    return str(leg["client_order_id"])
    return None


def _open_order_page_size() -> int:
    return max(1, int(os.environ.get("LEGO_OPEN_ORDER_PAGE_SIZE", "50")))


def _open_order_max_pages() -> int:
    return max(1, int(os.environ.get("LEGO_OPEN_ORDER_MAX_PAGES", "5")))


def fetch_open_orders(trade_client, symbol: str) -> list[dict]:
    """Every open order for *symbol*, following the broker's paging.

    get_order_open answers 10 orders per page by default and the reply is a page,
    not the whole book. The dispatcher uses an empty result to mean 'nothing of
    ours is live at the broker', so ten unrelated orders on the first page were
    enough to hide our own and let a second order go out on top of it.
    """
    account_id = os.environ["WEBULL_ACCOUNT_ID"]
    page_size = _open_order_page_size()
    cursor = None
    out: list[dict] = []
    for _ in range(_open_order_max_pages()):
        res = _retry_transient(
            lambda after=cursor: trade_client.order_v3.get_order_open(
                account_id, page_size=page_size, last_client_order_id=after).json())
        items = _open_order_items(res)
        for o in items:
            if not isinstance(o, dict):
                continue
            inner = o.get("items")
            cands = inner if isinstance(inner, list) else [o]
            for c in cands:
                if isinstance(c, dict) and str(c.get("symbol", "")).upper() == symbol.upper():
                    out.append(c)
        if len(items) < page_size:
            return out
        next_cursor = _page_cursor(items)
        if not next_cursor:
            raise IncompleteOpenOrdersError(
                "open-orders page เต็มแต่ไม่มี cursor — ยืนยันรายการทั้งหมดไม่ได้")
        if next_cursor == cursor:
            raise IncompleteOpenOrdersError(
                "open-orders cursor ไม่เดินหน้า — ยืนยันรายการทั้งหมดไม่ได้")
        cursor = next_cursor
    raise IncompleteOpenOrdersError(
        f"open-orders ยังมีหน้าถัดไปหลังครบ {_open_order_max_pages()} หน้า — "
        "block order แบบ fail-closed")


def _extract_qty(positions, symbol: str) -> float:
    if isinstance(positions, list):
        items = positions
    elif isinstance(positions, dict):
        for key in ("positions", "items", "data"):
            if key in positions:
                items = positions.get(key) or []
                break
        else:
            raise ValueError("positions response shape ไม่รู้จัก — fail closed")
    else:
        raise ValueError("positions response shape ไม่รู้จัก — fail closed")
    if not isinstance(items, list):
        raise ValueError("positions items ต้องเป็น list — fail closed")
    for p in items:
        if isinstance(p, dict) and str(p.get("symbol", "")).upper() == symbol.upper():
            return float(p.get("quantity", 0) or 0)
    return 0.0


_PRICE_KEYS = ("last", "lastPrice", "price", "close")
_QUOTE_TIME_KEYS = ("last_trade_time", "lastTradeTime", "trade_time")


def _entry_symbol(entry: dict) -> str:
    return str(entry.get("symbol") or "").upper()


def _price_of(entry: dict) -> float:
    for key in _PRICE_KEYS:
        if entry.get(key):
            return float(entry[key])
    return 0.0


def _snapshot_entry(snap, symbol: str) -> dict:
    """Return the one snapshot record that belongs to *symbol*."""
    want = symbol.upper()
    if isinstance(snap, list):
        entries = [entry for entry in snap if isinstance(entry, dict)]
        matching = [entry for entry in entries if _entry_symbol(entry) == want]
        if matching:
            snap = matching[0]
        elif any(_entry_symbol(entry) for entry in entries):
            return {}                    # response is about other symbols only
        else:
            # Unnamed single-symbol payload: the historical shape get_snapshot
            # returns for a one-symbol request.
            snap = entries[0] if entries else {}
    if not isinstance(snap, dict):
        return {}
    named = _entry_symbol(snap)
    if not named or named == want:
        if _price_of(snap):
            return snap
    nested = snap.get(want) or snap.get(symbol)
    return nested if isinstance(nested, dict) else {}


def _quote_datetime(raw) -> datetime | None:
    """Normalize Webull's Unix-millisecond trade time without guessing."""
    if raw is None or isinstance(raw, bool):
        return None
    try:
        numeric = float(raw)
    except (TypeError, ValueError):
        return None
    if not (numeric > 0 and numeric < float("inf")):
        return None
    # Webull documents last_trade_time as Unix epoch milliseconds.  Requiring
    # millisecond scale avoids silently reading an unrelated small integer as a
    # plausible 1970 quote and makes response-schema drift fail closed.
    if numeric < 100_000_000_000:
        return None
    try:
        return datetime.fromtimestamp(numeric / 1000.0, timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def _extract_quote_time(snap, symbol: str) -> datetime | None:
    """Read the source trade time from the same symbol record as its price."""
    entry = _snapshot_entry(snap, symbol)
    for key in _QUOTE_TIME_KEYS:
        if key in entry:
            return _quote_datetime(entry.get(key))
    return None


def _extract_price(snap, symbol: str) -> float:
    """Read the price of *symbol*, never of whatever came first.

    _extract_qty refuses to answer with another symbol's position; this function
    took the first entry it saw, so a batch response or a mis-routed quote priced
    the row off a different stock. That price goes straight into build_decision,
    where gap = fix_c - holdings*price decides BUY/SELL/PASS and the order size,
    with no later check that could notice.

    An entry that names a different symbol is now skipped. Returning 0.0 is the
    fail-closed answer: fetch_snapshot already turns a non-positive price into a
    ValueError, so the row is never built rather than built on a wrong price.
    """
    return _price_of(_snapshot_entry(snap, symbol))
