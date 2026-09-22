from __future__ import annotations

import copy
import os
import sys
import types
import uuid

import pytest


class FakeReference:
    def __init__(self, root: dict, path: str, query=None):
        self.root = root
        self.parts = [p for p in path.split('/') if p]
        self.query = dict(query or {})

    def _parent(self, create=True):
        node = self.root
        for part in self.parts[:-1]:
            if create:
                node = node.setdefault(part, {})
            else:
                node = node.get(part)
                if node is None:
                    return None, None
        return node, self.parts[-1] if self.parts else None

    def get(self):
        node = self.root
        for part in self.parts:
            if not isinstance(node, dict) or part not in node:
                return None
            node = node[part]
        node = copy.deepcopy(node)
        if not self.query or not isinstance(node, dict):
            return node
        child = self.query.get("order_by_child")
        items = list(node.items())
        if child:
            def value(item):
                payload = item[1]
                return payload.get(child) if isinstance(payload, dict) else None

            if "equal_to" in self.query:
                items = [item for item in items
                         if value(item) == self.query["equal_to"]]
            if "start_at" in self.query:
                items = [item for item in items
                         if value(item) is not None
                         and value(item) >= self.query["start_at"]]
            if "end_at" in self.query:
                items = [item for item in items
                         if value(item) is not None
                         and value(item) <= self.query["end_at"]]
            items.sort(key=lambda item: (
                value(item) is not None, str(value(item) or ""), str(item[0])))
        if "limit_to_first" in self.query:
            items = items[:int(self.query["limit_to_first"])]
        return dict(items)

    def _with_query(self, **fields):
        return FakeReference(self.root, "/".join(self.parts),
                             {**self.query, **fields})

    def order_by_child(self, child):
        return self._with_query(order_by_child=child)

    def equal_to(self, value):
        return self._with_query(equal_to=value)

    def start_at(self, value):
        return self._with_query(start_at=value)

    def end_at(self, value):
        return self._with_query(end_at=value)

    def limit_to_first(self, value):
        return self._with_query(limit_to_first=value)

    def set(self, value):
        if not self.parts:
            self.root.clear()
            self.root.update(copy.deepcopy(value or {}))
            return
        parent, key = self._parent(True)
        parent[key] = copy.deepcopy(value)

    def update(self, fields):
        current = self.get() or {}
        current.update(copy.deepcopy(fields))
        self.set(current)

    def delete(self):
        if not self.parts:
            self.root.clear()
            return
        parent, key = self._parent(False)
        if parent is not None:
            parent.pop(key, None)

    def transaction(self, fn):
        current = self.get()
        result = fn(copy.deepcopy(current))
        # Firebase RTDB treats a transaction result of None as a deletion.
        if result is None:
            self.delete()
        else:
            self.set(result)
        return copy.deepcopy(result)

    def push(self, value):
        current = self.get() or {}
        key = uuid.uuid4().hex
        current[key] = copy.deepcopy(value)
        self.set(current)
        return types.SimpleNamespace(key=key)


class FakeDB:
    def __init__(self):
        self.store = {}

    def reference(self, path=''):
        return FakeReference(self.store, path)


FAKE_DB = FakeDB()


@pytest.fixture(autouse=True)
def _runtime_identity_test_default(monkeypatch):
    """HTTP pipeline tests use an opaque, non-production account identity."""
    # A developer's configured receiver must never receive test notifications.
    # Alert tests explicitly install a fake HTTPS URL and mock the transport.
    monkeypatch.delenv("ALERT_WEBHOOK_URL", raising=False)
    if not os.environ.get("WEBULL_ACCOUNT_ID"):
        monkeypatch.setenv("WEBULL_ACCOUNT_ID", "codex-test-account")

firebase_admin = types.ModuleType('firebase_admin')
firebase_admin._apps = []
firebase_admin.initialize_app = lambda *args, **kwargs: firebase_admin._apps.append(object())
firebase_admin.credentials = types.SimpleNamespace(ApplicationDefault=lambda: object())
firebase_admin.db = FAKE_DB
sys.modules.setdefault('firebase_admin', firebase_admin)
sys.modules.setdefault('firebase_admin.db', FAKE_DB)

ff = types.ModuleType('functions_framework')
ff.http = lambda fn: fn
sys.modules.setdefault('functions_framework', ff)


# --- Webull SDK doubles ------------------------------------------------------
# webull_io is the only boundary that touches real money, and it was the least
# covered file in the repo for one reason: every call goes through a TradeClient
# or DataClient the tests had no way to build. These stand in for exactly the
# surface webull_io uses, so its shape-parsing and fail-closed branches can be
# exercised the same way firebase is.

class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return copy.deepcopy(self._payload)


class FakeCall:
    """One SDK method: records its calls, returns a payload or raises."""

    def __init__(self, payload=None):
        self.payload = payload
        self.calls = []

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if isinstance(self.payload, Exception):
            raise self.payload
        if callable(self.payload):
            return FakeResponse(self.payload(*args, **kwargs))
        return FakeResponse(self.payload)


class FakeNamespace:
    def __init__(self, **members):
        self.__dict__.update(members)


def fake_trade_client(*, positions=None, balance=None, open_orders=None, order_detail=None,
                      preview=None, place=None, instrument=None):
    return FakeNamespace(
        account_v2=FakeNamespace(
            get_account_position=FakeCall(positions),
            get_account_balance=FakeCall(balance),
        ),
        order_v3=FakeNamespace(
            get_order_open=FakeCall(open_orders),
            get_order_detail=FakeCall(order_detail),
            preview_order=FakeCall(preview),
            place_order=FakeCall(place),
        ),
        trade_instrument=FakeNamespace(
            get_instrument_stock_detail=FakeCall(instrument),
        ),
    )


def fake_data_client(*, snapshot=None):
    return FakeNamespace(market_data=FakeNamespace(get_snapshot=FakeCall(snapshot)))
