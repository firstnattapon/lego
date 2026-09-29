"""Create an append-only, redacted release binding from a captured ready revision.

This is an operator deployment receipt, not a signed build attestation or proof
of successful broker execution. Never overwrite a receipt for an older release.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def build(service, *, git_commit, candidate):
    template = service["spec"]["template"]
    container = template["spec"]["containers"][0]
    env = {item["name"]: item.get("value") for item in container.get("env", [])}
    ready = service["status"]["latestReadyRevisionName"]
    image = container["image"]
    if (not re.fullmatch(r"[0-9a-f]{40}", git_commit)
            or not re.fullmatch(r"[0-9a-f]{64}", candidate)
            or env.get("LEGO_GIT_COMMIT") != git_commit
            or env.get("LEGO_CANDIDATE_HASH") != candidate
            or not re.fullmatch(r".+@sha256:[0-9a-f]{64}", image)
            or env.get("WEBULL_ENV") not in {"UAT", "PROD"}
            or template.get("metadata", {}).get("name") != ready):
        raise ValueError("source/candidate/image/ready revision binding incomplete")
    account = next((item.get("valueFrom") for item in container.get("env", [])
                    if item["name"] == "WEBULL_ACCOUNT_ID"), None)
    if not account:
        raise ValueError("account must be bound through a secret reference")
    if sum(item.get("percent", 0) for item in service["status"].get("traffic", [])
           if item.get("revisionName") == ready) != 100:
        raise ValueError("ready revision does not own all traffic")
    risk = {key: value for key, value in env.items() if key.startswith((
        "LEGO_MAX_", "LEGO_STALE_", "LEGO_CANCEL_")) or key in {
            "LEGO_TRADING_WINDOW_END", "LEGO_SESSION_KEY_MODE", "LEGO_ALLOW_FRACTIONAL",
            "LEGO_FIX_C", "LEGO_DIFF", "LEGO_DNA_BUNDLE"}}
    receipt = {"schema": "lego_deployment_receipt_v1", "git_commit": git_commit,
               "candidate_hash": candidate, "container_image": image,
               "deployment_revision": ready, "environment": env["WEBULL_ENV"],
               "symbol": env.get("LEGO_SYMBOL"), "mode": env.get("LEGO_MODE"),
               "active": env.get("LEGO_ACTIVE"),
               "account_secret_binding_sha256": digest(account),
               "release_authorization_sha256": digest(env.get("LEGO_RELEASE_AUTHORIZATION")),
               "risk_limits_sha256": digest(risk),
               "scope": "captured deployment binding; build attestation and broker acceptance separate"}
    return {**receipt, "receipt_sha256": digest(receipt)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--service", type=Path, required=True)
    parser.add_argument("--git-commit", required=True)
    parser.add_argument("--candidate", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    receipt = build(json.loads(args.service.read_text(encoding="utf-8")),
                    git_commit=args.git_commit, candidate=args.candidate)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as handle:
        json.dump(receipt, handle, indent=2)
        handle.write("\n")


if __name__ == "__main__":
    main()
