"""Fee against volatility harvesting for constant-dollar rebalancing: the table of docs/AUDIT_20261010_TH.md.

A model, not a forecast. FIX_C dollars of stock are held; at each 15-minute slot (26 a day, 252 days
a year) the whole gap is traded at the slot price when |value - FIX_C| > DIFF, and the broker takes
FEE of the traded notional. No slippage, no bid/ask, quantities are not floored, no overnight gap,
no tax, no mean reversion beyond what the two worlds below imply. The same random paths are used for
every fee and every DIFF, so differences between rows are not sampling noise.

  median-flat  the log price has no drift: the median path goes nowhere and the arithmetic mean
               return is sigma^2 / 2, which is all the "volatility harvest" a constant-dollar
               holder can have
  martingale   the price has no arithmetic drift (E[P_t] = P_0): holding FIX_C earns nothing

Reported per world: USD a year (mean over PATHS paths, standard error in brackets) against nothing,
and the same figure for simply holding FIX_C dollars of the stock (buy-and-hold). The comparison that
matters is each row against the f=0.00% rows: the strategy's gross result is the same as buy-and-hold
in both worlds (it is a constant-dollar position), so the fee is a cost and not a test.

Needs numpy only:  python fee-drag-sim.py [sigma_annual]     (about half a minute)
"""
import sys

import numpy as np

FIX_C = 10000.0
SLOTS_PER_YEAR = 252 * 26
PATHS = 1500
FEES = (0.0, 0.0015, 0.005, 0.0107)      # 0.15% and 0.5% are guesses for PROD; 1.07% is what UAT charged
DIFFS = (25.0, 100.0, 300.0)             # 25 is the configured LEGO_DIFF
SEED = 20261010


def paths(sigma, world, normals):
    step = sigma / np.sqrt(SLOTS_PER_YEAR)
    drift = 0.0 if world == "median-flat" else -0.5 * step * step
    prices = 100.0 * np.exp(np.cumsum(drift + step * normals, axis=0))
    return np.vstack([np.full((1, normals.shape[1]), 100.0), prices])


def run(prices, diff, fee):
    held = np.full(prices.shape[1], FIX_C / prices[0, 0])
    cash = np.zeros(prices.shape[1])
    trades = np.zeros(prices.shape[1])
    for price in prices[1:]:
        gap = held * price - FIX_C
        act = np.abs(gap) > diff
        traded = np.where(act, gap, 0.0)
        cash += traded - fee * np.abs(traded)
        held -= traded / price
        trades += act
    return cash + held * prices[-1] - FIX_C, trades


def main(sigma):
    normals = np.random.default_rng(SEED).standard_normal((SLOTS_PER_YEAR, PATHS))
    print(f"sigma_annual={sigma}  paths={PATHS}  slots/year={SLOTS_PER_YEAR}  FIX_C={FIX_C:.0f}  seed={SEED}")
    for world in ("median-flat", "martingale"):
        prices = paths(sigma, world, normals)
        hold = (FIX_C / prices[0, 0]) * (prices[-1] - prices[0])
        print(f"\nworld {world}: buy-and-hold {hold.mean():+8.1f} USD/yr ({hold.std() / np.sqrt(PATHS):.1f})")
        for fee in FEES:
            print(f"  fee {fee * 100:5.2f}% of notional per order")
            for diff in DIFFS:
                net, trades = run(prices, diff, fee)
                print(f"    DIFF {diff:6.0f}: net {net.mean():+9.1f} ({net.std() / np.sqrt(PATHS):4.1f}) USD/yr"
                      f"   orders {trades.mean():7.1f}/yr")


if __name__ == "__main__":
    main(float(sys.argv[1]) if len(sys.argv) > 1 else 0.35)
