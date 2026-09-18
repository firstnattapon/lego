"""Golden UI, calculation, and output contracts from f8388a."""
from __future__ import annotations

import dataclasses
import hashlib
import json
import re
from html.parser import HTMLParser
from pathlib import Path

import pytest

from dna_engine import decode_dna, dna_fingerprint, parse_dna_spec
from lego_one_row import (
    Anchor,
    COLUMN_ORDER,
    Config,
    ExecutionFill,
    build_decision,
    compute_recurrence,
    compute_row,
    finalize_recurrence,
)


HERE = Path(__file__).resolve().parent
GUIDE = HERE / "LEARNING_GUIDE_TH.html"
DNA_CODE = "21222524217299"

EXPECTED_COLUMNS = [
    "เวลา (UTC)",
    "สินทรัพย์",
    "สถานะ",
    "DNA step",
    "DNA signal",
    "ราคา Pₙ (USD)",
    "จำนวนถือครอง (หุ้น)",
    "คำสั่ง",
    "ฝั่ง",
    "เหตุผล",
    "จำนวนสั่ง (หุ้น)",
    "มูลค่าพอร์ต (USD)",
    "ส่วนต่างเป้าหมาย (USD)",
    "Rₙ อ้างอิง (USD)",
    "ΔAₙ ต่อสเต็ป (USD)",
    "ΔAₙ เงินจริง (USD)",
    "Aₙ สะสม (USD)",
    "Eₙ ส่วนเกินสะสม (USD)",
]

CFG = Config(
    symbol="APLS",
    fix_c=1500.0,
    diff=60.0,
    dna_code=DNA_CODE,
    decimal_precision=5,
)
ANCHOR = Anchor(
    version=7,
    dna_step=4,
    p0=6.88,
    prev_price=6.88,
    prev_actual=0.0,
    prev_holdings=200.0,
)
SNAPSHOT = {
    "captured_at": "2026-07-23T18:00:00Z",
    "price": 6.5,
    "holdings": 200.0,
}


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _capture(pattern: str, text: str) -> str:
    match = re.search(pattern, text, re.DOTALL)
    assert match is not None
    return match.group(1)


class _DomShape(HTMLParser):
    def __init__(self):
        super().__init__()
        self.starts: list[tuple[str, dict[str, str | None]]] = []

    def handle_starttag(self, tag, attrs):
        self.starts.append((tag, dict(attrs)))

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)


def test_17_column_schema_and_order_match_f8388a_baseline():
    assert COLUMN_ORDER == EXPECTED_COLUMNS
    assert len(COLUMN_ORDER) == 18
    encoded = json.dumps(
        COLUMN_ORDER, ensure_ascii=False, separators=(",", ":"))
    assert _sha256(encoded) == (
        "e7a3e7dda3b7d8df10215f0ef7a80330f00e614c39ea00c1673e2d81cb906b2d"
    )


def test_representative_dna_decode_matches_f8388a_baseline():
    assert dataclasses.asdict(parse_dna_spec(DNA_CODE)) == {
        "kind": "stream",
        "length": 12,
        "dna_seed": 42,
        "mutation_rate": 0.25,
        "mutation_seeds": (7, 99),
    }
    assert decode_dna(DNA_CODE) == [1, 1, 1, 1, 0, 1, 1, 1, 0, 0, 1, 0]
    assert dna_fingerprint(DNA_CODE) == "f27afd6c9e3c250d"


@pytest.mark.parametrize(
    ("price", "holdings", "signal", "expected"),
    [
        (6.5, 200.0, 1, {
            "status": "READY_BUY",
            "action": "TRIGGER_ACTION",
            "side": "BUY",
            "reason": "READY_BUY",
            "quantity": 30.76923,
            "value": 1300.0,
            "gap": -200.0,
        }),
        (7.3, 200.0, 1, {
            "status": "PASS_THRESHOLD",
            "action": "PASS",
            "side": "",
            "reason": "PASS_THRESHOLD",
            "quantity": 0.0,
            "value": 1460.0,
            "gap": -40.0,
        }),
        (6.5, 200.0, 0, {
            "status": "PASS_DNA_ZERO",
            "action": "PASS",
            "side": "",
            "reason": "PASS_DNA_ZERO",
            "quantity": 0.0,
            "value": 1300.0,
            "gap": -200.0,
        }),
        (8.0, 200.0, 1, {
            "status": "READY_SELL",
            "action": "TRIGGER_ACTION",
            "side": "SELL",
            "reason": "READY_SELL",
            "quantity": 12.5,
            "value": 1600.0,
            "gap": 100.0,
        }),
    ],
)
def test_representative_decision_matrix_matches_f8388a_baseline(
        price, holdings, signal, expected):
    assert dataclasses.asdict(
        build_decision(CFG, price, holdings, signal)) == expected


