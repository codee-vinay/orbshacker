"""Tests for net.py – HTTP helper behaviour with no real network access."""

from types import SimpleNamespace

import pytest
import requests

from orbshacker import config
from orbshacker.errors import NetworkError
from orbshacker.net import fetch_json


URL = "https://example.test/api"


class FakeResponse:
    """Minimal stand-in for requests.Response."""

    def __init__(self, payload=None, *, json_error=None, status_error=None):
        self._payload = payload
        self._json_error = json_error
        self._status_error = status_error

    def raise_for_status(self):
        if self._status_error is not None:
            raise self._status_error

    def json(self):
        if self._json_error is not None:
            raise self._json_error
        return self._payload


def _patch_get(monkeypatch, response):
    """Replace orbshacker.net.requests with a stub that returns *response*."""
    calls = []

    def fake_get(url, **kwargs):
        calls.append({"url": url, "kwargs": kwargs})
        return response

    monkeypatch.setattr(
        "orbshacker.net.requests",
        SimpleNamespace(get=fake_get, RequestException=requests.RequestException),
    )
    return calls


class TestFetchJsonSuccess:
    def test_returns_parsed_payload(self, monkeypatch):
        payload = [{"id": "1", "name": "Minecraft"}]
        calls = _patch_get(monkeypatch, FakeResponse(payload))

        result = fetch_json(URL)

        assert result is payload
        assert calls[0]["url"] == URL

    def test_forwards_headers_params_and_default_timeout(self, monkeypatch):
        calls = _patch_get(monkeypatch, FakeResponse([]))

        fetch_json(URL, headers={"X-Test": "1"}, params={"term": "game"})

        kwargs = calls[0]["kwargs"]
        assert kwargs["headers"] == {"X-Test": "1"}
        assert kwargs["params"] == {"term": "game"}
        assert kwargs["timeout"] == config.REQUEST_TIMEOUT

    def test_explicit_timeout_is_used(self, monkeypatch):
        calls = _patch_get(monkeypatch, FakeResponse({}))

        fetch_json(URL, timeout=config.REQUEST_TIMEOUT_LONG)

        assert calls[0]["kwargs"]["timeout"] == config.REQUEST_TIMEOUT_LONG


class TestFetchJsonErrors:
    def test_http_error_becomes_network_error(self, monkeypatch):
        error = requests.exceptions.HTTPError("404 Client Error")
        _patch_get(monkeypatch, FakeResponse(status_error=error))

        with pytest.raises(NetworkError) as excinfo:
            fetch_json(URL)

        assert str(excinfo.value).startswith(f"Request to {URL} failed:")
        assert excinfo.value.__cause__ is error

    def test_connection_error_becomes_network_error(self, monkeypatch):
        error = requests.exceptions.ConnectionError("connection refused")

        def fake_get(url, **kwargs):
            raise error

        monkeypatch.setattr(
            "orbshacker.net.requests",
            SimpleNamespace(get=fake_get, RequestException=requests.RequestException),
        )

        with pytest.raises(NetworkError) as excinfo:
            fetch_json(URL)

        assert str(excinfo.value).startswith(f"Request to {URL} failed:")
        assert excinfo.value.__cause__ is error

    def test_invalid_json_becomes_network_error(self, monkeypatch):
        error = requests.exceptions.JSONDecodeError("Expecting value", "<html>", 0)
        _patch_get(monkeypatch, FakeResponse(json_error=error))

        with pytest.raises(NetworkError) as excinfo:
            fetch_json(URL)

        assert "not valid JSON" in str(excinfo.value)
        assert excinfo.value.__cause__ is error

    def test_plain_value_error_becomes_network_error(self, monkeypatch):
        error = ValueError("bad json")
        _patch_get(monkeypatch, FakeResponse(json_error=error))

        with pytest.raises(NetworkError) as excinfo:
            fetch_json(URL)

        assert excinfo.value.__cause__ is error
