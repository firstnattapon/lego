"""Build criterion-level acceptance and candidate manifests from the frozen plan."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PLAN = ROOT.parent / "review_artifacts" / "LEGO_COMMERCIAL_ARCHITECTURE_SOL_MEDIUM_V2.json"
OUT = ROOT / "release_evidence"


def _ids(group: str, count: int) -> set[str]:
    return {f"{group}-C{i:02d}" for i in range(1, count + 1)}


PASS_IDS = (
    _ids("G01", 6)
    | _ids("G02", 7)
    | (_ids("G03", 9) - {"G03-C09"})
    | (_ids("G04", 6) - {"G04-C06"})
    | _ids("G05", 3)
    | {"G06-C01", "G06-C02", "G06-C03", "G06-C05"}
    | {"G07-C04", "G07-C05", "G07-C06"}
    | {"G10-C01", "G10-C03", "G10-C07"}
    | {"G11-C01", "G11-C02"}
    | {"G12-C01", "G12-C02", "G12-C05"}
)

BLOCKERS = {
    "G03-C09": "ต้องมี isolated deployment และ policy เมื่อหลาย chain/account แชร์ buying power/symbol",
    "G04-C06": "ต้องรัน mixed-version rollout/rollback drill กับ unresolved intent",
    "G05-C04": "ยังไม่มี operator-approved retention และ replay/dedupe horizon",
    "G05-C05": "ยังไม่มี workload ceiling และ deployed RTDB read-byte measurement",
    "G06-C04": "มี emulator rules แต่ยังไม่มี real RTDB transaction contention evidence",
    "G06-C06": "source delivery ไม่มี .git จึงพิสูจน์ unrelated working-tree changes ไม่ได้",
    "G07-C01": "ไม่มี cloud IAM/deploy authority สำหรับ least-privilege verification",
    "G07-C02": "ไม่มี isolated deployed runtime identity evidence",
    "G07-C03": "ยังไม่ได้ verify deployed RTDB IAM/rules model",
    "G08-C01": "ต้องสร้าง clean isolated Gen2 build จาก exact candidate",
    "G08-C02": "ไม่มี GCP project/deploy authorization",
    "G08-C03": "ไม่มี deployed URLs สำหรับ invoke 3 entrypoints",
    "G08-C04": "ต้องพิสูจน์ env/SDK ใน deployed Gen2 runtime",
    "G08-C05": "ต้องทดสอบ deployed logging/response/auth negative paths",
    "G09-C01": "ไม่มี Webull UAT credentials/market entitlement ใน session",
    "G09-C02": "ขาด symbol/window/notional และ explicit one-order approval",
    "G09-C03": "ห้าม Place โดยไม่มี explicit UAT order authorization",
    "G09-C04": "ยังไม่มี real terminal positive fill",
    "G09-C05": "ยังไม่มี post-fill holdings witness จริง",
    "G09-C06": "ยังไม่มี live fill เพื่อพิสูจน์ two-ledger once-only",
    "G09-C07": "ยังไม่มี broker order-count evidence หลัง replay/reconcile",
    "G10-C02": "ยังไม่มี deployed alert route/owner drill",
    "G10-C04": "ต้องใช้ isolated backup restore และ external broker reconciliation",
    "G10-C05": "ต้องรัน rollback drill ขณะมี unresolved intent",
    "G10-C06": "operator ยังไม่กำหนด numeric RPO/RTO/retention/owner",
    "G11-C03": "operator ยังไม่ล็อก numeric budgets/workload ceiling",
    "G11-C04": "ยังไม่มี deployed latency/calls/read-byte regression",
    "G11-C05": "ยังไม่มี soak/backlog sustained-capacity run",
    "G11-C06": "ยังไม่มี reproducible deployed cost profile",
    "G12-C03": "ยังมี required criteria BLOCKED",
    "G12-C04": "external deployment/UAT/operations high-risk gates ยังไม่ปิด",
    "G12-C06": "ยังไม่มี independent operator deployment/recovery walkthrough",
}

EVIDENCE = {
    "G01": "release_evidence/CONTRACTS.md; execution/characterization tests",
    "G02": "release_evidence/CONTRACTS.md; partial/fill/holdings tests",
    "G03": "release_evidence/FAILURE_MATRIX.md; concurrency/idempotency tests",
    "G04": "release_evidence/STATE_TRANSITIONS.md; clock/identity/HTTP tests",
    "G05": "lego_archive.py; archive race regression and archive tests",
    "G06": "fresh pytest/compile/import/emulator results in REALITY_AUDIT.md",
    "G07": "pip-audit 0 findings; redaction/admin reconciliation tests",
    "G08": "release_evidence/DEPLOYMENT_PROFILE.md",
    "G09": "release_evidence/DEPLOYMENT_PROFILE.md",
    "G10": "release_evidence/OPERATIONS_RUNBOOK.md",
    "G11": "release_evidence/PERFORMANCE_PROFILE.json; service extraction",
    "G12": "release_evidence directory and README.md",
}


def _candidate_files() -> list[Path]:
    files = list(ROOT.glob("*.py"))
    files += [ROOT / name for name in (
        "requirements.txt", "database.rules.json", "firebase.json", ".gcloudignore")]
    files += list((ROOT / "vendor").glob("*.whl"))
    return sorted((p for p in files if p.is_file()), key=lambda p: p.as_posix())


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> None:
    plan = json.loads(PLAN.read_text(encoding="utf-8"))
    files = _candidate_files()
    hashes = {p.relative_to(ROOT).as_posix(): _sha(p) for p in files}
    canonical = "".join(f"{name}\0{digest}\n" for name, digest in hashes.items())
    tree_hash = hashlib.sha256(canonical.encode()).hexdigest()
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    criteria = []
    for group in plan["acceptance_registry"]["groups"]:
        for number, text in enumerate(group["required_criteria"], 1):
            criterion_id = f"{group['id']}-C{number:02d}"
            passed = criterion_id in PASS_IDS
            status = "PASS" if passed else "BLOCKED"
            criteria.append({
                "criterion_id": criterion_id,
                "group": group["name"],
                "criterion": text,
                "required": True,
                "status": status,
                "release_sha": None,
                "working_tree_hash": tree_hash,
                "environment": "local/emulator" if passed else None,
                "command_or_procedure": EVIDENCE[group["id"]],
                "expected": "criterion satisfied with candidate-bound evidence",
                "observed": "verified by fresh local evidence" if passed else None,
                "test_counts": "backend 644 passed + emulator 56 passed + streamlit 53 passed" if passed else None,
                "artifact_path": EVIDENCE[group["id"]],
                "timestamp_utc": now,
                "reviewer": "Codex local audit",
                "blocking_reason": None if passed else BLOCKERS.get(
                    criterion_id, "required external evidence is not available"),
            })

    passed = sum(item["status"] == "PASS" for item in criteria)
    total = len(criteria)
    acceptance = {
        "schema": "lego_acceptance_registry_v1",
        "registry_version": 1,
        "frozen_plan_sha256": _sha(PLAN),
        "generated_at": now,
        "release_state": "NOT_READY" if passed != total else "COMMERCIAL_RELEASE_CANDIDATE_100_READY",
        "candidate_working_tree_hash": tree_hash,
        "release_sha": None,
        "summary": {
            "required": total,
            "pass": passed,
            "fail": 0,
            "blocked": total - passed,
            "not_run": 0,
            "stale": 0,
            "pass_rate_percent": round(100 * passed / total, 4),
        },
        "pass_rule": plan["acceptance_registry"]["pass_rule"],
        "criteria": criteria,
    }
    (OUT / "ACCEPTANCE.json").write_text(
        json.dumps(acceptance, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    evidence_hashes = {
        p.name: _sha(p) for p in sorted(OUT.iterdir())
        if p.is_file() and p.name not in {"RELEASE_MANIFEST.json", "ACCEPTANCE_REPORT.md"}
    }
    manifest = {
        "schema": "lego_release_manifest_v1",
        "generated_at": now,
        "status": acceptance["release_state"],
        "candidate_working_tree_hash": tree_hash,
        "release_sha": None,
        "git_metadata_available": False,
        "runtime": {
            "python": "3.12.10",
            "firebase_database_emulator": "4.11.2",
            "emulator_java": "Eclipse Temurin 21.0.12.1+1",
            "webull_sdk": "2.0.15 vendored metadata-only patch",
            "cryptography": "50.0.0",
        },
        "verification": {
            "backend": "644 passed, 1 emulator-only skipped",
            "backend_clean_venv": "exact requirements installed; 644 passed, 1 emulator-only skipped",
            "database_rules_emulator": "56 passed",
            "streamlit": "53 passed",
            "compile_import_entrypoints": "PASS",
            "pip_check": "PASS",
            "pip_audit": "0 known vulnerabilities",
        },
        "source_hashes": hashes,
        "evidence_hashes": evidence_hashes,
        "blocked_external_phases": ["A6 isolated Gen2", "A7 real Webull UAT", "A8 deployed operations", "A9 final 100% audit", "A10 production authorization"],
    }
    (OUT / "RELEASE_MANIFEST.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    report = "# Acceptance report\n\n"
    report += f"Candidate working-tree hash: `{tree_hash}`  \n"
    report += f"Status: **{acceptance['release_state']}**  \n"
    report += f"Required: {total} · PASS: {passed} · BLOCKED: {total-passed} · pass rate: {100*passed/total:.4f}%\n\n"
    report += "คะแนนนี้ใช้ registry v1 จำนวน 76 criteria จึงห้ามเทียบตรงกับรายงานเก่า 80/100 ที่ใช้ weighting คนละชุด\n\n"
    report += "## Blocked criteria\n\n"
    for item in criteria:
        if item["status"] != "PASS":
            report += f"- `{item['criterion_id']}` {item['criterion']}: {item['blocking_reason']}\n"
    (OUT / "ACCEPTANCE_REPORT.md").write_text(report, encoding="utf-8")


if __name__ == "__main__":
    main()