def test_representative_recurrence_matches_f8388a_baseline():
    acted = dataclasses.asdict(
        compute_recurrence(CFG, 6.5, ANCHOR, acted=True))
    passed = dataclasses.asdict(
        compute_recurrence(CFG, 6.5, ANCHOR, acted=False))
    assert acted == pytest.approx({
        "R": -85.22471256549127,
        "dA": -82.8488372093023,
        "A": -82.8488372093023,
        "E": 2.3758753561889705,
        "acted_price_next": 6.5,
    })
    assert passed == pytest.approx({
        "R": -85.22471256549127,
        "dA": 0.0,
        "A": 0.0,
        "E": 0.0,
        "acted_price_next": 6.88,
    })


def test_representative_row_output_matches_f8388a_baseline():
    """Every decision column is byte-for-byte the f8388a baseline.

    The three cashflow columns are the deliberate exception: a READY_BUY is an
    intent, so the row now commits them carried forward and lego_order_worker
    books the baseline numbers once the fill is confirmed — which the test below
    checks against the very same golden values.
    """
    row = compute_row(CFG, SNAPSHOT, ANCHOR, dna_step=5)
    assert list(row) == [*EXPECTED_COLUMNS, "_meta"]
    assert {
        key: row[key]
        for key in EXPECTED_COLUMNS
        if key not in {
            "Rₙ อ้างอิง (USD)",
            "ΔAₙ ต่อสเต็ป (USD)",
            "ΔAₙ เงินจริง (USD)",
            "Aₙ สะสม (USD)",
            "Eₙ ส่วนเกินสะสม (USD)",
        }
    } == {
        "เวลา (UTC)": "2026-07-23T18:00:00Z",
        "สินทรัพย์": "APLS",
        "สถานะ": "READY_BUY",
        "DNA step": 5,
        "DNA signal": 1,
        "ราคา Pₙ (USD)": 6.5,
        "จำนวนถือครอง (หุ้น)": 200.0,
        "คำสั่ง": "TRIGGER_ACTION",
        "ฝั่ง": "BUY",
        "เหตุผล": "READY_BUY",
        "จำนวนสั่ง (หุ้น)": 30.76923,
        "มูลค่าพอร์ต (USD)": 1300.0,
        "ส่วนต่างเป้าหมาย (USD)": -200.0,
    }
    # Rₙ is unchanged: it is live on every row and never moved to the worker.
    assert row["Rₙ อ้างอิง (USD)"] == pytest.approx(-85.22471256549127)
    # Carried forward until a fill is confirmed. P_acted is still P₀ here, so
    # the pass form of Eₙ (Aₙ − fix·ln(P_acted/P₀)) is 0.
    assert row["ΔAₙ ต่อสเต็ป (USD)"] == 0.0
    assert row["Aₙ สะสม (USD)"] == pytest.approx(ANCHOR.prev_actual)
    assert row["Eₙ ส่วนเกินสะสม (USD)"] == pytest.approx(0.0)

    meta = dict(row["_meta"])
    actual_next = meta.pop("actual_next")
    assert meta == {
        "step": 5,
        "price": 6.5,
        "p0_next": 6.88,
        "acted": True,
        "execution_pending": True,
        "acted_price_next": 6.88,
        "status": "READY_BUY",
        "side": "BUY",
        "quantity": 30.76923,
        "action": "TRIGGER_ACTION",
    }
    assert actual_next == pytest.approx(ANCHOR.prev_actual)


