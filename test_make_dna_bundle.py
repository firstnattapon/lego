"""DNA bundle generator: reproduces the repo's bundles and refuses unsafe input."""
import json
import os
from pathlib import Path

import pytest

from config import ConfigurationError, load_runtime_config
from operational_health import dna_end
from tools import make_dna_bundle as tool

ROOT = Path(__file__).parent
ORIGIN = "2026-09-08T13:30:00Z"


def generate(tmp_path, code, **overrides):
    out = tmp_path / "bundle.json"
    argv = ["--dna-code", code, "--origin", overrides.get("origin", ORIGIN),
            "--interval", str(overrides.get("interval", 900)), "--output", str(out)]
    assert tool.main(argv) == 0
    return out


def test_tool_reproduces_the_existing_example_bundle_byte_for_byte(tmp_path):
    assert generate(tmp_path, "bypass:500").read_bytes() == (ROOT / "strategy.example.json").read_bytes()


def test_committed_uat_continuous_bundle_is_what_the_tool_generates(tmp_path):
    assert generate(tmp_path, "bypass:6500").read_bytes() == (
        ROOT / "strategy.uat-continuous.json").read_bytes()


def test_uat_continuous_bundle_loads_and_outlives_the_incident_dna(monkeypatch):
    env = {"LEGO_SYMBOL": "UBER", "LEGO_FIX_C": "10000", "LEGO_DIFF": "25",
           "WEBULL_ACCOUNT_ID": "test-account",
           "LEGO_DNA_BUNDLE": str(ROOT / "strategy.uat-continuous.json")}
    bundle = load_runtime_config(env).operator.dna_bundle
    assert bundle.dna_code == "bypass:6500" and bundle.explicit_bypass
    assert bundle.origin_utc == ORIGIN and bundle.interval_seconds == 900
    # Same origin and slot grid as strategy.example.json: ordinals keep counting.
    assert dna_end(ORIGIN, 6500, 900, "x") == "2027-09-07T19:30:00+00:00"
    assert dna_end(ORIGIN, 500, 900, "x") == "2026-10-05T15:00:00+00:00"


def test_an_existing_file_is_never_overwritten(tmp_path, capsys):
    out = generate(tmp_path, "bypass:6500")
    before = out.read_bytes()
    assert tool.main(["--dna-code", "bypass:10", "--origin", ORIGIN, "--output", str(out)]) == 2
    assert out.read_bytes() == before
    assert "refusing to overwrite" in capsys.readouterr().err


@pytest.mark.parametrize("origin", [
    "2026-09-08T13:30:00",          # no timezone
    "2026-10-03T14:00:00Z",         # a Saturday
    "2026-09-08T08:00:00Z"])        # before the open
def test_origin_must_be_inside_a_regular_session(tmp_path, origin):
    with pytest.raises(ValueError):
        tool.build_bundle("bypass:10", origin, 900)


@pytest.mark.parametrize("interval", [1000,      # not an allowed slot size
                                      7800])     # allowed grid is 900/1800/3600/4h/1d
def test_unsupported_slot_size_is_rejected(interval):
    with pytest.raises(ValueError):
        tool.build_bundle("bypass:10", ORIGIN, interval)


def test_a_slot_size_that_does_not_divide_the_session_fails_bundle_validation():
    # 14400 is a supported grid for yfinance bars but 6.5h is not a multiple of 4h.
    with pytest.raises(ConfigurationError):
        tool.build_bundle("bypass:10", ORIGIN, 14400)


@pytest.mark.parametrize("argv_tail,message", [
    (["--dna-code", "not-a-dna", "--origin", ORIGIN], "error:"),
    (["--dna-code", "bypass:10", "--origin", "2026-10-03T14:00:00Z"], "regular")])
def test_bad_input_is_a_clean_error_and_writes_nothing(tmp_path, capsys, argv_tail, message):
    out = tmp_path / "bundle.json"
    assert tool.main([*argv_tail, "--output", str(out)]) == 2
    assert message in capsys.readouterr().err and not out.exists()


def test_environment_is_left_as_it_was(monkeypatch):
    monkeypatch.setenv("LEGO_SLOT_SECONDS", "3600")
    monkeypatch.delenv("LEGO_DNA_ORIGIN_UTC", raising=False)
    tool.build_bundle("bypass:10", ORIGIN, 900)
    assert os.environ["LEGO_SLOT_SECONDS"] == "3600"
    assert "LEGO_DNA_ORIGIN_UTC" not in os.environ


def test_printed_summary_names_the_dna_end(tmp_path, capsys):
    generate(tmp_path, "bypass:6500")
    summary = json.loads(capsys.readouterr().out)
    assert summary["length"] == 6500 and summary["dna_end_utc"].startswith("2027-09-07T19:30:00")
    assert summary["decoded_array_sha256"] == json.loads(
        (ROOT / "strategy.uat-continuous.json").read_text())["decoded_array_sha256"]
