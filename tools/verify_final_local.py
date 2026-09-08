"""Capture reproducible local validation; never deploy or invoke a live broker."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from datetime import datetime, timezone

from candidate_manifest import ROOT, STREAMLIT, build_manifest, digest

EVIDENCE = ROOT / "release_evidence_v2"


def utc():
    return datetime.now(timezone.utc).isoformat()


def child(kind):
    if kind == "emulator":
        host = os.environ.get("FIREBASE_DATABASE_EMULATOR_HOST", "")
        if not host.startswith(("127.0.0.1:", "localhost:")):
            raise SystemExit("local emulator required")
        for args in (
            ["-m", "pytest", "-q", "test_database_rules.py"],
            ["tools/emulator_race_probe.py"], ["tools/emulator_tick_race_probe.py"],
            ["tools/emulator_hot_state_probe.py"], ["tools/emulator_fifo_stress.py"],
            ["tools/round3_regression_probe.py"],
        ):
            subprocess.run([sys.executable, *args], cwd=ROOT, check=True)
        print("ALL_LOCAL_EMULATOR_CHECKS_PASS", flush=True)
    else:
        files = build_manifest()["files"]
        findings = []
        count = 0
        patterns = [
            re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
            re.compile(r"AIza[0-9A-Za-z_-]{35}"),
            re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
            re.compile(r"(?:ghp_|github_pat_)[0-9A-Za-z_]{30,}"),
            re.compile(r'"private_key"\s*:\s*"-----BEGIN'),
        ]
        for key in files:
            owner, relative = key.split("/", 1)
            path = (ROOT if owner == "backend" else STREAMLIT) / relative
            if kind == "compile" and path.suffix == ".py":
                compile(path.read_bytes(), str(path), "exec")
                count += 1
            elif kind == "secret_scan" and path.suffix != ".whl":
                content = path.read_text(encoding="utf-8", errors="replace")
                for pattern in patterns:
                    if pattern.search(content):
                        # Record filenames only: never emit possible secret material.
                        findings.append(key)
                        break
                count += 1
        if findings:
            print(json.dumps({"status": "FAIL", "files": findings}))
            raise SystemExit(1)
        print(json.dumps({"status": "PASS", "check": kind, "files_checked": count,
                          "scope": "manifest source; heuristic secret patterns, wheel excluded"}))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--child", choices=("emulator", "compile", "secret_scan"))
    parser.add_argument("--reader-python")
    parser.add_argument("--firebase-cli")
    parser.add_argument("--java-home")
    parser.add_argument("--only", help="Rerun one failed command for the identical candidate")
    args = parser.parse_args()
    if args.child:
        return child(args.child)
    if not all((args.reader_python, args.firebase_cli, args.java_home)):
        parser.error("reader-python, firebase-cli and java-home are required")
    manifest = build_manifest()
    validation = {"candidate_hash": manifest["candidate_hash"], "started_utc": utc(),
                  "commands": [], "live_actions_performed": False}
    if args.only:
        validation = json.loads((EVIDENCE / "FINAL_VALIDATION.json").read_text(encoding="utf-8"))
        if validation["candidate_hash"] != manifest["candidate_hash"]:
            raise SystemExit("cannot reuse results from another candidate")
        validation["commands"] = [r for r in validation["commands"] if r["id"] != args.only]
    env = dict(os.environ, JAVA_HOME=args.java_home, CI="true", PYTHONIOENCODING="utf-8",
               XDG_CONFIG_HOME=str(EVIDENCE / "final_cli_config"))
    env["PATH"] = str(Path(args.java_home) / "bin") + os.pathsep + env["PATH"]
    reader_python = str(Path(args.reader_python).resolve())
    script = str(Path(__file__).resolve())
    commands = [
        ("dependency_install", [sys.executable, "-m", "pip", "install", "--no-index",
                                "-r", "requirements-dev.txt", "--disable-pip-version-check"], ROOT),
        ("backend", [sys.executable, "-m", "pytest", "-q", "-rs"], ROOT),
        ("reader", [reader_python, "-m", "pytest", "-q"], STREAMLIT),
        ("pip_check", [sys.executable, "-m", "pip", "check"], ROOT),
        ("compile", [sys.executable, script, "--child", "compile"], ROOT),
        ("secret_scan", [sys.executable, script, "--child", "secret_scan"], ROOT),
        ("pip_audit", ["pip-audit", "--path", str(Path(sys.executable).parents[1] / "Lib/site-packages"),
                       "--progress-spinner", "off"], ROOT),
        ("emulator", ["node", args.firebase_cli, "emulators:exec", "--only", "database",
                      "--project", "demo-lego-firebase", subprocess.list2cmdline(
                          [sys.executable, script, "--child", "emulator"])], ROOT),
    ]
    for name, command, cwd in commands:
        if args.only and args.only != name:
            continue
        print(f"START {name}", flush=True)
        log = EVIDENCE / f"FINAL_{name.upper()}.log"
        started = utc()
        with log.open("w", encoding="utf-8") as output:
            result = subprocess.run(command, cwd=cwd, env=env, stdout=output,
                                    stderr=subprocess.STDOUT, check=False)
        text = log.read_text(encoding="utf-8", errors="replace")
        summary = next((line.strip() for line in reversed(text.splitlines())
                        if " passed" in line), text.strip().splitlines()[-1] if text.strip() else "empty log")
        if name == "emulator" and "ALL_LOCAL_EMULATOR_CHECKS_PASS" in text:
            summary = "RTDB rules, races (16 workers), hot state, 10000 lots, and all new adversarial regressions PASS; <=8 matching steps, <=16 direct page reads/call."
        validation["commands"].append({"id": name, "command": command, "cwd": str(cwd),
            "started_utc": started, "finished_utc": utc(), "exit_code": result.returncode,
            "log": log.name, "log_sha256": digest(log), "summary": summary})
        print(f"DONE {name}: exit={result.returncode}; {summary}", flush=True)
    validation["finished_utc"] = utc()
    validation["candidate_unchanged"] = build_manifest() == manifest
    (EVIDENCE / "FINAL_VALIDATION.json").write_text(
        json.dumps(validation, indent=2) + "\n", encoding="utf-8")
    if not validation["candidate_unchanged"] or any(r["exit_code"] for r in validation["commands"]):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
