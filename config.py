"""Validated v2 configuration for one LEGO deployment.

The six :class:`OperatorSettings` fields are the complete strategy surface.
Infrastructure identity, credentials, and release authorization deliberately
live in :class:`DeploymentProfile`; they are deployment controls, not trading
knobs and cannot be supplied by an HTTP request.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from dna_engine import decode_dna, dna_fingerprint
from recovery_policy import RecoveryPolicy


ENVIRONMENT_HOSTS = {
    "UAT": "th-api.uat.webullbroker.com",
    "PROD": "api.webull.co.th",
}
MODES = {"observe", "trade"}


class ConfigurationError(ValueError):
    """A deployment or strategy value is incomplete, conflicting, or unsafe."""


def _bool(value: object, *, name: str) -> bool:
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    raise ConfigurationError(f"{name} ต้องเป็น true หรือ false")


def _finite_decimal(value: object, *, name: str, positive: bool) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ConfigurationError(f"{name} ต้องเป็นตัวเลข") from exc
    if not math.isfinite(number) or (number <= 0 if positive else number < 0):
        op = "> 0" if positive else ">= 0"
        raise ConfigurationError(f"{name} ต้อง finite และ {op}")
    return number


@dataclass(frozen=True)
class DNABundle:
    dna_code: str
    interval_seconds: int
    origin_utc: str | None
    calendar_id: str
    calendar_fingerprint: str | None
    decoder_version: str
    decoded_array_sha256: str
    explicit_bypass: bool = False

    @classmethod
    def bypass(cls, length: int = 100) -> "DNABundle":
        if length <= 0:
            raise ConfigurationError("bypass DNA length ต้อง > 0")
        code = f"bypass:{length}"
        return cls(
            dna_code=code,
            interval_seconds=900,
            origin_utc=None,
            calendar_id="XNYS-regular",
            calendar_fingerprint=None,
            decoder_version="legacy-v1",
            decoded_array_sha256=dna_fingerprint(code),
            explicit_bypass=True,
        )

    @classmethod
    def from_mapping(cls, raw: Mapping[str, object]) -> "DNABundle":
        required = {
            "dna_code", "interval_seconds", "origin_utc", "calendar_id",
            "calendar_fingerprint", "decoder_version", "decoded_array_sha256",
        }
        missing = sorted(required - set(raw))
        if missing:
            raise ConfigurationError(
                f"dna_bundle ขาด field: {', '.join(missing)}")
        code = str(raw["dna_code"]).strip()
        if not code:
            raise ConfigurationError("dna_bundle.dna_code ต้องไม่ว่าง")
        try:
            interval = int(raw["interval_seconds"])
        except (TypeError, ValueError) as exc:
            raise ConfigurationError("dna_bundle.interval_seconds ต้องเป็นจำนวนเต็ม") from exc
        if interval <= 0 or 23400 % interval != 0:
            raise ConfigurationError(
                "dna_bundle.interval_seconds ต้องหาร 6.5 ชั่วโมงตลาดปกติลงตัว")
        if str(raw["calendar_id"]).strip() != "XNYS-regular":
            raise ConfigurationError("รองรับ dna_bundle.calendar_id=XNYS-regular เท่านั้น")
        # Decode now, at config validation, so malformed DNA never reaches a tick.
        decode_dna(code)
        actual_fingerprint = dna_fingerprint(code)
        expected_fingerprint = str(raw["decoded_array_sha256"]).lower()
        if actual_fingerprint != expected_fingerprint:
            raise ConfigurationError(
                "dna_bundle.decoded_array_sha256 ไม่ตรงกับ decoded DNA")
        return cls(
            dna_code=code,
            interval_seconds=interval,
            origin_utc=(str(raw["origin_utc"]).strip() or None),
            calendar_id=str(raw["calendar_id"]).strip(),
            calendar_fingerprint=(
                str(raw["calendar_fingerprint"]).strip() or None),
            decoder_version=str(raw["decoder_version"]).strip(),
            decoded_array_sha256=actual_fingerprint,
            explicit_bypass=code.startswith("bypass:"),
        )


@dataclass(frozen=True)
class OperatorSettings:
    symbol: str
    principal_usd: float
    diff_usd: float
    dna_bundle: DNABundle
    mode: str = "observe"
    active: bool = False

    def __post_init__(self) -> None:
        symbol = self.symbol.strip().upper()
        if not symbol or not all(c.isalnum() or c in {".", "-"} for c in symbol):
            raise ConfigurationError("symbol ไม่ถูกต้อง")
        object.__setattr__(self, "symbol", symbol)
        _finite_decimal(self.principal_usd, name="principal_usd", positive=True)
        _finite_decimal(self.diff_usd, name="diff_usd", positive=False)
        mode = self.mode.strip().lower()
        if mode not in MODES:
            raise ConfigurationError("mode ต้องเป็น observe หรือ trade")
        object.__setattr__(self, "mode", mode)
        if type(self.active) is not bool:
            raise ConfigurationError("active ต้องเป็น boolean")

    @property
    def allows_new_intents(self) -> bool:
        return self.active and self.mode == "trade"

    def canonical(self) -> dict:
        return {
            "symbol": self.symbol,
            "principal_usd": format(self.principal_usd, ".17g"),
            "diff_usd": format(self.diff_usd, ".17g"),
            "dna_fingerprint": self.dna_bundle.decoded_array_sha256,
            "mode": self.mode,
            "active": self.active,
        }

    @property
    def config_hash(self) -> str:
        raw = json.dumps(self.canonical(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode()).hexdigest()


@dataclass(frozen=True)
class DeploymentProfile:
    environment: str
    project_id: str
    region: str
    database_url: str
    account_id: str
    candidate_hash: str
    release_authorization: str
    allow_fractional: bool = True
    execution_limits: tuple[str, ...] = ("", "", "", "")
    trading_symbol: str = ""
    strategy_binding: str = ""
    session_key_mode: str = "release_window"
    recovery_policy: RecoveryPolicy = RecoveryPolicy()
    # Production only: the acknowledgement of the exact release (see
    # release_horizon.prod_live_ack). Deliberately NOT part of
    # expected_release_binding, so existing UAT releases keep their binding.
    prod_live_ack: str = ""

    def __post_init__(self) -> None:
        if self.session_key_mode not in {"market_day", "release_window"}:
            raise ConfigurationError("LEGO_SESSION_KEY_MODE must be market_day or release_window")
        if type(self.allow_fractional) is not bool:
            raise ConfigurationError("LEGO_ALLOW_FRACTIONAL must be boolean")
        environment = self.environment.strip().upper()
        if environment == "PRODUCTION":
            environment = "PROD"
        if environment not in ENVIRONMENT_HOSTS:
            raise ConfigurationError("environment ต้องเป็น UAT หรือ PROD")
        object.__setattr__(self, "environment", environment)
        if environment == "PROD" and self.recovery_policy.action == "cancel_expire":
            # Releasing an order the broker still calls PENDING rests on UAT
            # evidence only (2026-10-06). On real money a person confirms it with
            # tools/resume_order_reconciliation --expiry-proof instead.
            raise ConfigurationError(
                "LEGO_STALE_ORDER_ACTION=cancel_expire is UAT only; "
                "PROD must use hold or cancel")
        if not self.account_id.strip():
            raise ConfigurationError("WEBULL_ACCOUNT_ID ว่างหรือไม่ได้ตั้งค่า")
        if self.database_url and not self.database_url.startswith("https://"):
            raise ConfigurationError("FIREBASE_DB_URL ต้องเป็น HTTPS")

    @property
    def endpoint(self) -> str:
        return ENVIRONMENT_HOSTS[self.environment]

    @property
    def account_fingerprint(self) -> str:
        raw = f"webull-runtime-v2\0{self.environment}\0{self.account_id.strip()}"
        return hashlib.sha256(raw.encode()).hexdigest()

    @property
    def expected_release_binding(self) -> str:
        from execution_limits import policy_hash
        raw = (
            f"lego-release-v4\0{self.environment}\0"
            f"{self.account_fingerprint}\0{self.candidate_hash}\0"
            f"{self.trading_symbol}\0{policy_hash(self.execution_limits)}\0"
            f"{self.strategy_binding}\0{self.allow_fractional}\0"
            f"{self.session_key_mode}\0{self.recovery_policy.fingerprint}"
        )
        return hashlib.sha256(raw.encode()).hexdigest()

    @property
    def release_is_authorized(self) -> bool:
        return bool(
            self.candidate_hash
            and self.release_authorization
            and self.release_authorization == self.expected_release_binding
        )


@dataclass(frozen=True)
class RuntimeConfig:
    operator: OperatorSettings
    deployment: DeploymentProfile

    @property
    def prod_live_gate_open(self) -> bool:
        """Real money needs the operator to echo the ack derived from this release.

        The release binding alone is a fingerprint anyone can recompute, and it
        is checked by the runtime but not by every deploy path (deploy.ps1 and a
        plain `gcloud` deploy never looked at it). Enforcing the ack here makes
        the gate independent of how the service was deployed. Non-production
        environments are unaffected.
        """
        if self.deployment.environment != "PROD":
            return True
        from release_horizon import prod_live_gate_open
        return prod_live_gate_open(self)

    @property
    def allows_new_broker_mutation(self) -> bool:
        return (self.operator.allows_new_intents
                and self.deployment.release_is_authorized
                and self.prod_live_gate_open)


def _load_bundle(env: Mapping[str, str]) -> DNABundle:
    bundle_path = env.get("LEGO_DNA_BUNDLE", "").strip()
    if bundle_path:
        try:
            raw = json.loads(Path(bundle_path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ConfigurationError(f"อ่าน LEGO_DNA_BUNDLE ไม่สำเร็จ: {exc}") from exc
        if not isinstance(raw, dict):
            raise ConfigurationError("LEGO_DNA_BUNDLE ต้องเป็น JSON object")
        return DNABundle.from_mapping(raw)

    # Compatibility bridge. A trained legacy DNA without origin/timeframe is
    # intentionally rejected; only an explicit bypass is safe to infer.
    legacy_code = env.get("LEGO_DNA_CODE", "bypass:100").strip()
    if not legacy_code.startswith("bypass:"):
        raise ConfigurationError(
            "legacy LEGO_DNA_CODE ต้องย้ายเป็น DNA bundle ที่มี origin/timeframe")
    length = int(legacy_code.split(":", 1)[1])
    return DNABundle.bypass(length)


def load_runtime_config(env: Mapping[str, str] | None = None) -> RuntimeConfig:
    from execution_limits import ENV_KEYS
    env = os.environ if env is None else env
    try:
        recovery_policy = RecoveryPolicy.from_env(env)
    except (TypeError, ValueError) as exc:
        raise ConfigurationError(str(exc)) from exc
    # AUTO_SUBMIT belongs to the legacy facade only. Letting it influence v2
    # made an obsolete environment variable silently opt a deployment into
    # trading, bypassing the six-field operator contract.
    mode = env.get("LEGO_MODE", "observe")
    active = _bool(env.get("LEGO_ACTIVE", "false"), name="LEGO_ACTIVE")

    operator = OperatorSettings(
        symbol=env.get("LEGO_SYMBOL", ""),
        principal_usd=_finite_decimal(
            env.get("LEGO_FIX_C"), name="principal_usd", positive=True),
        diff_usd=_finite_decimal(
            env.get("LEGO_DIFF"), name="diff_usd", positive=False),
        dna_bundle=_load_bundle(env),
        mode=str(mode),
        active=active,
    )
    deployment = DeploymentProfile(
        environment=env.get("WEBULL_ENV", "UAT"),
        project_id=env.get("GOOGLE_CLOUD_PROJECT", ""),
        region=env.get("FUNCTION_REGION", "asia-southeast1"),
        database_url=env.get("FIREBASE_DB_URL", ""),
        account_id=env.get("WEBULL_ACCOUNT_ID", ""),
        candidate_hash=env.get("LEGO_CANDIDATE_HASH", ""),
        release_authorization=env.get("LEGO_RELEASE_AUTHORIZATION", ""),
        allow_fractional=_bool(env.get("LEGO_ALLOW_FRACTIONAL", "true"),
                               name="LEGO_ALLOW_FRACTIONAL"),
        execution_limits=tuple(env.get(key, "").strip() for key in ENV_KEYS),
        trading_symbol=operator.symbol,
        strategy_binding=hashlib.sha256(json.dumps({
            "operator": operator.canonical(),
            "bundle": operator.dna_bundle.__dict__,
        }, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
        session_key_mode=env.get("LEGO_SESSION_KEY_MODE", "release_window"),
        recovery_policy=recovery_policy,
        prod_live_ack=env.get("LEGO_PROD_LIVE_ACK", "").strip(),
    )
    return RuntimeConfig(operator=operator, deployment=deployment)


def release_binding_for(env: Mapping[str, str] | None = None) -> str:
    """Return the exact value an explicitly authorized deployment must store."""
    runtime = load_runtime_config(env)
    return runtime.deployment.expected_release_binding
