"""Render Cloud Monitoring REST resources; creates no cloud resources.

Reference: https://docs.cloud.google.com/monitoring/api/ref_v3/rest/v3/projects.alertPolicies
"""
import argparse
import json
from pathlib import Path
import re


def build(service, channel):
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,62}", service):
        raise ValueError("invalid Cloud Run service")
    if not re.fullmatch(r"projects/[A-Za-z0-9-]+/notificationChannels/[0-9]+", channel):
        raise ValueError("use an existing verified notification channel resource name")
    base = f'resource.type="cloud_run_revision" resource.labels.service_name="{service}"'
    metric = f"lego_tick_completed_{service.replace('-', '_')}"
    log_filter = base + ' jsonPayload.event="lego_tick_completed"'
    # Things that need an operator now. Kept apart from the horizons below: a
    # release that has ended matches every tick, and one policy for both paged
    # every five minutes for days (2,748 RELEASE_EXPIRED ticks in the 10-02..04 logs).
    alarming = ('severity>=ERROR OR jsonPayload.business_status=("AUTH_BACKOFF" OR "OPERATOR_HALT" '
                'OR "OPEN_ORDER_BLOCKED")')
    # Horizons that end silently: renew the release window, DNA or token. These
    # notify at most every six hours; alerting.notify_tick also posts the webhook
    # once a day with the instant the horizon ends.
    horizons = ('jsonPayload.business_status=("DNA_LOW" OR "DNA_EXHAUSTED" OR "RELEASE_EXPIRING" '
                'OR "RELEASE_EXPIRED" OR "TOKEN_EXPIRY_WARNING") '
                'OR jsonPayload.operational_health.dna_low=true '
                'OR jsonPayload.operational_health.dna_exhausted=true '
                'OR jsonPayload.operational_health.release_expiring=true '
                'OR jsonPayload.operational_health.release_expired=true '
                'OR jsonPayload.operational_health.token_warning=true')
    common = {"combiner": "OR", "enabled": True, "notificationChannels": [channel],
              "documentation": {"mimeType": "text/markdown", "content":
                  "Inspect candidate/revision and private order audit. Pause new orders; retain recovery and fences. Follow CONTINUOUS_RELEASE_V4_TH.md."}}
    renewal_docs = {"mimeType": "text/markdown", "content":
        "A release horizon (approval window, DNA or token) is ending or has ended. Nothing renews itself: "
        "run `python ops.py release-plan` for the next release (UAT: CONTINUOUS_RELEASE_V4_TH.md; "
        "PROD: PROD_LIVE_RUNBOOK_TH.md). Orders are blocked until a new release is deployed."}
    return {
        "tick-metric": {"name": metric, "filter": log_filter,
                        "metricDescriptor": {"metricKind": "DELTA", "valueType": "INT64", "unit": "1"}},
        "health-policy": {**common, "displayName": f"{service}: execution health",
            "conditions": [{"displayName": "Action required", "conditionMatchedLog": {
                "filter": log_filter + " (" + alarming + ")"}}],
            "alertStrategy": {"notificationRateLimit": {"period": "300s"}, "autoClose": "1800s"}},
        "horizon-policy": {**common, "displayName": f"{service}: release/DNA/token horizon",
            "documentation": renewal_docs,
            "conditions": [{"displayName": "Renewal needed", "conditionMatchedLog": {
                "filter": log_filter + " (" + horizons + ")"}}],
            "alertStrategy": {"notificationRateLimit": {"period": "21600s"}, "autoClose": "86400s"}},
        "absence-policy": {**common, "displayName": f"{service}: three missing ticks",
            "conditions": [{"displayName": "No tick for 180 seconds", "conditionAbsent": {
                "filter": f'metric.type="logging.googleapis.com/user/{metric}" AND resource.type="cloud_run_revision" AND resource.labels.service_name="{service}"',
                "duration": "180s", "trigger": {"count": 1},
                "aggregations": [{"alignmentPeriod": "60s", "perSeriesAligner": "ALIGN_SUM",
                                  "crossSeriesReducer": "REDUCE_SUM"}]}}]},
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--service", required=True)
    parser.add_argument("--channel", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    resources = build(args.service, args.channel)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    for name, payload in resources.items():
        (args.output_dir / (name + ".json")).write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": "RENDERED", "delivery_verified": False}))


if __name__ == "__main__":
    main()
