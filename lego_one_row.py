"""Pure LEGO row engine for the fixed 17-column contract.

The 17-column recurrence is a model ledger, not broker-realized P&L, and it is
now split across the two machines that own it:

* This module (driven by lego_one_row) decides. It reads the price and the
  holdings, walks the DNA, and produces the decision columns plus Rₙ, which is
  live on every row. A READY_BUY/READY_SELL is an *intent to trade*, so the
  cashflow columns ΔAₙ/Aₙ/Eₙ are carried forward unchanged — the same values a
  PASS row gets. Nothing here may advance them.
* lego_order_worker finalizes. Once the broker confirms a real fill and the
  post-execution holdings, `finalize_recurrence` computes ΔAₙ/Aₙ/Eₙ from the
  filled price and the worker patches them onto the committed row.

`compute_recurrence` still implements both branches: the act branch is what the
worker's finalization reduces to once a fill is confirmed, and it stays here so
the two paths cannot drift apart. DNA progression is supplied by the market
clock in production; legacy anchor+1 remains available for shadow mode and
backward-compatible tests.

LEAPS as stock replacement (``Config.leaps_shares``, env ``LEGO_LEAPS``): the
position the strategy rebalances is the real broker shares plus the LEAPS
share-equivalents (1 contract = 100). They are added once, first, inside
`build_decision`, so the row and every dispatch-time re-check value the same
position, while a SELL stays limited to the real shares (LEAPS cannot be sold
as shares). Everything that guards the account — the vanished-position check,
the chain's prev_holdings, the paper book — keeps reading real shares only.
With no LEAPS every value here is the very one it always was.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN

from dna_engine import decode_dna

SNAPSHOT_READY = "SNAPSHOT_READY"
PASS_DNA_ZERO = "PASS_DNA_ZERO"
PASS_THRESHOLD = "PASS_THRESHOLD"
PASS_MIN_ORDER = "PASS_MIN_ORDER"
# LEAPS only: the rebalance would have to sell at least every real share. LEAPS
# cannot be sold as shares, and the order is never cut down to what is held.
PASS_SELL_EXCEEDS_REAL = "PASS_SELL_EXCEEDS_REAL"
READY_BUY = "READY_BUY"
READY_SELL = "READY_SELL"
DECISION_STAGE = 8

COLUMN_ORDER = [
    "เวลา (UTC)", "สินทรัพย์", "สถานะ", "DNA step", "DNA signal",
    "ราคา Pₙ (USD)", "จำนวนถือครอง (หุ้น)", "คำสั่ง", "ฝั่ง", "เหตุผล",
    "จำนวนสั่ง (หุ้น)", "มูลค่าพอร์ต (USD)", "ส่วนต่างเป้าหมาย (USD)",
    "Rₙ อ้างอิง (USD)", "ΔAₙ ต่อสเต็ป (USD)", "Aₙ สะสม (USD)",
    "Eₙ ส่วนเกินสะสม (USD)",
]

# The four cashflow columns, named off the contract itself so the worker that
# finalizes three of them can never patch a column the engine stopped writing.
REFERENCE_COLUMN, DELTA_COLUMN, ACTUAL_COLUMN, EXCESS_COLUMN = COLUMN_ORDER[13:17]

# A column the contract no longer carries. Rows and cashflow observations that
# an earlier revision wrote may still hold it; it is tolerated on the way in and
# never written again. Not part of COLUMN_ORDER.
LEGACY_DELTA_ACTUAL_COLUMN = "ΔAₙ เงินจริง (USD)"


class DNAExhausted(RuntimeError):
    pass


class RowValidationError(RuntimeError):
    pass


class HoldingsAnomaly(RuntimeError):
    """The chain last saw a position; this snapshot says there is none."""


@dataclass(frozen=True)
class Anchor:
    version: int
    dna_step: int
    p0: float
    prev_price: float
    prev_actual: float
    # Real broker shares, never LEAPS: check_holdings_continuity compares it with
    # a broker reading, and the paper book is this number.
    prev_holdings: float | None = None
    # Persisted execution-terminal E. None denotes a legacy state that did not
    # store the frozen basis and therefore needs the old reconstruction path.
    prev_excess: float | None = None
    # LEAPS share-equivalents the chain last decided with. None: never used.
    # A different value now is a LEAPS rollover, recorded on the row it first
    # applies to; the ledger (P0, A, E) carries on, it is never rebased.
    prev_leaps_shares: float | None = None


@dataclass(frozen=True)
class Config:
    symbol: str
    fix_c: float
    diff: float = 0.0
    dna_code: str = "bypass:100"
    strategy_id: str = "shannon_demon_lego"
    decimal_precision: int = 5
    quantity_increment: float = 1.0
    # LEGO_LEAPS share-equivalents. Deliberately not part of lego_state.config_hash:
    # changing it continues the same chain instead of starting a new one.
    leaps_shares: float = 0.0

    def __post_init__(self):
        if not self.symbol or not str(self.symbol).strip():
            raise ValueError("symbol ต้องไม่ว่าง")
        if not (math.isfinite(self.fix_c) and self.fix_c > 0):
            raise ValueError("fix_c ต้อง finite และ > 0")
        if not (math.isfinite(self.diff) and self.diff >= 0):
            raise ValueError("diff ต้อง finite และ >= 0")
        if not (0 <= self.decimal_precision <= 5):
            raise ValueError("decimal_precision ต้อง 0..5")
        if not (math.isfinite(self.quantity_increment) and self.quantity_increment > 0):
            raise ValueError("quantity_increment ต้อง finite และ > 0")
        if (isinstance(self.leaps_shares, (bool, str))
                or not (math.isfinite(self.leaps_shares) and self.leaps_shares >= 0)):
            raise ValueError("leaps_shares ต้อง finite และ >= 0")
        object.__setattr__(self, "leaps_shares", float(self.leaps_shares) + 0.0)


def effective_holdings(real_holdings: float, leaps_shares: float = 0.0) -> float:
    """The position the strategy rebalances: real shares plus LEAPS share-equivalents.

    Without LEAPS this returns the real reading itself, the very same float, so
    a deployment that never sets LEGO_LEAPS computes bit for bit what it always
    did. With LEAPS the sum is taken in Decimal on each float's shortest repr:
    151.3228 + 100 is 251.3228, not a binary neighbour of it.
    """
    if not leaps_shares:
        return real_holdings
    return float(Decimal(str(real_holdings)) + Decimal(str(leaps_shares)))


def _shares_text(value: Decimal) -> str:
    """A share count for a human: at most 6 places, no trailing zeros."""
    text = format(value.quantize(Decimal("0.000001"), rounding=ROUND_DOWN), "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _sell_exceeds_real(gap: float, price: float, real_holdings: float, qty: float,
                       leaps_shares: float) -> str | None:
    """The PASS reason when a LEAPS SELL could only be met by every real share or more.

    LEAPS are not shares and cannot be sold as shares, so the sale has to come
    out of the real position. When the rebalance needs at least all of it — the
    exact quantity |gap| / P, or the quantity after rounding — the slot passes
    instead of selling what is there: a partial fill of the target is not the
    target, and selling the last real share would leave a zero position that
    check_holdings_continuity (by design) reads as a broker that lost the symbol.
    """
    need = Decimal(str(abs(gap))) / Decimal(str(price))
    held = Decimal(str(real_holdings))
    rounded = Decimal(str(qty))
    if need < held and rounded < held:
        return None
    return (f"{PASS_SELL_EXCEEDS_REAL}: ต้องขาย {_shares_text(max(need, rounded))} หุ้น "
            f"≥ หุ้นจริง {_shares_text(held)} หุ้น — LEAPS "
            f"{_shares_text(Decimal(str(leaps_shares)))} หุ้นจำลองขายเป็นหุ้นไม่ได้ "
            "จึงไม่ส่ง order (ไม่ตัดเหลือ)")


def position_vanished(prev_holdings: float | None, holdings: float) -> bool:
    """The predicate behind check_holdings_continuity, on plain numbers.

    Shared so the post-execution read in lego_order_worker applies exactly the
    same rule as the pre-decision one, rather than a second opinion about what
    'the position disappeared' means.
    """
    return prev_holdings is not None and prev_holdings > 0 and holdings == 0


def check_holdings_continuity(anchor: Anchor | None, holdings: float) -> None:
    """Refuse a snapshot that says the position vanished.

    _extract_qty returns 0.0 both when the account is genuinely flat and when a
    well-formed positions response simply does not mention the symbol. The two
    are indistinguishable at the adapter, and the second one is the worst
    possible input: value = 0 makes gap = fix_c, the largest order the strategy
    can ever produce, placed on top of a position we already hold. Nothing
    downstream catches it — the dispatch-time drift check compares two readings
    from the same source, preview passes because the cash is real, and the
    17-column ledger is theoretical and never reads filled quantity. A broker
    that keeps answering that way buys fix_c again every slot until buying power
    runs out.

    A rebalance can never produce it either: a SELL targets value fix_c, so it
    leaves fix_c/price > 0 shares behind. Holdings reaching exactly zero while
    the chain remembers a position is therefore not a market event, and only
    that case is refused — a partial drop is ordinary and stays silent.

    Both numbers are REAL shares, with LEAPS or without. Adding LEAPS here would
    disarm the check exactly when it matters: the effective position never reads
    0 while LEAPS are held, so a response that lost the symbol would pass as a
    partial drop. With LEAPS a SELL that would take the real position to 0
    becomes PASS_SELL_EXCEEDS_REAL instead, so the rule above still holds.
    """
    if anchor is None:
        return                       # genesis: no reference to contradict
    if position_vanished(anchor.prev_holdings, holdings):
        raise HoldingsAnomaly(
            f"chain เคยถือ {anchor.prev_holdings} หุ้น แต่ snapshot นี้อ่านได้ 0 — "
            "อาจเป็น positions response ที่ไม่ครบ ไม่ใช่การถือ 0 จริง จึงไม่ commit")


def dna_step_for(anchor: Anchor | None, explicit_step: int | None = None) -> int:
    if explicit_step is not None:
        if type(explicit_step) is not int or explicit_step < 0:
            raise ValueError("explicit DNA step ต้องเป็นจำนวนเต็ม >= 0")
        return explicit_step
    return 0 if anchor is None else anchor.dna_step + 1


def dna_signal_for(dna_code: str, step: int) -> int:
    dna = decode_dna(dna_code)
    if step >= len(dna):
        raise DNAExhausted(f"DNA exhausted: step={step} len={len(dna)}")
    return int(dna[step])


def dna_steps_remaining(dna_code: str, step: int) -> int:
    """Slots this dna_code can still serve after *step*.

    Running out is not a fault — it is the DNA finishing — but it arrives as a
    hard stop with no warning: a 100-step code on a 30m grid lasts about eight
    trading days, and the first sign is the row that cannot be built. decode_dna
    is cached, so the count is free on every row and lets the response warn while
    there is still time to extend the code.
    """
    return max(0, len(decode_dna(dna_code)) - int(step) - 1)


@dataclass(frozen=True)
class Decision:
    status: str
    action: str
    side: str
    reason: str
    quantity: float
    value: float
    gap: float

    @property
    def acted(self) -> bool:
        return self.status in (READY_BUY, READY_SELL) and self.quantity > 0


def build_decision(cfg: Config, price: float, holdings: float, signal: int) -> Decision:
    """One slot's decision from the quote and the broker's REAL shares.

    ``holdings`` is what the broker says the account holds (option rows are not
    shares, see webull_io._extract_qty). LEAPS share-equivalents are added here,
    before anything is computed, so the decision row and every dispatch-time
    re-check (execution_service._dispatch_quote_safety) value the same position:
    value and gap use real + LEAPS. A SELL can only come out of the real shares:
    its ceiling is the real holdings, and a SELL that needs at least all of them
    is PASS_SELL_EXCEEDS_REAL rather than a truncated order.
    """
    if not (math.isfinite(price) and price > 0):
        raise ValueError("price (Pₙ) ต้อง finite และ > 0")
    if not (math.isfinite(holdings) and holdings >= 0):
        raise ValueError("holdings ต้อง finite และ >= 0")
    if signal not in (0, 1):
        raise ValueError("signal ต้อง ∈ {0,1}")
    position = effective_holdings(holdings, cfg.leaps_shares)
    value = position * price
    gap = value - cfg.fix_c
    if signal == 0:
        return Decision(PASS_DNA_ZERO, "PASS", "", PASS_DNA_ZERO, 0.0, value, gap)
    if abs(gap) <= cfg.diff:
        return Decision(PASS_THRESHOLD, "PASS", "", PASS_THRESHOLD, 0.0, value, gap)
    if cfg.strategy_id.endswith("_v2"):
        raw = Decimal(str(abs(gap))) / Decimal(str(price))
        increment = Decimal(str(cfg.quantity_increment))
        qty = float((raw / increment).to_integral_value(rounding=ROUND_DOWN) * increment)
    else:
        qty = round(abs(gap) / price, cfg.decimal_precision)
    if gap > cfg.diff:
        if cfg.leaps_shares:
            # With LEAPS the exact rebalance can exceed the real shares (LEAPS ×
            # price alone may be above FIX_C), and LEAPS cannot be sold as shares.
            exceeded = _sell_exceeds_real(gap, price, holdings, qty, cfg.leaps_shares)
            if exceeded is not None:
                return Decision(PASS_SELL_EXCEEDS_REAL, "PASS", "", exceeded, 0.0,
                                value, gap)
        # The exact rebalance is below holdings because FIX_C > 0, but rounding
        # can push a tiny fractional SELL above holdings. Cap at holdings rounded
        # down to the same broker precision; never round this ceiling upward.
        # `holdings` is the real position: with LEAPS too, the ceiling is real.
        quantum = Decimal(1).scaleb(-cfg.decimal_precision)
        sell_ceiling = float(Decimal(str(holdings)).quantize(
            quantum, rounding=ROUND_DOWN))
        qty = min(qty, sell_ceiling)
    if cfg.strategy_id.endswith("_v2") and qty > 0 and qty < 1.0:
        if (Decimal(str(qty)) * Decimal(str(price))) < Decimal("1.0"):
            qty = 0.0
    if qty <= 0:
        reason = PASS_MIN_ORDER if cfg.strategy_id.endswith("_v2") else PASS_THRESHOLD
        return Decision(reason, "PASS", "", reason, 0.0, value, gap)
    if gap > cfg.diff:
        return Decision(READY_SELL, "TRIGGER_ACTION", "SELL", READY_SELL, qty, value, gap)
    return Decision(READY_BUY, "TRIGGER_ACTION", "BUY", READY_BUY, qty, value, gap)


@dataclass(frozen=True)
class Recurrence:
    R: float
    dA: float
    A: float
    E: float
    acted_price_next: float


def compute_recurrence(cfg: Config, price: float, anchor: Anchor | None,
                       acted: bool | None = None, *, signal: int | None = None) -> Recurrence:
    if acted is None:
        if signal not in (0, 1):
            raise ValueError("ต้องระบุ acted bool หรือ signal ∈ {0,1}")
        acted = bool(signal)
    if type(acted) is not bool:
        raise ValueError("acted ต้องเป็น bool")
    if not (math.isfinite(price) and price > 0):
        raise ValueError("price ต้อง finite และ > 0")
    if anchor is None:
        return Recurrence(0.0, 0.0, 0.0, 0.0, float(price))
    if not (anchor.p0 > 0 and anchor.prev_price > 0
            and math.isfinite(anchor.p0) and math.isfinite(anchor.prev_price)
            and math.isfinite(anchor.prev_actual)):
        raise ValueError("anchor recurrence values ต้อง finite และ price > 0")
    R = cfg.fix_c * math.log(price / anchor.p0)
    if acted:
        dA = cfg.fix_c * (price / anchor.prev_price - 1.0)
        A = anchor.prev_actual + dA
        return Recurrence(R, dA, A, A - R, float(price))
    A = anchor.prev_actual
    if cfg.strategy_id.endswith("_v2") and anchor.prev_excess is not None:
        frozen_E = float(anchor.prev_excess)
    else:
        # Compatibility only for a legacy state. V2 persists E explicitly,
        # because fill slippage means it cannot be reconstructed from P_acted.
        R_acted = cfg.fix_c * math.log(anchor.prev_price / anchor.p0)
        frozen_E = A - R_acted
    return Recurrence(R, 0.0, A, frozen_E, float(anchor.prev_price))


@dataclass(frozen=True)
class ExecutionFill:
    """The broker-confirmed facts, and the only inputs allowed to move Aₙ.

    `filled_quantity` is cumulative for one client_order_id, so a partial fill
    and the poll that observes it again carry the same number; `holdings_after`
    is read back from the broker, never derived from the ordered quantity.
    """
    filled_price: float
    filled_quantity: float
    # The ledger's position after the fill, the unit of column 7: real shares
    # plus the row's LEAPS share-equivalents (just the real shares without LEAPS).
    holdings_after: float
    # LEAPS only: the broker's real reading behind holdings_after, and the LEAPS
    # added to it. None means no LEAPS, and holdings_after is the real reading.
    real_holdings_after: float | None = None
    leaps_shares: float = 0.0

    @property
    def acted(self) -> bool:
        """Shares moved. No status, and no READY_*, can substitute for this."""
        return (math.isfinite(self.filled_quantity) and self.filled_quantity > 0
                and math.isfinite(self.filled_price) and self.filled_price > 0)

    @property
    def broker_holdings_after(self) -> float:
        """Real shares after the fill: what the chain's prev_holdings must hold."""
        return (self.holdings_after if self.real_holdings_after is None
                else self.real_holdings_after)

    @classmethod
    def from_broker(cls, filled_price: float, filled_quantity: float,
                    real_holdings_after: float,
                    leaps_shares: float = 0.0) -> "ExecutionFill":
        """A fill from the broker's real post-fill reading and the row's LEAPS.

        Without LEAPS this is exactly ExecutionFill(price, quantity, real), the
        object every fill has always been. With LEAPS holdings_after becomes
        real + LEAPS so it matches the ledger, and the real reading is kept.
        """
        if not leaps_shares:
            return cls(filled_price=filled_price, filled_quantity=filled_quantity,
                       holdings_after=real_holdings_after)
        if isinstance(leaps_shares, bool) or not (
                math.isfinite(leaps_shares) and leaps_shares > 0):
            raise ValueError("leaps_shares ต้อง finite และ >= 0")
        if not (math.isfinite(real_holdings_after) and real_holdings_after >= 0):
            raise ValueError("holdings หลัง fill ต้อง finite และ >= 0")
        return cls(filled_price=filled_price, filled_quantity=filled_quantity,
                   holdings_after=effective_holdings(real_holdings_after, leaps_shares),
                   real_holdings_after=float(real_holdings_after),
                   leaps_shares=float(leaps_shares))


