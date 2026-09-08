"""Move finished order records off the paths the live loops scan.

list_actionable and pending_audits each read a whole RTDB path and filter in
Python. That is correct and cheap while the path is small, and it stays correct
forever — but not cheap: every slot adds one intent and one audit, so after a
year the order worker downloads thousands of finished records on every tick to
find at most a handful of live ones, and the dashboard pays the same cost on
every page load.

Records are moved, never deleted, so the history stays queryable at
*_archive; only the working set shrinks. Two records are always left in place:
anything a human still has to answer (needs_manual_check) and anything that
carries no usable timestamp — an undatable record is never old enough to move.
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

from firebase_admin import db

from lego_orders import TERMINAL_STATUSES, normalize_status
from lego_outbox import DISPATCH_LOCK_PATH, OUTBOX_PATH, TERMINAL
from lego_state import AUDIT_PATH

UTC = timezone.utc
OUTBOX_ARCHIVE_PATH = f"{OUTBOX_PATH}_archive"
AUDIT_ARCHIVE_PATH = f"{AUDIT_PATH}_archive"
# An audit is finished when either ledger says so: the outbox has nothing left to
# dispatch, or the broker has nothing left to do.
AUDIT_TERMINAL = TERMINAL | TERMINAL_STATUSES
TIMESTAMP_FIELDS = ("updated_at", "placed_at", "created_at",
                    "decision_time", "slot_start_utc")


def retention_days() -> int:
    return max(1, int(os.environ.get("LEGO_ARCHIVE_RETENTION_DAYS", "30")))


def archive_limit() -> int:
    return max(1, int(os.environ.get("LEGO_ARCHIVE_LIMIT", "500")))


def _bounded_oldest(path: str, timestamp_child: str,
                    cutoff: datetime, limit: int) -> dict:
    """Return a bounded old tail; never download an unbounded history root."""
    cutoff_text = cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")
    result = (db.reference(path)
              .order_by_child(timestamp_child)
              .end_at(cutoff_text)
              .limit_to_first(max(1, limit * 4))
              .get() or {})
    if not isinstance(result, dict):
        raise ValueError("archive bounded query response ต้องเป็น object")
    return result


def _parse_ts(value) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(UTC)
    except (TypeError, ValueError):
        return None


def _finished_before(doc: dict, cutoff: datetime) -> bool:
    """True only when every timestamp on the record is older than *cutoff*."""
    stamps = [ts for ts in (_parse_ts(doc.get(f)) for f in TIMESTAMP_FIELDS) if ts]
    return bool(stamps) and max(stamps) < cutoff


def _movable(doc, terminal: set[str], cutoff: datetime) -> bool:
    return (isinstance(doc, dict)
            and normalize_status(doc.get("status")) in terminal
            and not doc.get("needs_manual_check")
            and not doc.get("audit_pending")
            and _finished_before(doc, cutoff))


class _ArchiveRace(RuntimeError):
    """The live record changed while an archive attempt was in progress."""


def _move_if_unchanged(source_path: str, archive_path: str, key: str,
                       scanned: dict, terminal: set[str], cutoff: datetime) -> bool:
    """Atomically delete only the exact record that was judged movable.

    RTDB cannot condition a multi-location update on two independently changing
    nodes.  A small durable claim closes the dangerous window without a root
    transaction: claim the exact scanned value, copy that claimed value, then
    transactionally delete only if no writer changed it.  A crash at either
    boundary is replayable.  If recovery updates the source after the claim,
    the compare fails and the live record is retained.
    """
    source = db.reference(f"{source_path}/{key}")
    archive = db.reference(f"{archive_path}/{key}")
    token = uuid.uuid4().hex

    def claim(current):
        if current != scanned or not _movable(current, terminal, cutoff):
            raise _ArchiveRace("archive eligibility changed before claim")
        claimed = dict(current)
        claimed["_archive_claim"] = token
        return claimed

    try:
        claimed = source.transaction(claim)
    except _ArchiveRace:
        return False

    archive.set(claimed)

    def delete_claimed(current):
        if current != claimed or current.get("_archive_claim") != token:
            raise _ArchiveRace("live record changed after archive copy")
        # Returning None from an RTDB transaction is an atomic delete.
        return None

    try:
        source.transaction(delete_claimed)
    except _ArchiveRace:
        # The archive copy is only a stale attempt; remove it iff it is still
        # our copy.  Never remove a newer successful archive attempt.
        def discard_our_copy(current):
            if isinstance(current, dict) and current.get("_archive_claim") == token:
                return None
            return current

        archive.transaction(discard_our_copy)
        return False
    return True


def archive_terminal_intents(cutoff: datetime, limit: int,
                             chain_key_: str | None = None,
                             dispatch_key_: str | None = None) -> int:
    moved = 0
    if chain_key_ is None:
        chains = (db.reference(OUTBOX_PATH).get() or {}).items()
    else:
        chains = [(chain_key_, _bounded_oldest(
            f"{OUTBOX_PATH}/{chain_key_}", "created_at", cutoff, limit))]
    for ck, intents in chains:
        if not isinstance(intents, dict):
            continue
        dispatch_key = dispatch_key_ or ck
        dispatch = db.reference(
            f"{DISPATCH_LOCK_PATH}/{dispatch_key}").get() or {}
        inflight_run_id = str(dispatch.get("inflight_run_id") or "") \
            if isinstance(dispatch, dict) else ""
        for run_id, doc in intents.items():
            if moved >= limit:
                return moved
            # A terminal result is written before the order worker clears its
            # durable chain fence. If a crash lands between those operations,
            # archiving this source would make recovery see a missing inflight
            # intent and block the chain forever. Leave it live until the fence
            # is cleared; on the next archive tick it becomes movable normally.
            if str(run_id) == inflight_run_id:
                continue
            if _movable(doc, TERMINAL, cutoff):
                moved += int(_move_if_unchanged(
                    f"{OUTBOX_PATH}/{ck}", f"{OUTBOX_ARCHIVE_PATH}/{ck}",
                    run_id, doc, TERMINAL, cutoff))
    return moved


def archive_terminal_audits(cutoff: datetime, limit: int, *, bounded=False) -> int:
    moved = 0
    docs = (_bounded_oldest(AUDIT_PATH, "placed_at", cutoff, limit)
            if bounded else (db.reference(AUDIT_PATH).get() or {}))
    for event_id, doc in docs.items():
        if moved >= limit:
            return moved
        if _movable(doc, AUDIT_TERMINAL, cutoff):
            moved += int(_move_if_unchanged(
                AUDIT_PATH, AUDIT_ARCHIVE_PATH, event_id, doc,
                AUDIT_TERMINAL, cutoff))
    return moved


def archive_terminal_records(now_utc: datetime | None = None, *,
                             days: int | None = None, limit: int | None = None,
                             chain_key_: str | None = None,
                             dispatch_key_: str | None = None) -> dict:
    now_utc = (now_utc or datetime.now(UTC)).astimezone(UTC)
    days = retention_days() if days is None else days
    limit = archive_limit() if limit is None else limit
    cutoff = now_utc - timedelta(days=days)
    return {
        "cutoff": cutoff.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "retention_days": days,
        "intents_archived": archive_terminal_intents(
            cutoff, limit, chain_key_=chain_key_,
            dispatch_key_=dispatch_key_),
        "audits_archived": archive_terminal_audits(
            cutoff, limit, bounded=chain_key_ is not None),
        "bounded": chain_key_ is not None,
    }

