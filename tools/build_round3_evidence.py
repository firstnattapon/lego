"""Build local closure from successful candidate-bound raw validation.

Historical criteria are preserved in release_evidence_v2/baseline_7704.
Missing/failed commands, changed logs, or another candidate cannot earn PASS.
"""
from __future__ import annotations

import json
if __package__:
    from .candidate_manifest import ROOT, build_manifest, digest
else:
    from candidate_manifest import ROOT, build_manifest, digest

EVIDENCE = ROOT / "release_evidence_v2"
REQUIRED = {"backend", "reader", "emulator", "compile", "pip_check",
            "pip_audit", "secret_scan", "dependency_install"}


def validate(manifest, validation, evidence_dir=EVIDENCE):
    if validation.get("candidate_hash") != manifest["candidate_hash"]:
        raise ValueError("validation belongs to another candidate")
    if validation.get("candidate_unchanged") is not True:
        raise ValueError("candidate changed during validation")
    records = {item["id"]: item for item in validation["commands"]}
    if not REQUIRED.issubset(records):
        raise ValueError("required validation commands missing")
    for name in REQUIRED:
        record = records[name]
        if record["exit_code"] != 0:
            raise ValueError(f"validation failed: {name}")
        log = (evidence_dir / record["log"]).resolve()
        if not log.is_relative_to(evidence_dir.resolve()):
            raise ValueError("log must be inside evidence directory")
        if not log.is_file() or digest(log) != record["log_sha256"]:
            raise ValueError(f"missing or changed raw log: {name}")
    return records


def main():
    manifest = build_manifest()
    validation = json.loads((EVIDENCE / "FINAL_VALIDATION.json").read_text(encoding="utf-8"))
    records = validate(manifest, validation)
    common = {"candidate_hash": manifest["candidate_hash"],
              "dependency_lock_hash": manifest["dependency_lock_hash"],
              "source_commit_or_tree_hash": "snapshot:" + manifest["candidate_hash"],
              "timestamp_utc": validation["finished_utc"]}
    local_names = {
        "L01": ["backend", "reader"], "L02": ["backend", "emulator"],
        "L03": ["backend", "emulator"], "L04": ["backend", "emulator"],
        "L05": ["emulator"], "L06": ["backend", "emulator"],
        "L07": ["backend"], "L08": ["emulator"],
        "L09": ["backend", "reader", "emulator"], "L10": sorted(REQUIRED),
    }
    acceptance_names = {"A01": ["backend", "reader"], "A10": ["emulator"],
                        "A15": ["backend", "emulator"], "A24": ["reader"]}
    outputs = {}
    for filename, names in (("LOCAL_CLOSURE.json", local_names),
                            ("ACCEPTANCE.json", acceptance_names)):
        document = json.loads((EVIDENCE / "baseline_7704" / filename).read_text(encoding="utf-8"))
        document.update(common)
        for check in document["checks"]:
            check.update(common)
            if check["status"] == "PASS":
                selected = names.get(check["check_id"], ["backend"])
                check["actual"] = "; ".join(records[name]["summary"] for name in selected)
                check["command_or_procedure"] = "FINAL_VALIDATION.json: " + ", ".join(selected)
                check["evidence_paths"] = ["release_evidence_v2/FINAL_VALIDATION.json"] + [
                    "release_evidence_v2/" + records[name]["log"] for name in selected]
                if check["check_id"] in {"L01", "A01"}:
                    check["evidence_paths"] += ["release_evidence_v2/REVIEW3_FINAL_REPORT_TH.md",
                                                "release_evidence_v2/baseline_7704/RELEASE_MANIFEST.json"]
        document["pass_count"] = sum(c["status"] == "PASS" for c in document["checks"])
        document["framework_ready"] = False
        outputs[filename] = document
    local = outputs["LOCAL_CLOSURE.json"]
    local["local_complete"] = local["pass_count"] == local["required_total"]
    local["local_completion_percent"] = 100 * local["pass_count"] / local["required_total"]
    acceptance = outputs["ACCEPTANCE.json"]
    acceptance["blocked_count"] = sum(c["status"] == "BLOCKED" for c in acceptance["checks"])
    acceptance["pass_rate_percent"] = 100 * acceptance["pass_count"] / acceptance["required_total"]
    acceptance["local_complete"] = local["local_complete"]
    acceptance["ready"] = all(c["status"] == "PASS" for c in acceptance["checks"])
    outputs["RELEASE_MANIFEST.json"] = {
        **manifest, **common, "schema_version": 4,
        "local_complete": local["local_complete"], "framework_ready": False,
        "evidence_links": ["release_evidence_v2/" + name for name in (
            "LOCAL_CLOSURE.json", "ACCEPTANCE.json", "FINAL_VALIDATION.json",
            "REVIEW3_FINAL_REPORT_TH.md")],
        "evidence_hashes": {r["log"]: r["log_sha256"] for r in records.values()},
    }
    for name, value in outputs.items():
        (EVIDENCE / name).write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps({**common, "local_complete": local["local_complete"],
                      "framework_ready": False}, indent=2))


if __name__ == "__main__":
    main()
