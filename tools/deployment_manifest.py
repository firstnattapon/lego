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


def resolved_image(service, revision=None):
    """Cloud Functions Services retain tags; Ready Revisions expose digests."""
    template = service["spec"]["template"]
    image = template["spec"]["containers"][0]["image"]
    if revision is None:
        if not re.fullmatch(r".+@sha256:[0-9a-f]{64}", image):
            raise ValueError("tagged Service requires captured Ready Revision")
        return image
    ready = service["status"]["latestReadyRevisionName"]
    metadata, status = revision["metadata"], revision["status"]
    containers = revision["spec"]["containers"]
    if (metadata.get("name") != ready or len(containers) != 1
            or metadata.get("labels", {}).get("serving.knative.dev/service") != service["metadata"]["name"]
            or not any(c.get("type") == "Ready" and c.get("status") == "True"
                       for c in status.get("conditions", []))):
        raise ValueError("captured Revision is not this Service's Ready Revision")
    digest_image = status.get("imageDigest", "")
    if (not re.fullmatch(r".+@sha256:[0-9a-f]{64}", digest_image)
            or containers[0].get("image") != digest_image):
        raise ValueError("Revision spec/status image digest mismatch")
    def repository(value):
        base = value.split("@", 1)[0]
        prefix, _, name = base.rpartition("/")
        return prefix + "/" + name.split(":", 1)[0]
    if (repository(image) != repository(digest_image)
            or ("@sha256:" in image and image != digest_image)):
        raise ValueError("Service/Revision image repository or digest mismatch")
    # A digest from another revision cannot certify this Service's settings.
    for key in ("env", "resources", "ports"):
        left = template["spec"]["containers"][0].get(key)
        right = containers[0].get(key)
        if key == "env":
            left = sorted(left or [], key=lambda item: item["name"])
            right = sorted(right or [], key=lambda item: item["name"])
        if left != right:
            raise ValueError("Service/Revision container configuration mismatch: " + key)
    for key in ("serviceAccountName", "containerConcurrency", "timeoutSeconds"):
        if template["spec"].get(key) != revision["spec"].get(key):
            raise ValueError("Service/Revision runtime configuration mismatch: " + key)
    return digest_image


def build(service, *, git_commit, candidate, revision=None):
    template = service["spec"]["template"]
    container = template["spec"]["containers"][0]
    env = {item["name"]: item.get("value") for item in container.get("env", [])}
    ready = service["status"]["latestReadyRevisionName"]
    image = resolved_image(service, revision)
    if (not re.fullmatch(r"[0-9a-f]{40}", git_commit)
            or not re.fullmatch(r"[0-9a-f]{64}", candidate)
            or env.get("LEGO_GIT_COMMIT") != git_commit
            or env.get("LEGO_CANDIDATE_HASH") != candidate
            or not re.fullmatch(r".+@sha256:[0-9a-f]{64}", image)
            or env.get("WEBULL_ENV") not in {"UAT", "PROD"}
            or template.get("metadata", {}).get("name", ready if revision is not None else None) != ready):
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
    parser.add_argument("--revision", type=Path,
                        help="captured gcloud run revisions describe JSON (required for tagged Service)")
    parser.add_argument("--git-commit", required=True)
    parser.add_argument("--candidate", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    receipt = build(json.loads(args.service.read_text(encoding="utf-8")),
                    git_commit=args.git_commit, candidate=args.candidate,
                    revision=json.loads(args.revision.read_text(encoding="utf-8")) if args.revision else None)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as handle:
        json.dump(receipt, handle, indent=2)
        handle.write("\n")


if __name__ == "__main__":
    main()
