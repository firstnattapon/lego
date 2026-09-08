"""One-shot, deterministic extraction of orchestration from the reviewed main.py.

Kept as an auditable mechanical transform: the source slices are asserted before
writing, and the generated services retain the exact reviewed function bodies.
"""
from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MAIN = ROOT / "main.py"


def _bound_names(source: str) -> list[str]:
    tree = ast.parse(source)
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                names.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
    return sorted(names)


def main() -> None:
    source = MAIN.read_text(encoding="utf-8")
    if "Thin Cloud Functions HTTP boundary" in source:
        raise SystemExit("services already extracted")
    lines = source.splitlines(keepends=True)
    assert lines[100].startswith("class FillNotConfirmed")
    assert lines[1048].startswith("def _outbox_intent")
    assert lines[1067].startswith("def _run_order_worker")
    assert lines[1203].startswith("@functions_framework.http")
    assert lines[1204].startswith("def lego_one_row")
    assert lines[1491].startswith("@functions_framework.http")

    common = "".join(lines[11:100])
    injectable = _bound_names(common)
    inject_literal = repr(injectable)

    execution_body = "".join(lines[100:1048] + lines[1067:1203])
    execution = (
        '"""Execution orchestration for durable broker dispatch and recovery."""\n'
        + common
        + f"\n_INJECTABLE = {inject_literal}\n\n"
        + "def configure(deps) -> None:\n"
          "    \"\"\"Inject the HTTP facade dependencies for deterministic tests/wiring.\"\"\"\n"
          "    target = globals()\n"
          "    for name in _INJECTABLE:\n"
          "        if hasattr(deps, name):\n"
          "            target[name] = getattr(deps, name)\n\n\n"
        + execution_body
    )

    decision_helper = "".join(lines[1048:1067])
    decision_body = list(lines[1204:1491])
    decision_body[0] = decision_body[0].replace(
        "def lego_one_row(request):", "def run_decision(request):")
    decision = (
        '"""Decision orchestration: snapshot to atomic row and recoverable intent."""\n'
        + common
        + "\n_DECISION_PRIVATE_DEPS = [\n"
          "    '_announce_identity_adoption', '_error_text', '_init_firebase',\n"
          "    '_iso', '_min_dna_remaining', '_record_warning',\n"
          "    '_recover_pending_order_intents', '_run_order_worker',\n"
          "]\n"
        + f"_INJECTABLE = {inject_literal} + _DECISION_PRIVATE_DEPS\n\n"
        + "def configure(deps) -> None:\n"
          "    \"\"\"Inject composition-root dependencies without importing main.\"\"\"\n"
          "    target = globals()\n"
          "    for name in _INJECTABLE:\n"
          "        if hasattr(deps, name):\n"
          "            target[name] = getattr(deps, name)\n\n\n"
        + decision_helper
        + "".join(decision_body)
    )

    funcs = []
    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.lineno < 1204:
            if node.name != "_outbox_intent":
                funcs.append(node.name)
    wrappers = []
    for name in funcs:
        wrappers.append(
            f"def {name}(*args, **kwargs):\n"
            f"    return _execution_call('{name}', *args, **kwargs)\n\n\n"
        )
    facade = (
        '"""Thin Cloud Functions HTTP boundary and backward-compatible test facade."""\n'
        + common
        + "\nimport sys\n\n"
          "import decision_service\n"
          "import execution_service\n\n"
          "FillNotConfirmed = execution_service.FillNotConfirmed\n"
          "RealizedMathError = execution_service.RealizedMathError\n\n"
          "def _facade():\n"
          "    return sys.modules[__name__]\n\n\n"
          "def _execution_call(name, *args, **kwargs):\n"
          "    execution_service.configure(_facade())\n"
          "    return getattr(execution_service, name)(*args, **kwargs)\n\n\n"
        + "".join(wrappers)
        + "def _outbox_intent(*args, **kwargs):\n"
          "    decision_service.configure(_facade())\n"
          "    return decision_service._outbox_intent(*args, **kwargs)\n\n\n"
          "@functions_framework.http\n"
          "def lego_one_row(request):\n"
          "    decision_service.configure(_facade())\n"
          "    return decision_service.run_decision(request)\n\n\n"
        + "".join(lines[1491:])
    )

    (ROOT / "execution_service.py").write_text(execution, encoding="utf-8")
    (ROOT / "decision_service.py").write_text(decision, encoding="utf-8")
    MAIN.write_text(facade, encoding="utf-8")


if __name__ == "__main__":
    main()