def test_confirmed_fill_reproduces_the_f8388a_cashflow_baseline():
    """The moved arithmetic, unchanged: same inputs, same three numbers.

    finalize_recurrence at the decision price must land exactly where the f8388a
    act branch of compute_recurrence did — the only difference in production is
    that the price is the broker's fill, not the decision's quote.
    """
    row = compute_row(CFG, SNAPSHOT, ANCHOR, dna_step=5)
    final = finalize_recurrence(
        CFG,
        ExecutionFill(filled_price=SNAPSHOT["price"],
                      filled_quantity=row["จำนวนสั่ง (หุ้น)"],
                      holdings_after=SNAPSHOT["holdings"] + row["จำนวนสั่ง (หุ้น)"]),
        last_action_price=ANCHOR.prev_price,
        actual_cumulative=ANCHOR.prev_actual,
        reference_R=row["Rₙ อ้างอิง (USD)"])
    assert dataclasses.asdict(final) == pytest.approx({
        "dA": -82.8488372093023,
        "A": -82.8488372093023,
        "E": 2.3758753561889705,
        "acted_price_next": 6.5,
    })


def test_learning_guide_normalized_source_hash_matches_f8388a():
    html = GUIDE.read_text(encoding="utf-8")
    assert _sha256(html) == (
        "43b95a7ddf25f35cf5e94cade882918c14d907b73fa4d2d0d3ec1403057930c0"
    )


def test_learning_guide_embedded_contract_hashes_match_f8388a():
    html = GUIDE.read_text(encoding="utf-8")
    blocks = {
        "flow": _capture(r"const flow=(\[.*?\]);\s*//", html),
        "columns": _capture(
            r"const columns=(\[.*?\]);cols\.innerHTML", html),
        "quiz": _capture(r"const qs=(\[.*?\]),picked=", html),
    }
    assert {name: _sha256(value) for name, value in blocks.items()} == {
        "flow": "7ae6a2e681c04574d465a2a1f32989c216794e07675ac38b9f89fe2473df979e",
        "columns": "119bb5c317a3d89e8e19e716c0719271cd7d2daa38a518bad94fff0ff6169573",
        "quiz": "ab7f80952b487c31c1ae25d67ec7bcf017a0a1b5c0394c6b4337ee8624cf1cea",
    }
    labels = re.findall(r"'([^']*)'", blocks["columns"])
    assert [label.split("|", 1)[0] for label in labels] == [
        c for c in EXPECTED_COLUMNS if c != "ΔAₙ เงินจริง (USD)"
    ]


def test_learning_guide_dom_shape_matches_f8388a():
    html = GUIDE.read_text(encoding="utf-8")
    parser = _DomShape()
    parser.feed(html)
    sections = [
        attrs.get("id") for tag, attrs in parser.starts if tag == "section"
    ]
    nav = [
        attrs.get("href")
        for tag, attrs in parser.starts
        if tag == "a" and str(attrs.get("href", "")).startswith("#")
    ]
    ids = [
        attrs["id"] for _, attrs in parser.starts if "id" in attrs
    ]
    shape = [
        (
            tag,
            attrs.get("id"),
            attrs.get("class"),
            attrs.get("href"),
            attrs.get("data-i"),
            attrs.get("type"),
        )
        for tag, attrs in parser.starts
    ]
    encoded_shape = json.dumps(
        shape, ensure_ascii=False, separators=(",", ":"))
    assert sections == [
        "flow", "files", "decision", "math", "columns",
        "db", "guards", "status", "quiz",
    ]
    assert nav == [f"#{section}" for section in sections]
    assert ids == [
        "flow", "detail", "files", "decision", "fix", "diff", "price",
        "hold", "sig", "prec", "dstatus", "value", "gap", "qty", "side",
        "dcode", "math", "rf", "p0", "pp", "pa", "pn", "acted", "rr",
        "rda", "ra", "re", "rcode", "columns", "cols", "db", "guards",
        "status", "quiz", "quizbox",
    ]
    assert _sha256(encoded_shape) == (
        "0230b65a95578595edb60beb644a03ce3c7ef6ce85545d14bb93a7ce9b803809"
    )
