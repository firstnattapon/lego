#!/usr/bin/env bash
# Continuous UAT release (UBER, FIX_C=10000). Run `python ops.py release-plan
# --env-file deploy/uat-continuous.env.example --window-sessions 10 --reference-price <price>`
# first: it prints the three values required below for the release you mean to deploy.
set -Eeuo pipefail
: "${EXPECTED_CANDIDATE_HASH:?Exact tested candidate required}"
: "${LEGO_RELEASE_AUTHORIZATION_OVERRIDE:?v4 release authorization required}"
: "${LEGO_TRADING_WINDOW_END_OVERRIDE:?New explicit approval expiry required}"
export WEBULL_ENV_OVERRIDE=UAT LEGO_MODE_OVERRIDE=trade LEGO_ACTIVE_OVERRIDE=true
export LEGO_SYMBOL_OVERRIDE=UBER LEGO_FIX_C_OVERRIDE=10000 LEGO_DIFF_OVERRIDE=25
export LEGO_ALLOW_FRACTIONAL_OVERRIDE=true LEGO_SESSION_KEY_MODE_OVERRIDE=market_day
export LEGO_DNA_BUNDLE_OVERRIDE="${LEGO_DNA_BUNDLE_OVERRIDE:-strategy.uat-continuous.json}"
export LEGO_STALE_ORDER_ACTION_OVERRIDE=cancel LEGO_STALE_ORDER_SECONDS_OVERRIDE=300
export LEGO_CANCEL_CONFIRM_GRACE_SECONDS_OVERRIDE=120
# Caps for FIX_C=10000: 15% of principal in notional, ~1.3x that in shares, one
# order per 15-minute slot (docs/AUDIT_20261005_TH.md). The authorization above is
# bound to these exact values, so override them only together with release-plan.
export LEGO_MAX_SESSION_ORDERS_OVERRIDE="${LEGO_MAX_SESSION_ORDERS_OVERRIDE:-26}"
export LEGO_MAX_ORDER_QUANTITY_OVERRIDE="${LEGO_MAX_ORDER_QUANTITY_OVERRIDE:-30}"
export LEGO_MAX_ORDER_NOTIONAL_USD_OVERRIDE="${LEGO_MAX_ORDER_NOTIONAL_USD_OVERRIDE:-1500}"
export LEGO_SCHEDULE_OVERRIDE='* * * * *'
exec bash deploy/cloudshell-all-in-one.sh
