"""Optional bounded webhook delivery; no background work after a Cloud Run tick.

Generic HTTPS JSON receiver only. Platform-specific Telegram/LINE payloads and
credentials belong in a receiver/adapter. Notifications never authorize orders.
"""
import hashlib
import logging
import os
import time
import uuid
from urllib.parse import urlsplit

import requests
from firebase_admin import db
import tick_runtime

logger = logging.getLogger(__name__)
# The release window and the DNA end silently. One alert per family (the most
# severe that applies), each carrying the instant its horizon ends, so an
# operator hears before it closes (RELEASE_EXPIRING within 48h, DNA_LOW under two
# sessions) and once more when it has. Delivery shares the 24h durable cooldown,
# so an expired release costs one message a day instead of one per tick.
HORIZON_FAMILIES = (
    (("RELEASE_EXPIRED", "release_expired", "release_expiry_utc"),
     ("RELEASE_EXPIRING", "release_expiring", "release_expiry_utc")),
    (("DNA_EXHAUSTED", "dna_exhausted", "dna_end_utc"),
     ("DNA_LOW", "dna_low", "dna_end_utc")),
)
ACTION_EVENTS = frozenset({"MANUAL_RECONCILIATION_REQUIRED", "FEE_OVERDUE",
                           "EXECUTION_LIMIT_BLOCKED", "RECONCILIATION_OVERDUE"})
EVENTS = {"BROKER_REJECT_HALT", "AUTH_BACKOFF", "TOKEN_EXPIRY_WARNING",
          "MANUAL_RECONCILIATION_REQUIRED", "FEE_OVERDUE", "EXECUTION_LIMIT_BLOCKED",
          "RECONCILIATION_OVERDUE"} | {
    kind for family in HORIZON_FAMILIES for kind, _flag, _key in family}


def horizon_events(body):
    """[(kind, ends_at)] for each horizon family that needs attention."""
    health = body.get("operational_health") or {}
    events = []
    for family in HORIZON_FAMILIES:
        for kind, flag, key in family:
            if health.get(flag):
                events.append((kind, health.get(key)))
                break
    return events


def notify_tick(body):
    """Reuse durable rate limiting for actionable execution and horizon health."""
    if not os.environ.get("ALERT_WEBHOOK_URL", "").strip():
        return False
    events = []
    kind = body.get("business_status")
    if kind in ACTION_EVENTS:
        events.append((kind, None))
    events.extend(horizon_events(body))
    if not events:
        return False
    try:
        account = os.environ.get("WEBULL_ACCOUNT_ID", "").strip()
        if not account:
            return False
        identity = hashlib.sha256(
            f"{os.environ.get('WEBULL_ENV', 'UAT')}\0{account}".encode()).hexdigest()
        symbol = os.environ.get("LEGO_SYMBOL", "")
        delivered = False
        for event, ends_at in events:
            delivered = notify(event, f"{identity}:{symbol}", symbol=symbol,
                               expires_at=ends_at) or delivered
        return delivered
    except Exception:
        logger.warning("lego tick alert unavailable events=%s",
                       ",".join(event for event, _ in events))
        return False


def notify(kind, scope, *, symbol=None, count=None, expires_at=None):
    url = os.environ.get("ALERT_WEBHOOK_URL", "").strip()
    if not url or kind not in EVENTS:
        return False
    try:
        parsed = urlsplit(url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("webhook must use HTTPS without userinfo")
        remaining = tick_runtime.remaining()
        if remaining is not None and remaining < 5:
            return False
        now, owner = time.time(), uuid.uuid4().hex
        key = hashlib.sha256(f"{kind}\0{scope}".encode()).hexdigest()[:32]
        # Private, bounded node per event/account scope. Failed delivery retries
        # after 60s; successful delivery is rate limited for 24h across instances.
        ref = db.reference(f"webull_lego_alert_delivery/{key}")

        def claim(old):
            doc = dict(old or {})
            if float(doc.get("next_attempt", 0)) > now:
                return doc
            return {**doc, "owner": owner, "next_attempt": now + 60}

        if ref.transaction(claim).get("owner") != owner:
            return False
        payload = {"event": kind, "symbol": symbol,
                   "consecutive_count": count, "expires_at": expires_at}
        with requests.post(url, json=payload, timeout=(1, 2),
                           allow_redirects=False, stream=True) as response:
            if not 200 <= response.status_code < 300:
                raise RuntimeError("webhook non-success response")

        def delivered(old):
            if not old or old.get("owner") != owner:
                return old
            return {**old, "delivered_at": now, "next_attempt": now + 86400}

        ref.transaction(delivered)
        return True
    except Exception:
        # Never include exception text: URLs commonly embed bearer credentials.
        logger.warning("lego alert delivery failed event=%s", kind)
        return False
