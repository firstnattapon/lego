"""Reproduces the fee-versus-premium table of docs/AUDIT_20261006_TH.md.

A model, not a forecast: constant-dollar rebalancing (FIX_C 10,000, DIFF 25, 26 slots a day,
quantity floored to 0.01) of a log price that is AR(1) with the measured per-slot return std,
reported as USD a day in excess of buy-and-hold. Needs numpy only: python fee-model.py
"""
import math

import numpy as np

SIGMA = 0.001889   # std of the 17 slot-to-slot log returns of the 18 rows committed on 2026-10-05
FIX_C, DIFF, INCREMENT, SLOTS = 10000.0, 25.0, 0.01, 26
UAT_FEE = 0.0108   # 1.50 on 139.24 and 1.14 on 106.15 notional (UAT, 2026-10-05)


def simulate(phi, fee, days=4000, seed=7):
    rng = np.random.default_rng(seed)
    n = days * SLOTS
    if phi >= 1:   # arithmetic martingale: the convexity drift is removed, so no premium exists
        x = np.cumsum(np.concatenate([[0.0], rng.normal(0.0, SIGMA, n) - 0.5 * SIGMA ** 2]))
    else:          # innovation scaled so the one-slot return std stays SIGMA
        step = SIGMA / math.sqrt(2.0 / (1.0 + phi))
        eps = rng.normal(0.0, step, n)
        x = np.zeros(n + 1)
        for t in range(n):
            x[t + 1] = phi * x[t] + eps[t]
    price = 70.0 * np.exp(x)
    held, cash, traded, trades = FIX_C / price[0], 0.0, 0.0, 0
    for p in price[1:]:
        gap = held * p - FIX_C
        if abs(gap) <= DIFF:
            continue
        quantity = math.floor(abs(gap) / p / INCREMENT) * INCREMENT
        if quantity <= 0:
            continue
        notional = quantity * p
        held, cash = (held - quantity, cash + notional * (1 - fee)) if gap > 0 else (
            held + quantity, cash - notional * (1 + fee))
        traded, trades = traded + notional, trades + 1
    excess = cash + held * price[-1] - FIX_C - (FIX_C / price[0]) * (price[-1] - price[0])
    return excess / days, traded / days, trades / days


def averaged(phi, fee, seeds=(1, 2, 3, 4, 5)):
    runs = np.array([simulate(phi, fee, seed=seed) for seed in seeds])
    return runs[:, 0].mean(), runs[:, 0].std(ddof=1) / math.sqrt(len(seeds)), runs[:, 1].mean(), runs[:, 2].mean()


if __name__ == "__main__":
    print("USD/day in excess of buy-and-hold; mean +- standard error over 5 seeds x 4000 days")
    for phi in (1.0, 0.95, 0.90, 0.80):
        gross, error, traded, trades = averaged(phi, 0.0)
        row = f"phi={phi:4.2f}  gross {gross:+.3f} +-{error:.3f}  traded {traded:5.1f}/day  trades {trades:3.1f}/day"
        for fee in (0.0025, UAT_FEE):
            row += f"  | fee {fee * 100:.2f}%: {averaged(phi, fee)[0]:+.3f}"
        print(row + f"  | break-even fee {100 * gross / traded:.3f}%")
