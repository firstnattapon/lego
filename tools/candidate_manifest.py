"""Print deterministic candidate identity for the deployable backend + v2 reader.

Without arguments the working directory is hashed, as always. That walk sees files git
ignores (a token.txt, a credentials file, a virtualenv, a sibling reader checkout), which
`git status --porcelain` does not show, so the same commit could produce another hash on
another machine: the CANDIDATE_MISMATCH of PROD revisions 00003-dob and 00005-tul
(docs/AUDIT_20261103_TH.md). `--from-head` hashes the tree of git HEAD instead, exported
with `git archive` into a directory of its own, and `--export-to DIR` keeps that export so
the deploy uploads exactly the bytes it hashed.
"""
import argparse
import hashlib
import io
import json
import subprocess
import tarfile
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
STREAMLIT = ROOT.parent / "lego-firebase-streamlit"
EXCLUDED_PARTS = {
    ".git", ".pytest_cache", ".cache-v4", ".runtime-artifacts", ".pytest_tmp", ".tools", ".venv", ".venv-release",
    "__pycache__", "release_evidence", "release_evidence_v2",
    ".audit-cache", ".review-runtime", ".firebase",
}
INCLUDED_SUFFIXES = {".py", ".json", ".md", ".txt", ".ps1", ".rules", ".whl",
                     ".yaml", ".yml", ".html", ".sh"}
INCLUDED_NAMES = {".gcloudignore", ".gitignore"}


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def source_files(root):
    for path in root.rglob("*"):
        parts = path.relative_to(root).parts
        excluded = (bool(EXCLUDED_PARTS.intersection(parts))
                    or any(part.startswith(".venv") for part in parts)
                    or ".review-envs" in parts)
        if (path.is_file() and (path.suffix.lower() in INCLUDED_SUFFIXES or path.name.endswith(".env.example")
                or path.name in INCLUDED_NAMES) and not excluded):
            yield path


def backend_files():
    yield from source_files(ROOT)


def _manifest_for(root: Path, reader: Path | None) -> dict:
    """The manifest of the backend at *root* plus, when it is a directory, the reader."""
    files = {
        f"backend/{path.relative_to(root).as_posix()}": digest(path)
        for path in source_files(root)
    }
    with_reader = reader is not None and reader.is_dir()
    if with_reader:
        for path in source_files(reader):
            files[f"reader/{path.relative_to(reader).as_posix()}"] = digest(path)
    canonical = json.dumps(files, sort_keys=True, separators=(",", ":"))
    dependency_files = {
        name: digest(root / name)
        for name in ("requirements.txt", "requirements-dev.txt")
    }
    if with_reader and (reader / "requirements.txt").is_file():
        dependency_files["reader/requirements.txt"] = digest(reader / "requirements.txt")
    dependency_lock_hash = hashlib.sha256(json.dumps(
        dependency_files, sort_keys=True, separators=(",", ":")
    ).encode()).hexdigest()
    return {
        "algorithm": "sha256(canonical path-to-sha256 map)",
        "candidate_hash": hashlib.sha256(canonical.encode()).hexdigest(),
        "dependency_lock_hash": dependency_lock_hash,
        "dependency_files": dependency_files,
        "file_count": len(files),
        "scope": "backend-and-reader" if with_reader else "backend-only",
        "files": files,
    }


def build_manifest():
    # ROOT and STREAMLIT are read at call time: tools.trace_audit swaps them.
    return _manifest_for(ROOT, STREAMLIT)


def _git(repo: Path, *args: str, text: bool = True):
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, text=text).stdout


def _extract(archive: bytes, target: Path) -> None:
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        if hasattr(tarfile, "data_filter"):
            tar.extractall(target, filter="data")
            return
        # Python without extraction filters: plain files and directories inside target only.
        base = target.resolve()
        for member in tar.getmembers():
            destination = (target / member.name).resolve()
            if not (member.isfile() or member.isdir()) or (
                    destination != base and base not in destination.parents):
                raise ValueError(f"unexpected archive member {member.name!r}")
        tar.extractall(target)


def export_head(repo: Path, target: Path) -> str:
    """Write the tree of git HEAD into *target* (which must not exist yet); return the commit."""
    commit = _git(repo, "rev-parse", "--verify", "HEAD^{commit}").strip()
    if target.exists():
        raise FileExistsError(f"{target} already exists; the export goes into a new directory")
    archive = _git(repo, "archive", "--format=tar", commit, text=False)
    target.mkdir(parents=True)
    _extract(archive, target)
    return commit


def head_manifest(repo: Path | None = None, export_to: Path | None = None) -> dict:
    """The backend-only manifest of git HEAD, whatever else the checkout holds.

    The tree is exported with `git archive`: no ignored or untracked file, no sibling reader
    checkout, and the result equals the walk of a clean clone of that commit.
    """
    repo = Path(repo or ROOT)
    if export_to is None:
        with tempfile.TemporaryDirectory(prefix="lego-candidate-") as scratch:
            return head_manifest(repo, Path(scratch) / "src")
    target = Path(export_to)
    commit = export_head(repo, target)
    manifest = _manifest_for(target, None)
    manifest["source"] = f"git HEAD {commit}"
    return manifest


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--from-head", action="store_true",
                        help="hash the tree of git HEAD (git archive), not the working directory")
    parser.add_argument("--export-to", type=Path,
                        help="with --from-head: write that tree here (a new directory) and hash it")
    args = parser.parse_args(argv)
    if args.export_to is not None and not args.from_head:
        parser.error("--export-to needs --from-head")
    manifest = head_manifest(ROOT, args.export_to) if args.from_head else build_manifest()
    print(json.dumps(manifest, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
