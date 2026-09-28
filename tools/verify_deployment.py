"""Verify captured Cloud Run v1 service and Scheduler JSON; emit no secrets.

Run after deployment and before enabling scheduled work. This validates the
observed revision/config, not credentials, current broker state or alert delivery.
"""
import argparse
import hashlib
import json
import re
from pathlib import Path


def verify(service, scheduler, *, candidate, revision, image, function=None):
    template = service["spec"]["template"]
    spec = template["spec"]
    containers = spec["containers"]
    env = {v["name"]: v.get("value") for v in containers[0].get("env", [])}
    traffic = service["status"].get("traffic", [])
    checks = {
        "single_container": len(containers) == 1,
        "concurrency": spec.get("containerConcurrency") == 1,
        "timeout": spec.get("timeoutSeconds") == 45,
        "max_instances": template.get("metadata", {}).get("annotations", {}).get("autoscaling.knative.dev/maxScale") == "1",
        "revision": service["status"].get("latestReadyRevisionName") == revision,
        "traffic": (sum(t.get("percent", 0) for t in traffic if t.get("revisionName") == revision) == 100
                    and all(t.get("revisionName") == revision for t in traffic)),
        "image": bool(re.fullmatch(r".+@sha256:[0-9a-f]{64}", image)) and containers[0].get("image") == image,
        "candidate": bool(re.fullmatch(r"[0-9a-f]{64}", candidate)) and env.get("LEGO_CANDIDATE_HASH") == candidate,
        "environment": env.get("WEBULL_ENV") in {"UAT", "PROD"},
        "scheduler_retry": (scheduler.get("retryConfig", {}).get("retryCount", 0) == 0
                            and scheduler.get("retryConfig", {}).get("maxRetryDuration", "0s") in {"0s", "0.000s"}),
        "scheduler_cadence": scheduler.get("schedule") == "* * * * *",
        "scheduler_target": scheduler.get("httpTarget", {}).get("uri") == service["status"].get("url"),
    }
    # Functions may expose another URL. Require its captured service identity.
    if function is not None:
        function_config = function.get("serviceConfig", {})
        checks["function_identity"] = bool(service.get("metadata", {}).get("name")) and function_config.get("service", "").split("/")[-1] == service["metadata"]["name"]
        checks["scheduler_target"] = bool(function_config.get("uri")) and scheduler.get("httpTarget", {}).get("uri") == function_config["uri"]
    if env.get("WEBULL_ENV") == "PROD":
        checks["production_observe"] = env.get("LEGO_MODE") == "observe" and env.get("LEGO_ACTIVE") == "false"
    config_hash = hashlib.sha256(json.dumps(sorted(containers[0].get("env", []), key=lambda v: v["name"]),
                                          sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return {"status": "PASS" if all(checks.values()) else "FAIL", "checks": checks,
            "candidate_hash": candidate, "revision": revision, "image": image, "config_hash": config_hash,
            "scope": "captured deployment settings; broker and alert delivery unverified"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--service", type=Path, required=True)
    parser.add_argument("--scheduler", type=Path, required=True)
    parser.add_argument("--candidate", required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--function", type=Path)
    args = parser.parse_args()
    report = verify(json.loads(args.service.read_text()), json.loads(args.scheduler.read_text()),
                    candidate=args.candidate, revision=args.revision, image=args.image,
                    function=json.loads(args.function.read_text()) if args.function else None)
    print(json.dumps(report, indent=2))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
