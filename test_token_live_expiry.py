"""PROD token: Webull's Check Token answer decides the expiry, not the copy in the secret.

The secret's second line is the expiry Webull answered when the token was issued.
Webull ends a token at the expiry its Create/Check Token answer carries (named
`expires` by the SDK and PROD, `expires_at` by the reference) and after 15 days
without an API call, and the answer today can differ from the copy kept at issue.
These tests pin the order build_clients keeps: Check Token first, and the SDK
constructors (each calls create_token, which for a dead token would start a 2FA)
only after it answers NORMAL with time left.
"""
import io
import json
import logging
import sys
import types
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import auth_circuit
import ops
import tick_runtime
import webull_io
from conftest import FAKE_DB
from test_webull_io import _install_fake_sdk

UTC = timezone.utc
TOKEN = "fake-token-" + "0" * 21                     # 32 characters, like Webull's


def _at(delta: timedelta) -> int:
    return int((datetime.now(UTC) + delta).timestamp() * 1000)


def _secret(expires: int, token: str = TOKEN):
    data = f"{token}\n{expires}\nNORMAL\n".encode()
    return SimpleNamespace(access_secret_version=lambda **_: SimpleNamespace(
        payload=SimpleNamespace(data=data)))


def _answer(status="NORMAL", left=timedelta(days=14), token=TOKEN, field="expires", http=200):
    payload = {"token": token, "status": status}
    if left is not None:
        payload[field] = _at(left)
    return SimpleNamespace(status_code=http, json=lambda: payload)


@pytest.fixture
def prod(monkeypatch, tmp_path):
    FAKE_DB.store.clear()
    monkeypatch.setenv("WEBULL_ENV", "PROD")
    monkeypatch.setenv("WEBULL_TOKEN_SECRET", "projects/p/secrets/webull-token-prod")
    monkeypatch.setenv("WEBULL_TOKEN_DIR", str(tmp_path))
    monkeypatch.setenv("WEBULL_APP_KEY", "key")
    monkeypatch.setenv("WEBULL_APP_SECRET", "secret")
    monkeypatch.setattr(webull_io, "_HYDRATED_TOKEN", None)
    webull_io.reset_clients()
    yield tmp_path
    webull_io.reset_clients()
    FAKE_DB.store.clear()


def _sdk(monkeypatch, answer, stored_expiry):
    """The SDK doubled at its edges; `events` is the order Webull would see calls."""
    _install_fake_sdk(monkeypatch)
    events = []
    trade_client = sys.modules["webull.trade.trade_client"].TradeClient

    def recorded_trade_client(api):
        events.append("trade_client")
        return trade_client(api)

    class TokenOperation:
        def __init__(self, api):
            pass

        def check_token(self, token):
            events.append("check_token")
            return answer

    monkeypatch.setitem(sys.modules, "webull.trade.trade_client",
                        SimpleNamespace(TradeClient=recorded_trade_client))
    for name in ("webull.core.http", "webull.core.http.initializer",
                 "webull.core.http.initializer.token"):
        monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
    monkeypatch.setitem(sys.modules, "webull.core.http.initializer.token.token_operation",
                        SimpleNamespace(TokenOperation=TokenOperation))
    hydrate = webull_io.hydrate_token_from_secret
    monkeypatch.setattr(webull_io, "hydrate_token_from_secret",
                        lambda: hydrate(_secret(stored_expiry)))
    return events


# ------------------------------------------------------------------ the live answer
@pytest.mark.parametrize("field", ["expires", "expires_at"])
def test_live_status_reads_either_expiry_name_and_never_returns_the_token(field):
    answer = _answer(left=timedelta(days=9), field=field)
    live = webull_io.live_token_status(SimpleNamespace(check_token=lambda _: answer), TOKEN)
    assert live["status"] == "NORMAL" and live["token_matches"] is True
    assert timedelta(days=8.99) < live["expires_at"] - datetime.now(UTC) < timedelta(days=9)
    assert TOKEN not in json.dumps(live, default=str)


