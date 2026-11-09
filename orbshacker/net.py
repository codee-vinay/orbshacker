"""
net.py – Shared HTTP helpers.
"""

import requests
from typing import Any, Mapping

from . import config
from .errors import NetworkError


def fetch_json(
    url: str,
    headers: Mapping[str, str] | None = None,
    params: Mapping[str, str] | None = None,
    timeout: int | None = None,
) -> Any:
    """GET *url* and return the parsed JSON body of any shape.

    Callers must validate the shape: the Discord endpoint returns an array,
    the Steam endpoints return objects.

    Raises NetworkError when the request fails, the status is an error, or
    the body is not valid JSON.
    """
    effective_timeout = config.REQUEST_TIMEOUT if timeout is None else timeout

    try:
        resp = requests.get(
            url,
            headers=dict(headers or {}),
            params=dict(params or {}),
            timeout=effective_timeout,
        )
        resp.raise_for_status()
    except requests.RequestException as exc:
        raise NetworkError(f"Request to {url} failed: {exc}") from exc

    try:
        return resp.json()
    except ValueError as exc:
        raise NetworkError(f"Response from {url} was not valid JSON: {exc}") from exc
