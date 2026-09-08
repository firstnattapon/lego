"""Guards for the findings a static CodeFlow scan raised against this repo.

The scan reported five things. Three were real (`dna_summary`,
`dna_origin_utc`, `columns_presented` had no caller anywhere) and two were
artifacts of pattern matching: an "XSS" on a page whose every interpolated
value is a literal in the same file, and a "Function constructor" on a
Markdown file that only contains the words "Cloud Function".

A report is a snapshot; these are the checks that keep the answers true. Each
test states which finding it stands for, so the next scan can be answered from
the suite instead of by re-reading every file.
"""
from __future__ import annotations

import ast
import os
import re

from lego_one_row import COLUMN_ORDER, columns_presented

HERE = os.path.dirname(os.path.abspath(__file__))
GUIDE = os.path.join(HERE, "LEARNING_GUIDE_TH.html")


def _python_files() -> dict[str, str]:
    return {name: open(os.path.join(HERE, name), encoding="utf-8").read()
            for name in sorted(os.listdir(HERE)) if name.endswith(".py")}


def _is_production(name: str) -> bool:
    return not name.startswith("test_") and name != "conftest.py"


def _local_imports(source: str, modules: set[str]) -> set[str]:
    """Local modules *source* imports, at any nesting depth.

    Function-level imports count: `webull_io` defers the SDK imports into
    `build_clients`, and a cycle hidden inside a function is still a cycle.
    Third-party names drop out because they are not repo modules.
    """
    found = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            found.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module.split(".")[0])
    return found & modules


# --- Finding: "6 Circular Dependencies" -------------------------------------
def test_import_graph_has_no_cycles():
    """The scan named six cycles, including `main.py <-> webull_io.py`.

    None exist: the repo is a strict DAG rooted at `main.py`, and test modules
    import production modules and are imported by nothing. What the scan
    measured was two files mentioning each other's names, which a docstring is
    enough to do. This walks real import statements instead.
    """
    files = _python_files()
    modules = {name[:-3] for name in files}
    graph = {name[:-3]: _local_imports(src, modules) for name, src in files.items()}

    visiting, done, cycles = set(), set(), []

    def walk(node, path):
        if node in done:
            return
        if node in visiting:
            cycles.append(" -> ".join(path + [node]))
            return
        visiting.add(node)
        for nxt in sorted(graph.get(node, ())):
            walk(nxt, path + [node])
        visiting.discard(node)
        done.add(node)

    for module in sorted(graph):
        walk(module, [])
    assert cycles == [], f"import cycles: {cycles}"


def test_no_production_module_imports_a_test_module():
    """The half of the cycle claim that would matter if it were true.

    `lego_one_row.py <-> test_lego_fixes.py` can only be a cycle if the engine
    imports its own tests, which would ship test doubles into the money path.
    """
    files = _python_files()
    modules = {name[:-3] for name in files}
    for name, source in files.items():
        if not _is_production(name):
            continue
        leaked = {m for m in _local_imports(source, modules)
                  if not _is_production(f"{m}.py")}
        assert not leaked, f"{name} imports test module(s): {sorted(leaked)}"


# --- Finding: "3 Unused Functions" ------------------------------------------
def test_every_production_function_is_referenced_somewhere():
    """Keeps the dead-code count at zero instead of re-discovering it later.

    A function referenced only by tests is not dead — `reset_clients` and
    `row_is_committed` exist so the suite can reach a seam — so tests count as
    references. Zero references anywhere is the condition that flagged
    `dna_summary` and `dna_origin_utc`, both now removed.
    """
    files = _python_files()
    orphans = []
    for name, source in files.items():
        if not _is_production(name):
            continue
        for node in ast.parse(source).body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            pattern = re.compile(r"\b" + re.escape(node.name) + r"\b")
            refs = sum(len(pattern.findall(text)) for text in files.values())
            # One hit is the `def` line itself.
            if refs <= 1:
                orphans.append(f"{name}:{node.lineno} {node.name}")
    assert orphans == [], f"functions with no reference at all: {orphans}"


# --- Finding: "columns_presented() has zero calls" --------------------------
# Kept rather than deleted: it is the single written definition of how the row
# is rounded for display, and the dashboard repo points at it by name. Pinning
# the behaviour is what turns it from unverified code into a contract.
def test_columns_presented_rounds_only_the_seven_money_columns():
    row = {key: 1.005 for key in COLUMN_ORDER}
    row["เวลา (UTC)"] = "2026-07-27T13:30:00Z"
    row["สินทรัพย์"] = "FFIV"
    row["สถานะ"] = "READY_BUY"
    row["DNA step"] = 7
    row["DNA signal"] = 1
    row["คำสั่ง"] = "TRIGGER_ACTION"
    row["ฝั่ง"] = "BUY"
    row["เหตุผล"] = "READY_BUY"

    out = columns_presented(row)

    assert list(out.keys()) == COLUMN_ORDER
    money = ["ราคา Pₙ (USD)", "มูลค่าพอร์ต (USD)", "ส่วนต่างเป้าหมาย (USD)",
             "Rₙ อ้างอิง (USD)", "ΔAₙ ต่อสเต็ป (USD)", "Aₙ สะสม (USD)",
             "Eₙ ส่วนเกินสะสม (USD)"]
    assert len(money) == 7
    for key in money:
        assert out[key] == round(1.005, 2)
    # Quantity keeps full precision here: it is already rounded to
    # decimal_precision when the decision is built, and rounding it again to 2
    # would misreport the order size on a 5-decimal chain.
    assert out["จำนวนสั่ง (หุ้น)"] == 1.005
    assert out["สถานะ"] == "READY_BUY"
    assert out["DNA step"] == 7