@pytest.mark.parametrize("answer", [
    SimpleNamespace(status_code=401, json=lambda: {"error_code": "UNAUTHORIZED"}),
    SimpleNamespace(status_code=200, json=lambda: [{"token": TOKEN}])])
def test_no_usable_answer_is_none(answer):
    assert webull_io.live_token_status(SimpleNamespace(check_token=lambda _: answer), TOKEN) is None


def test_an_answer_about_another_token_or_without_an_expiry_says_so():
    other = webull_io.live_token_status(
        SimpleNamespace(check_token=lambda _: _answer(token="f" * 32)), TOKEN)
    undated = webull_io.live_token_status(
        SimpleNamespace(check_token=lambda _: _answer(left=None)), TOKEN)
    assert other["token_matches"] is False and undated["expires_at"] is None


# ------------------------------------------------------------- build_clients on PROD
def test_a_passed_stored_expiry_still_reaches_check_token_and_webull_decides(prod, monkeypatch):
    events = _sdk(monkeypatch, _answer(left=timedelta(days=14)), _at(-timedelta(days=1)))
    webull_io.build_clients()
    assert events == ["check_token", "trade_client"]
    health = webull_io.token_health()
    assert health["ready"] and 13.9 < health["days_left"] <= 14
    assert webull_io.new_order_token_block(health, "PROD") is None


@pytest.mark.parametrize("answer", [
    _answer(status="INVALID"), _answer(status="EXPIRED"),
    _answer(left=-timedelta(minutes=1)), _answer(token="f" * 32), _answer(left=None),
    _answer(http=401)])
def test_a_token_webull_does_not_accept_stops_before_any_sdk_client(prod, monkeypatch, answer):
    events = _sdk(monkeypatch, answer, _at(-timedelta(days=1)))
    with tick_runtime.tick_scope("token-rejected"):
        with pytest.raises(webull_io.TokenUnavailableError):
            webull_io.build_clients()
    assert events == ["check_token"]                   # no create_token, so no 2FA from a scheduler
    assert auth_circuit.status(webull_io.auth_circuit_key())["active"]


def test_less_than_a_day_left_still_reads_but_opens_no_new_order(prod, monkeypatch):
    events = _sdk(monkeypatch, _answer(left=timedelta(hours=12)), _at(timedelta(days=10)))
    webull_io.build_clients()                          # reads and reconcile keep working
    assert events == ["check_token", "trade_client"]
    assert webull_io.new_order_token_block(webull_io.token_health(), "PROD")


def test_minutes_left_builds_no_client(prod, monkeypatch):
    events = _sdk(monkeypatch, _answer(left=timedelta(minutes=4)), _at(timedelta(days=10)))
    with pytest.raises(webull_io.TokenUnavailableError, match="within minutes"):
        webull_io.build_clients()
    assert events == ["check_token"]


# ------------------------------------------------------------------ hydration rules
def test_prod_hands_the_token_it_already_holds_to_check_token(prod):
    (prod / "token.txt").write_text(f"{TOKEN}\n{_at(timedelta(days=3))}\nNORMAL\n")
    webull_io.hydrate_token_from_secret(_secret(_at(-timedelta(days=1))))
    assert webull_io.read_local_token()["token"] == TOKEN


def test_prod_never_lets_a_passed_secret_replace_another_token(prod):
    before = f"{'f' * 32}\n{_at(timedelta(days=3))}\nNORMAL\n"
    (prod / "token.txt").write_text(before)
    with pytest.raises(webull_io.TokenUnavailableError):
        webull_io.hydrate_token_from_secret(_secret(_at(-timedelta(days=1))))
    assert (prod / "token.txt").read_text() == before


def test_uat_still_refuses_a_secret_whose_stored_expiry_passed(prod, monkeypatch):
    monkeypatch.setenv("WEBULL_ENV", "UAT")
    with pytest.raises(webull_io.TokenUnavailableError):
        webull_io.hydrate_token_from_secret(_secret(_at(-timedelta(days=1))))
    assert not (prod / "token.txt").exists()