@dataclass(frozen=True)
class Finalization:
    dA: float
    A: float
    E: float
    acted_price_next: float


def finalize_recurrence(cfg: Config, fill: ExecutionFill, *,
                        last_action_price: float, actual_cumulative: float,
                        reference_R: float, initial_funding: bool = False) -> Finalization:
    """ΔAₙ/Aₙ/Eₙ for a row whose order the broker confirmed as filled.

    Identical arithmetic to `compute_recurrence`'s act branch, with the executed
    price in place of the decision price: the decision price is what the engine
    saw when it chose, and using it here would book a cashflow the account never
    experienced. reference_R is the row's committed market reference less any
    explicit funding-origin offset supplied by the state layer. The row's
    market-reference column itself is never rewritten here.

    One consequence worth naming: Aₙ is now built from executed prices while Rₙ
    is still built from quoted ones, so `Eₙ = Aₙ − Rₙ ≥ 0` — which holds exactly
    when both walk the same price path — now holds up to execution slippage and
    can sit slightly below zero after an unlucky fill. That is the difference
    being measured, not an error in it. This remains a FIX_C-scaled model;
    actual share quantities and fees belong to the separate broker ledger.
    For an explicitly identified initial funding
    BUY, there was no invested price interval: seed P_acted from the actual
    fill and initialize dA/A/E to zero. The state layer identifies that case;
    neither a zero A nor a DNA step is sufficient evidence on its own.
    """
    if not fill.acted:
        raise ValueError(
            "finalize ต้องมี fill จริง: filled_quantity > 0 และ filled_price > 0")
    if not (math.isfinite(last_action_price) and last_action_price > 0):
        raise ValueError("last_action_price (P_acted) ต้อง finite และ > 0")
    if not (math.isfinite(actual_cumulative) and math.isfinite(reference_R)):
        raise ValueError("actual_cumulative และ Rₙ ต้อง finite")
    if not (math.isfinite(fill.holdings_after) and fill.holdings_after >= 0):
        raise ValueError("holdings หลัง fill ต้อง finite และ >= 0")
    price = float(fill.filled_price)
    if type(initial_funding) is not bool:
        raise ValueError("initial_funding must be bool")
    if initial_funding:
        if actual_cumulative != 0 or reference_R != 0:
            raise ValueError("initial funding requires a zero model baseline")
        return Finalization(0.0, 0.0, 0.0, price)
    dA = cfg.fix_c * (price / last_action_price - 1.0)
    A = actual_cumulative + dA
    return Finalization(dA, A, A - reference_R, price)


