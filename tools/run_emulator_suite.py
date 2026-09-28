"""Run the full suite with a private local RTDB emulator; never use a live DB."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.request


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--jar", type=Path, required=True)
    parser.add_argument("--port", type=int, default=9014)
    args = parser.parse_args()
    if not args.jar.is_file() or not 1024 <= args.port <= 65535:
        parser.error("existing Firebase emulator jar and unprivileged port required")
    root = Path(__file__).resolve().parents[1]
    artifacts = root / ".runtime-artifacts"
    artifacts.mkdir(exist_ok=True)
    env = {**os.environ, "FIREBASE_DATABASE_EMULATOR_HOST": f"127.0.0.1:{args.port}",
           "GCLOUD_PROJECT": "demo-lego-firebase"}
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    with (artifacts / "emulator.log").open("w", encoding="utf-8") as log:
        process = subprocess.Popen(["java", "-jar", str(args.jar.resolve()), "--host", "127.0.0.1", "--port", str(args.port)],
                                   stdout=log, stderr=subprocess.STDOUT, cwd=root, creationflags=flags)
        try:
            url = f"http://127.0.0.1:{args.port}/.settings/rules.json?ns=demo-lego-firebase-default-rtdb"
            ready = False
            for _ in range(50):
                if process.poll() is not None: raise RuntimeError("emulator exited; inspect local emulator.log")
                try:
                    request = urllib.request.Request(url, data=(root / "database.rules.json").read_bytes(),
                        headers={"Authorization": "Bearer owner", "Content-Type": "application/json"}, method="PUT")
                    with urllib.request.urlopen(request, timeout=1) as response: ready = response.status == 200
                    if ready: break
                except OSError: time.sleep(0.2)
            if not ready: raise RuntimeError("local emulator did not become ready")
            return subprocess.call([sys.executable, "-m", "pytest", "-q", "-o", "cache_dir=.cache-v4/pytest",
                "--junitxml=release_evidence/continuous-v4-pytest.xml"], cwd=root, env=env)
        finally:
            process.terminate()
            try: process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


if __name__ == "__main__":
    raise SystemExit(main())
