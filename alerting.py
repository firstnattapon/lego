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
EVENTS = {"BROKER_REJECT_HALT", "AUTH_BACKOFF", "TOKEN_EXPIRY_WARNING",
          "MANUAL_RECONCILIATION_REQUIRED", "FEE_OVERDUE", "EXECUTION_LIMIT_BLOCKED",
          "RECONCILIATION_OVERDUE"}


def notify_tick(body):
    """Reuse durable rate limiting for actionable execution health only."""
    if not os.environ.get("ALERT_WEBHOOK_URL", "").strip():
        return False
    kind = body.get("business_status")
    if kind not in {"MANUAL_RECONCILIATION_REQUIRED", "FEE_OVERDUE", "EXECUTION_LIMIT_BLOCKED", "RECONCILIATION_OVERDUE"}:
        return False
    try:
        account = os.environ.get("WEBULL_ACCOUNT_ID", "").strip()
        if not account:
            return False
        identity = hashlib.sha256(
            f"{os.environ.get('WEBULL_ENV', 'UAT')}\0{account}".encode()).hexdigest()
        symbol = os.environ.get("LEGO_SYMBOL", "")
        return notify(kind, f"{identity}:{symbol}", symbol=symbol)
    except Exception:
        logger.warning("lego tick alert unavailable event=%s", kind)
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
