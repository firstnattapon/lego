#!/usr/bin/env bash
# Run in a reviewed, pinned, clean checkout. No clone, delete, or auto-authorization.
set -Eeuo pipefail
: "${EXPECTED_COMMIT:?Set EXPECTED_COMMIT to the reviewed full 40-character commit}"
[[ "${EXPECTED_COMMIT}" =~ ^[0-9a-f]{40}$ ]]
[[ "$(git rev-parse HEAD)" == "${EXPECTED_COMMIT}" ]]
[[ -z "$(git status --porcelain --untracked-files=all)" ]]
export LEGO_MODE_OVERRIDE=observe
export LEGO_ACTIVE_OVERRIDE=false
export LEGO_RELEASE_AUTHORIZATION_OVERRIDE=''
export EXPECTED_CANDIDATE_HASH="$(python3 tools/candidate_manifest.py | python3 -c 'import json,sys; print(json.load(sys.stdin)["candidate_hash"])')"
bash deploy/cloudshell-all-in-one.sh