def test_columns_presented_never_mutates_or_leaks_meta():
    row = {key: 0.0 for key in COLUMN_ORDER}
    row["มูลค่าพอร์ต (USD)"] = 12.3456
    row["_meta"] = {"acted": True}

    out = columns_presented(row)

    assert "_meta" not in out
    assert row["มูลค่าพอร์ต (USD)"] == 12.3456


# --- Finding: "HIGH: XSS Vulnerability in LEARNING_GUIDE_TH.html" -----------
# Not exploitable when it was raised: every value reaching innerHTML is a
# literal defined a few lines above it, and the only user input on the page
# (the calculator fields) is numeric and written with textContent. The finding
# is still worth closing, because "no untrusted input yet" is a property of
# today's content, not of the code. Escaping is invisible — an entity renders
# as the character it replaced — so the page looks and behaves identically.
#
# Each entry is (raw fragment that must be gone, escaped fragment that must be
# there). The pairs quote the surrounding markup on purpose: the same names also
# appear in textContent templates, where escaping would be wrong — an entity
# there would be shown to the reader instead of the character.
GUARDED_SINKS = (
    ('<span class="tag">${flow[i][0]}</span>',
     '<span class="tag">${esc(flow[i][0])}</span>'),
    ("<h3>${flow[i][1]}</h3>", "<h3>${esc(flow[i][1])}</h3>"),
    ('<div class="code">${flow[i][2]}</div>',
     '<div class="code">${esc(flow[i][2])}</div>'),
    ('<span class="status ${cl}">${st}</span>',
     '<span class="status ${esc(cl)}">${esc(st)}</span>'),
    ("<b>${p[0]}</b>", "<b>${esc(p[0])}</b>"),
    ("<td>${p.slice(1).join('|')}</td>", "<td>${esc(p.slice(1).join('|'))}</td>"),
    ("${qi+1}. ${q[0]}</b>", "${qi+1}. ${esc(q[0])}</b>"),
    (">${a}</button>", ">${esc(a)}</button>"),
)


def test_learning_guide_escapes_every_innerhtml_interpolation():
    html = open(GUIDE, encoding="utf-8").read()
    for raw, escaped in GUARDED_SINKS:
        assert escaped in html, f"missing escaped sink: {escaped}"
        assert raw not in html, f"raw value still reaches innerHTML: {raw}"


def test_learning_guide_escape_helper_covers_every_markup_character():
    html = open(GUIDE, encoding="utf-8").read()
    table = re.search(r"const ESCAPES=\{(.+?)\}", html)
    assert table, "no ESCAPES table"
    pairs = dict(re.findall(r"""['"](.)['"]:['"](&[a-z#0-9]+;)['"]""", table.group(1)))
    assert pairs == {"&": "&amp;", "<": "&lt;", ">": "&gt;",
                     '"': "&quot;", "'": "&#39;"}
    charclass = re.search(r"replace\(/\[([^\]]+)\]/g", html)
    assert charclass, "no escaping character class"
    assert set(charclass.group(1)) == set(pairs)


def test_learning_guide_declares_a_content_security_policy():
    """Second layer: the page loads nothing and talks to nothing.

    Inline script and style have to stay allowed — they are the page — so the
    policy cannot stop injected inline code by itself. What it does remove is
    every channel such code would need: no network origin, no remote script,
    no form target.
    """
    html = open(GUIDE, encoding="utf-8").read()
    match = re.search(r'http-equiv="Content-Security-Policy"\s+content="([^"]+)"', html)
    assert match, "no CSP meta tag"
    policy = match.group(1)
    for directive in ("default-src 'none'", "form-action 'none'", "base-uri 'none'"):
        assert directive in policy, f"CSP missing {directive}"


def test_learning_guide_has_no_dynamic_code_execution():
    """The other scanner hit — 'Function constructor' — was matched on the
    words 'Cloud Function' in README.md, which is Markdown and runs nothing.
    The page it should have looked at has no eval and no Function() either."""
    html = open(GUIDE, encoding="utf-8").read()
    assert not re.search(r"\beval\s*\(", html)
    assert not re.search(r"\bnew\s+Function\s*\(", html)
    assert "srcdoc" not in html and "document.write" not in html
