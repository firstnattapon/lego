"""Print deterministic candidate identity for the deployable backend + v2 reader."""
import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
STREAMLIT = ROOT.parent / "lego-firebase-streamlit"
EXCLUDED_PARTS = {
    ".git", ".pytest_cache", ".tools", ".venv", ".venv-release",
    "__pycache__", "release_evidence", "release_evidence_v2",
    ".audit-cache", ".review-runtime", ".firebase",
}
INCLUDED_SUFFIXES = {".py", ".json", ".md", ".txt", ".ps1", ".rules", ".whl",
                     ".yaml", ".yml", ".html"}
INCLUDED_NAMES = {".gcloudignore", ".gitignore"}


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def source_files(root):
    for path in root.rglob("*"):
        parts = path.relative_to(root).parts
        excluded = (bool(EXCLUDED_PARTS.intersection(parts))
                    or any(part.startswith(".venv") for part in parts)
                    or ".review-envs" in parts)
        if (path.is_file() and (path.suffix.lower() in INCLUDED_SUFFIXES
                or path.name in INCLUDED_NAMES) and not excluded):
            yield path


def backend_files():
    yield from source_files(ROOT)


def build_manifest():
    files = {
        f"backend/{path.relative_to(ROOT).as_posix()}": digest(path)
        for path in backend_files()
    }
    for path in source_files(STREAMLIT):
        files[f"reader/{path.relative_to(STREAMLIT).as_posix()}"] = digest(path)
    canonical = json.dumps(files, sort_keys=True, separators=(",", ":"))
    dependency_files = {
        name: digest(ROOT / name)
        for name in ("requirements.txt", "requirements-dev.txt")
    }
    if (STREAMLIT / "requirements.txt").is_file():
        dependency_files["reader/requirements.txt"] = digest(STREAMLIT / "requirements.txt")
    dependency_lock_hash = hashlib.sha256(json.dumps(
        dependency_files, sort_keys=True, separators=(",", ":")
    ).encode()).hexdigest()
    return {
        "algorithm": "sha256(canonical path-to-sha256 map)",
        "candidate_hash": hashlib.sha256(canonical.encode()).hexdigest(),
        "dependency_lock_hash": dependency_lock_hash,
        "dependency_files": dependency_files,
        "file_count": len(files),
        "scope": "backend-and-reader" if STREAMLIT.is_dir() else "backend-only",
        "files": files,
    }


def main():
    print(json.dumps(build_manifest(), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
