"""Pure execution-terminal-frozen-v2 model and broker cashflow arithmetic."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, localcontext


D = Decimal
SEMANTICS = "execution_terminal_funding_v3"
SCHEMA_VERSION = 2
FUNDING_BASELINE_POLICY = "initial_funding_zero_v1"


def decimal(value: object, *, name: str, positive: bool | None = None) -> Decimal:
    try:
        number = D(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{name} ต้องเป็น decimal") from exc
    if not number.is_finite():
        raise ValueError(f"{name} ต้อง finite")
    if positive is True and number <= 0:
        raise ValueError(f"{name} ต้อง > 0")
    if positive is False and number < 0:
        raise ValueError(f"{name} ต้อง >= 0")
    return number


def r_market(principal: object, price: object, p0: object) -> Decimal:
    principal_d = decimal(principal, name="principal", positive=True)
    price_d = decimal(price, name="price", positive=True)
    p0_d = decimal(p0, name="p0", positive=True)
    with localcontext() as ctx:
        ctx.prec = 34
        return principal_d * (price_d / p0_d).ln()


@dataclass(frozen=True)
class FrozenLedger:
    A: Decimal
    p_acted: Decimal
    E: Decimal
    r_basis: Decimal
    finalized_seq: int = 0

    @classmethod
    def genesis(cls, price: object) -> "FrozenLedger":
        p0 = decimal(price, name="genesis price", positive=True)
        return cls(D("0"), p0, D("0"), D("0"), 0)

    def observe(self, principal: object, price: object, p0: object) -> dict:
        mark_r = r_market(principal, price, p0)
        return {
            "delta_A": D("0"),
            "A": self.A,
            "p_acted": self.p_acted,
            "E": self.E,
            "R_market": mark_r,
            "E_mark": self.A - mark_r,
            "finalized_seq": self.finalized_seq,
        }

    def finalize_terminal_fill(
        self, *, principal: object, fill_price: object, decision_r_basis: object,
        initial_funding: bool = False,
    ) -> tuple["FrozenLedger", dict]:
        principal_d = decimal(principal, name="principal", positive=True)
        fill_d = decimal(fill_price, name="fill_price", positive=True)
        basis_d = decimal(decision_r_basis, name="decision_r_basis")
        if type(initial_funding) is not bool:
            raise ValueError("initial_funding must be bool")
        if initial_funding and (self.finalized_seq != 0 or self.A != 0 or basis_d != 0):
            raise ValueError("initial funding requires an unfinalized zero baseline")
        delta = principal_d * (fill_d / self.p_acted - D("1"))
        if initial_funding:
            delta = D("0")
        next_A = self.A + delta
        next_ledger = FrozenLedger(
            A=next_A,
            p_acted=fill_d,
            E=next_A - basis_d,
            r_basis=basis_d,
            finalized_seq=self.finalized_seq + 1,
        )
        return next_ledger, {
            "delta_A": delta,
            "A": next_ledger.A,
            "p_acted": next_ledger.p_acted,
            "R_basis": basis_d,
            "E": next_ledger.E,
            "finalized_seq": next_ledger.finalized_seq,
        }


@dataclass(frozen=True)
class BrokerCashflow:
    cumulative_quantity: Decimal = D("0")
    cumulative_notional: Decimal = D("0")
    actual_fees: Decimal | None = None
    cash_delta: Decimal = D("0")

    def apply_cumulative(
        self, *, side: str, quantity: object, average_price: object,
        actual_fees: object | None = None,
    ) -> tuple["BrokerCashflow", dict]:
        side = str(side).upper()
        if side not in {"BUY", "SELL"}:
            raise ValueError("side ต้องเป็น BUY หรือ SELL")
        qty = decimal(quantity, name="cumulative quantity", positive=False)
        price = decimal(average_price, name="cumulative average price", positive=True)
        notional = qty * price
        if qty < self.cumulative_quantity or notional < self.cumulative_notional:
            raise ValueError("broker cumulative fill ลดลง; ต้อง reconciliation")
        fees = (
            None if actual_fees is None
            else decimal(actual_fees, name="actual fees", positive=False)
        )
        if fees is not None and self.actual_fees is not None and fees < self.actual_fees:
            raise ValueError("broker cumulative fee ลดลง; ต้อง reconciliation")
        delta_qty = qty - self.cumulative_quantity
        delta_notional = notional - self.cumulative_notional
        delta_fee = D("0")
        if fees is not None:
            delta_fee = fees - (self.actual_fees or D("0"))
        sign = D("-1") if side == "BUY" else D("1")
        delta_cash = sign * delta_notional - delta_fee
        updated = BrokerCashflow(
            cumulative_quantity=qty,
            cumulative_notional=notional,
            actual_fees=fees if fees is not None else self.actual_fees,
            cash_delta=self.cash_delta + delta_cash,
        )
        return updated, {
            "delta_quantity": delta_qty,
            "delta_notional": delta_notional,
            "delta_fee": delta_fee,
            "broker_cash_delta": delta_cash,
            "cash_cumulative": updated.cash_delta,
        }
