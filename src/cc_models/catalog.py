"""Catalog fetching from the CPA upstream.

Only ever performs a single GET of ``{endpoint}/models``. Redirects are
rejected (the redirect destination is never contacted), the HTTP timeout is
bounded, and every ``data[].id`` is validated as a nonempty string before the
catalog is built. Raw IDs are never rewritten; duplicates are deduped with
their extra occurrence counts recorded.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from datetime import UTC, datetime

from .domain import (
    Catalog,
    CatalogError,
    ConfigError,
    NetworkError,
    normalize_endpoint,
)

DEFAULT_TIMEOUT_SECONDS = 10.0


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Redirects must fail: never follow, never contact the destination."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def fetch_catalog(
    endpoint: str,
    api_key: str | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    allow_insecure_http: bool = False,
) -> Catalog:
    """Fetch and validate the model catalog for a normalized endpoint.

    ``api_key`` is only ever held in memory and sent as a Bearer header; it is
    never stored in local state and never appears in error messages.
    Remote plain-HTTP endpoints require the explicit ``allow_insecure_http``
    opt-in (persisted in local state); redirects are always refused.
    """
    normalized = normalize_endpoint(endpoint, allow_insecure_http=allow_insecure_http)
    url = f"{normalized}/models"
    headers = {"Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with _OPENER.open(request, timeout=timeout) as response:
            body = response.read()
    except urllib.error.HTTPError as exc:
        if 300 <= exc.code < 400:
            raise NetworkError(f"catalog request redirected (refused): HTTP {exc.code}") from exc
        raise NetworkError(f"catalog request failed: HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
        raise NetworkError(f"catalog request failed: {type(exc).__name__}") from exc
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CatalogError(f"catalog response is not valid JSON: {exc}") from exc
    fetched_at = datetime.now(UTC).isoformat(timespec="seconds")
    return Catalog.from_response(normalized, fetched_at, payload)


def strict_json_loads(text: str) -> object:
    """Strict JSON parse used for trusted local files (kept here for reuse)."""
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"malformed JSON: {exc}") from exc
