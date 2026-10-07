"""Immutable release-bound recovery controls; legacy intents remain hold-only."""
from dataclasses import asdict, dataclass
import hashlib
import json


# cancel_expire = cancel, and when the broker refuses it and the DAY session is over,
# release the order only on the proof in order_recovery.expiry_proof_blockers.
# UAT only: config.DeploymentProfile refuses it on PROD.
ACTIONS = frozenset({"hold", "cancel", "cancel_expire"})
CANCEL_ACTIONS = frozenset({"cancel", "cancel_expire"})


@dataclass(frozen=True)
class RecoveryPolicy:
    action: str = "hold"
    stale_seconds: int = 300
    grace_seconds: int = 120
    max_mutations: int = 1

    def __post_init__(self):
        if self.action not in ACTIONS:
            raise ValueError("LEGO_STALE_ORDER_ACTION must be hold, cancel or cancel_expire")
        if any(type(v) is not int or v <= 0 for v in
               (self.stale_seconds, self.grace_seconds, self.max_mutations)):
            raise ValueError("recovery durations must be positive integers")
        if self.max_mutations != 1:
            raise ValueError("LEGO_MAX_CANCEL_MUTATIONS_PER_ORDER must be 1")

    def snapshot(self):
        return asdict(self)

    @property
    def fingerprint(self):
        return hashlib.sha256(json.dumps(self.snapshot(), sort_keys=True,
                                        separators=(",", ":")).encode()).hexdigest()

    @classmethod
    def from_env(cls, env):
        if "LEGO_ORDER_TIMEOUT_SECONDS" in env:
            raise ValueError("replace LEGO_ORDER_TIMEOUT_SECONDS with LEGO_STALE_ORDER_SECONDS")
        return cls(env.get("LEGO_STALE_ORDER_ACTION", "hold"),
                   int(env.get("LEGO_STALE_ORDER_SECONDS", "300")),
                   int(env.get("LEGO_CANCEL_CONFIRM_GRACE_SECONDS", "120")),
                   int(env.get("LEGO_MAX_CANCEL_MUTATIONS_PER_ORDER", "1")))
