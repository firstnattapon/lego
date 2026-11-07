#!/usr/bin/env bash
# Every PROD real-money bot in one Cloud Shell run, after the close (docs/PROD_LIVE_RUNBOOK_TH.md 19):
#
#   cd ~/lego-live && git pull --ff-only
#   LEGO_ALERTS_CONFIRMED=yes LEGO_FEE_PCT_OVERRIDE=0.35 bash deploy/cloudshell-prod-live-all.sh
#   LEGO_ALERTS_CONFIRMED=yes LEGO_FEE_PCT_OVERRIDE=0.35 bash deploy/cloudshell-prod-live-all.sh RGTI
#                                                                           # only the bots named
#
# It runs deploy/cloudshell-prod-live.sh (the prefunded canary) once per bot of BOTS, one after
# another, with the values of its row. Every gate of that script judges each bot on its own:
# token, database, bot inventory, account preflight, release-plan --enforce, deploy. A bot a gate
# refuses before its deploy keeps the release it runs now; after the deploy step its own section
# says what runs. Either way the next bot still goes, and the table at the end says which
# deployed and gives the command for the ones left.
#
# BOTS holds the money of every bot. Change it with a commit: a real-money release deploys a
# clean, committed checkout, so what is deployed is what git holds. Nothing else of the shell
# reaches a bot: every *_OVERRIDE an earlier command left there is dropped, except
# LEGO_FEE_PCT_OVERRIDE and ALERT_WEBHOOK_SECRET_OVERRIDE, which are the same for every bot.
# The one-off funding release of a new bot (LEGO_FUNDING_MODE_OVERRIDE=initial-funding) and
# retiring a symbol stay with deploy/cloudshell-prod-live.sh itself (runbook 16-17).
set -Eeuo pipefail

# SYMBOL  FIX_C  DIFF  LEAPS  FRACTIONAL  BOT  LEAPS_CONFIRMED
#   BOT              - = the first bot (function lego-tick-prod), otherwise lego-tick-prod-<BOT>
#   LEAPS_CONFIRMED  yes = the owner confirms LEAPS the positions do not show (LEGO_LEAPS_CONFIRMED);
#                    no  = the preflight must see them
readonly BOTS=(
    "QBTS  1500  50  100  true  -     no"
    "RGTI  1500  50  100  true  rgti  no"
    "USAR  1500  50  100  true  usar  yes"
)
readonly PASS_THROUGH=(LEGO_FEE_PCT_OVERRIDE ALERT_WEBHOOK_SECRET_OVERRIDE)

fail() {
    echo "ERROR: $*" >&2
    exit 1
}

is_number() {
    [[ "$1" =~ ^[0-9]+([.][0-9]+)?$ ]]
}

positive() {
    is_number "$1" && awk -v v="$1" 'BEGIN { exit !(v > 0) }'
}

function_of() {  # as deploy/cloudshell-prod-live.sh and tools/bot_inventory.py name it
    if [[ "$1" == - ]]; then echo "lego-tick-prod"; else echo "lego-tick-prod-$1"; fi
}

[[ -f deploy/cloudshell-prod-live.sh && -f market_clock.py ]] || fail "run from the repository root"

# ------------------------------------------------------------------ the table, checked whole
declare -A SYMBOLS=() FUNCTIONS=()
ALL_SYMBOLS=()
for row in "${BOTS[@]}"; do
    read -r symbol fix_c diff leaps fractional bot confirmed extra <<<"${row}"
    [[ -n "${confirmed:-}" && -z "${extra:-}" ]] || fail "BOTS row '${row}' needs 7 values"
    [[ "${symbol}" =~ ^[A-Z][A-Z0-9.-]{0,9}$ ]] || fail "BOTS ${symbol}: the symbol must be an upper-case ticker"
    positive "${fix_c}" || fail "BOTS ${symbol}: FIX_C must be a number > 0 (USD, no comma)"
    positive "${diff}" || fail "BOTS ${symbol}: DIFF must be a number > 0 (USD)"
    is_number "${leaps}" || fail "BOTS ${symbol}: LEAPS must be a number >= 0 (share-equivalents)"
    [[ "${fractional}" == true || "${fractional}" == false ]] || fail "BOTS ${symbol}: FRACTIONAL must be true or false"
    [[ "${bot}" == - || "${bot}" =~ ^[a-z][a-z0-9]{0,15}$ ]] || \
        fail "BOTS ${symbol}: BOT must be - or 1-16 lower-case letters and digits, starting with a letter"
    [[ "${confirmed}" == yes || "${confirmed}" == no ]] || fail "BOTS ${symbol}: LEAPS_CONFIRMED must be yes or no"
    target="$(function_of "${bot}")"
    [[ -z "${SYMBOLS[${symbol}]:-}" ]] || fail "BOTS lists ${symbol} twice: two bots would rebalance the same shares"
    [[ -z "${FUNCTIONS[${target}]:-}" ]] || fail "BOTS sends two symbols to ${target}"
    SYMBOLS[${symbol}]=1
    FUNCTIONS[${target}]=1
    ALL_SYMBOLS+=("${symbol}")
