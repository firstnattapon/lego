"""Regression coverage for Webull UAT HTTP 417 system failures."""

import webull_io


class _SystemError(Exception):
    http_status = 417
    error_code = "OPENAPI_SYSTEM_ERROR"


class _ParameterError(Exception):
    http_status = 417
    error_code = "OPENAPI_PARAM_ERR"


def test_openapi_system_error_is_transient_for_safe_read_retry():
    assert webull_io.is_transient_exception(_SystemError("System error")) is True


def test_generic_417_business_error_is_not_retried():
    assert webull_io.is_transient_exception(_ParameterError("bad parameter")) is False