# ------------------------------------------------------------------ ops token-status
@pytest.fixture
def token_status(monkeypatch, capsys):
    """Run `ops.py token-status` against a doubled Check Token; returns (exit, report, raw)."""
    monkeypatch.setenv("WEBULL_ENV", "PROD")
    monkeypatch.setenv("WEBULL_APP_KEY", "key")
    monkeypatch.setenv("WEBULL_APP_SECRET", "secret")
    sdk_logger = logging.getLogger("webull")           # the command silences it; undo after
    monkeypatch.setattr(sdk_logger, "handlers", list(sdk_logger.handlers))
    monkeypatch.setattr(sdk_logger, "propagate", sdk_logger.propagate)
    seen = []

    def run(stdin, answer=None, raises=None):
        class ApiClient:
            def __init__(self, key, secret, region):
                seen.append(("client", region))

            def add_endpoint(self, region, endpoint):
                seen.append(("endpoint", endpoint))

            def set_token(self, _token):
                pass

        class TokenOperation:
            def __init__(self, api):
                pass

            def check_token(self, _token):
                seen.append(("check_token",))
                if raises is not None:
                    raise raises
                return answer

        for name in ("webull", "webull.core", "webull.core.http", "webull.core.http.initializer",
                     "webull.core.http.initializer.token"):
            monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
        monkeypatch.setitem(sys.modules, "webull.core.client", SimpleNamespace(ApiClient=ApiClient))
        monkeypatch.setitem(sys.modules, "webull.core.http.initializer.token.token_operation",
                            SimpleNamespace(TokenOperation=TokenOperation))
        monkeypatch.setattr(sys, "stdin", io.StringIO(stdin))
        monkeypatch.setattr(sys, "argv", ["ops.py", "token-status"])
        code = ops.main_cli()
        raw = capsys.readouterr().out
        assert TOKEN not in raw                        # never printed, whatever the answer
        return code, json.loads(raw)

    run.seen = seen
    return run


STORED = f"{TOKEN}\n1893456000000\nNORMAL\n"


@pytest.mark.parametrize("field", ["expires", "expires_at"])
def test_token_status_fresh_is_exit_0(token_status, field):
    code, report = token_status(STORED, _answer(left=timedelta(days=10), field=field))
    assert code == 0 and report["fresh"] is True and report["status"] == "NORMAL"
    assert report["environment"] == webull_io.PROD and 9.9 < report["days_left"] <= 10
    assert ("endpoint", webull_io.PROD_ENDPOINT) in token_status.seen


@pytest.mark.parametrize("answer", [
    _answer(left=timedelta(days=2)),                   # inside the refresh margin
    _answer(status="INVALID"), _answer(token="f" * 32), _answer(left=None),
    _answer(status=TOKEN)])                            # an unexpected status is never echoed
def test_token_status_not_usable_is_exit_1(token_status, answer):
    code, report = token_status(STORED, answer)
    assert code == 1 and report["fresh"] is False
    assert report["status"] in {"NORMAL", "INVALID", "UNKNOWN"}


def test_token_status_without_a_well_formed_token_never_asks_webull(token_status):
    code, report = token_status("only-one-line\n", _answer())
    assert code == 1 and "stdin" in report["error"] and token_status.seen == []


@pytest.mark.parametrize("answer, raises", [
    (_answer(http=401), None), (None, ConnectionError("api.webull.co.th unreachable"))])
def test_token_status_that_cannot_ask_webull_is_exit_2(token_status, answer, raises):
    code, report = token_status(STORED, answer, raises)
    assert code == 2 and report["fresh"] is False
    assert "unreachable" not in report["error"]        # only the exception type leaves


def test_token_status_without_app_credentials_is_exit_2(token_status, monkeypatch):
    monkeypatch.delenv("WEBULL_APP_SECRET")
    code, report = token_status(STORED, _answer())
    assert code == 2 and report["error"] == "Check Token unavailable: KeyError"