done

# The bots asked for, in the order of the table; none named = all of them.
for wanted in "$@"; do
    [[ -n "${wanted}" && -n "${SYMBOLS[${wanted}]:-}" ]] || fail "${wanted} is not a symbol of BOTS: ${ALL_SYMBOLS[*]}"
done
SELECTED=()
for row in "${BOTS[@]}"; do
    read -r symbol _ <<<"${row}"
    if (( $# == 0 )) || [[ " $* " == *" ${symbol} "* ]]; then
        SELECTED+=("${row}")
    fi
done

# ------------------------------------------------- the local gates every bot would fail alike
[[ -z "$(git status --porcelain --untracked-files=all)" ]] || \
    fail "commit or stash local changes first: a real-money release deploys a clean, committed checkout"
[[ "${LEGO_ALERTS_CONFIRMED:-}" == yes ]] || \
    fail "G4: alerts must reach a person before real money (docs/PROD_LIVE_RUNBOOK_TH.md 16); once a drill reached a person, run again with LEGO_ALERTS_CONFIRMED=yes"
[[ -z "${WEBULL_API_DEBUG:-}" ]] || \
    fail "unset WEBULL_API_DEBUG: on PROD the SDK would log full request and response bodies"
SESSION_STATUS=0
python3 -c 'import sys; from market_clock import is_regular_session; sys.exit(3 if is_regular_session() else 0)' || \
    SESSION_STATUS=$?
(( SESSION_STATUS != 3 )) || \
    fail "a real-money release is refused during the regular session (09:30-16:00 New York); run it after the close"
(( SESSION_STATUS == 0 )) || fail "python3 could not read the market clock (market_clock.py)"

# ------------------------------------------------------------------------------ the bots
run_bot() {  # SYMBOL FIX_C DIFF LEAPS FRACTIONAL BOT LEAPS_CONFIRMED
    (
        while IFS= read -r name; do
            keep=0
            for pass in "${PASS_THROUGH[@]}"; do
                [[ "${name}" != "${pass}" ]] || keep=1
            done
            (( keep )) || unset "${name}"
        done < <(compgen -e | grep -E '_OVERRIDE$|^EXPECTED_CANDIDATE_HASH$' || true)
        unset LEGO_LEAPS_CONFIRMED
        export LEGO_SYMBOL_OVERRIDE="$1" LEGO_FIX_C_OVERRIDE="$2" LEGO_DIFF_OVERRIDE="$3" \
               LEGO_LEAPS_OVERRIDE="$4" LEGO_ALLOW_FRACTIONAL_OVERRIDE="$5"
        [[ "$6" == - ]] || export LEGO_BOT_OVERRIDE="$6"
        [[ "$7" != yes ]] || export LEGO_LEAPS_CONFIRMED=yes
        exec bash deploy/cloudshell-prod-live.sh
    )
}

RESULTS=()
LEFT=()
index=0
for row in "${SELECTED[@]}"; do
    index=$(( index + 1 ))
    read -r symbol fix_c diff leaps fractional bot confirmed <<<"${row}"
    target="$(function_of "${bot}")"
    echo
    echo "################################################################################"
    echo "## [${index}/${#SELECTED[@]}] ${symbol} -> ${target}"
    echo "##   FIX_C ${fix_c}, DIFF ${diff}, LEAPS ${leaps}, fractional ${fractional}, LEAPS confirmed ${confirmed}"
    echo "################################################################################"
    status=0
    run_bot "${symbol}" "${fix_c}" "${diff}" "${leaps}" "${fractional}" "${bot}" "${confirmed}" || status=$?
    if (( status == 0 )); then
        RESULTS+=("$(printf '%-6s %-22s deployed' "${symbol}" "${target}")")
    else
        RESULTS+=("$(printf '%-6s %-22s FAILED (status %s): read its section above' \
            "${symbol}" "${target}" "${status}")")
        LEFT+=("${symbol}")
    fi
done

echo
echo "################################################################################"
echo "## Summary"
for line in "${RESULTS[@]}"; do
    echo "  ${line}"
done
if (( ${#LEFT[@]} == 0 )); then
    cat <<EOF

Every bot asked for is deployed. Watch the first session from 09:30 New York (each bot printed
its own log query and kill switch above). Each release ends after its window: run this command
again before then.
EOF
    exit 0
fi
prefix="LEGO_ALERTS_CONFIRMED=yes"
for pass in "${PASS_THROUGH[@]}"; do
    [[ -z "${!pass:-}" ]] || prefix+=" ${pass}=${!pass}"
done
cat <<EOF

A bot refused before its deploy step still runs the release it ran before. Fix the error
printed in the section of each failed bot, then deploy only those:
  ${prefix} bash deploy/cloudshell-prod-live-all.sh ${LEFT[*]}
EOF
exit 1