def compute_row(cfg: Config, snapshot: dict, anchor: Anchor | None,
                dna_step: int | None = None) -> dict:
    step = dna_step_for(anchor, dna_step)
    signal = dna_signal_for(cfg.dna_code, step)
    price = float(snapshot["price"])
    # snapshot -> holdings. The snapshot carries the broker's real shares (the
    # paper position in paper mode) and keeps carrying them: run_id, the intent's
    # decision_holdings and every guard read that number. Column 7 is the
    # position the decision used, real + LEAPS; build_decision adds the LEAPS
    # itself from the real reading.
    real_holdings = float(snapshot.get("holdings", 0.0) or 0.0)
    holdings = effective_holdings(real_holdings, cfg.leaps_shares)
    dec = build_decision(cfg, price, real_holdings, signal)
    # acted=False on every row, including READY_BUY/READY_SELL. A decision is
    # not an execution: the order may be suppressed, expire unsent, be rejected,
    # or fill at another price entirely, and each of those would leave a booked
    # ΔAₙ that never happened. The engine therefore commits the carried-forward
    # ledger and lego_order_worker finalizes it against the broker's fill.
    rec = compute_recurrence(cfg, price, anchor, acted=False)
    row = {
        "เวลา (UTC)": snapshot["captured_at"],
        "สินทรัพย์": cfg.symbol,
        "สถานะ": dec.status,
        "DNA step": step,
        "DNA signal": signal,
        "ราคา Pₙ (USD)": price,
        "จำนวนถือครอง (หุ้น)": holdings,
        "คำสั่ง": dec.action,
        "ฝั่ง": dec.side,
        "เหตุผล": dec.reason,
        "จำนวนสั่ง (หุ้น)": dec.quantity,
        "มูลค่าพอร์ต (USD)": dec.value,
        "ส่วนต่างเป้าหมาย (USD)": dec.gap,
        "Rₙ อ้างอิง (USD)": rec.R,
        "ΔAₙ ต่อสเต็ป (USD)": rec.dA,
        "Aₙ สะสม (USD)": rec.A,
        "Eₙ ส่วนเกินสะสม (USD)": rec.E,
    }
    validate_row_columns(row)
    row["_meta"] = {
        "step": step,
        "price": price,
        "p0_next": anchor.p0 if anchor else price,
        # The decision's own verdict, unchanged: it is what decides whether an
        # order intent is created. It no longer decides the cashflow columns.
        "acted": dec.acted,
        # Carried forward, never advanced here. They seed the execution cashflow
        # at genesis and are the values a PASS row keeps forever.
        "acted_price_next": rec.acted_price_next,
        "actual_next": rec.A,
        # True while the row is waiting for a broker fill to finalize ΔAₙ/Aₙ/Eₙ.
        "execution_pending": dec.acted,
        "status": dec.status,
        "side": dec.side,
        "quantity": dec.quantity,
        "action": dec.action,
    }
    if cfg.strategy_id.endswith("_v2"):
        row["_meta"].update({
            "excess_next": rec.E,
            "e_mark": rec.A - rec.R,
        })
    # LEAPS: the split behind column 7, only on a chain that uses (or just
    # stopped using) LEAPS, so a chain without them keeps its exact _meta.
    prior_leaps = None if anchor is None else anchor.prev_leaps_shares
    if cfg.leaps_shares or prior_leaps:
        row["_meta"].update({
            "real_holdings": real_holdings,
            "leaps_shares": cfg.leaps_shares,
            "effective_holdings": holdings,
        })
    if anchor is not None and float(prior_leaps or 0.0) != cfg.leaps_shares:
        # A LEAPS rollover: the new base applies from this row on. Recorded,
        # never silent; P0, Aₙ and Eₙ carry on unchanged (no rebase).
        row["_meta"]["leaps_shares_changed_from"] = float(prior_leaps or 0.0)
    return row


def validate_row_columns(row: dict) -> None:
    keys = [k for k in row.keys() if k != "_meta"]
    if keys != COLUMN_ORDER:
        raise RowValidationError(
            f"คอลัมน์ไม่ตรงสัญญา: got {len(keys)} / need {len(COLUMN_ORDER)} (ลำดับตายตัว)")


def columns_presented(row: dict) -> dict:
    money = {6, 12, 13, 14, 15, 16, 17}
    out = {}
    for i, k in enumerate(COLUMN_ORDER, start=1):
        v = row[k]
        out[k] = round(v, 2) if (i in money and isinstance(v, (int, float))) else v
    return out
