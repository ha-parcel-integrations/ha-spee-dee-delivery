"""Spee-Dee Delivery public tracking client.

The contract the coordinator relies on:

* ``async_get_parcel`` returns the raw per-parcel dict on success,
* returns ``None`` when the carrier says the tracking code is unknown (a
  normal, expected state — never an error),
* raises :class:`SpeeDeeDeliveryApiError` for anything else, with
  ``status_code`` set on a non-2xx response and ``retry_after`` set when the
  carrier's own ``Retry-After`` header on a 429 could be parsed as seconds,
* lets ``aiohttp.ClientError`` propagate untouched — ``DataUpdateCoordinator``
  already wraps those into ``UpdateFailed``.
"""
from __future__ import annotations

import asyncio
from typing import Any
from urllib.parse import quote

import aiohttp

from .const import TRACKING_API_URL
from .parser import SpeeDeeDeliveryParseError, parse_page, warn_once

USER_AGENT = "ha-spee-dee-delivery (Home Assistant custom integration)"
MAX_BODY_BYTES = 2_000_000


class SpeeDeeDeliveryApiError(Exception):
    """Raised when a Spee-Dee Delivery call returns an unexpected response."""

    def __init__(
        self,
        detail: str,
        *,
        status_code: int | None = None,
        retry_after: float | None = None,
    ) -> None:
        """Store the status code and the ``Retry-After`` header, if any."""
        super().__init__(f"Spee-Dee Delivery request failed: {detail}")
        self.detail = detail
        self.status_code = status_code
        self.retry_after = retry_after


class SpeeDeeDeliveryApiClient:
    """Client for the public Spee-Dee Delivery package page.

    No authentication: the page is keyed on the tracking code alone. Requests
    are sent one at a time, once per registered code, and never retried.
    """

    def __init__(self, session: aiohttp.ClientSession) -> None:
        """Initialise the client with an aiohttp session."""
        self._session = session
        self._lock = asyncio.Lock()

    async def async_get_parcel(self, tracking_code: str) -> dict[str, Any] | None:
        """Fetch one parcel's package page.

        Returns the raw parcel dict for a known parcel, or ``None`` on a 404.
        A changed page, a redirect or any other failure raises
        :class:`SpeeDeeDeliveryApiError`; network errors propagate as
        ``aiohttp.ClientError``.
        """
        code = tracking_code.strip()
        url = TRACKING_API_URL.format(tracking_code=quote(code, safe=""))
        async with self._lock, self._session.get(
            url, headers={"User-Agent": USER_AGENT}, allow_redirects=False
        ) as response:
            if response.status == 404:
                return None
            if response.status == 429:
                retry_after_header = response.headers.get("Retry-After")
                try:
                    retry_after = (
                        float(retry_after_header) if retry_after_header else None
                    )
                except ValueError:
                    retry_after = None  # an HTTP-date, not seconds
                raise SpeeDeeDeliveryApiError(
                    "HTTP 429", status_code=429, retry_after=retry_after
                )
            if response.status != 200:
                raise SpeeDeeDeliveryApiError(
                    f"HTTP {response.status}", status_code=response.status
                )
            body = await response.read()

        if len(body) > MAX_BODY_BYTES:
            raise SpeeDeeDeliveryApiError("response too large")
        html = body.decode("utf-8", errors="replace")
        try:
            # Off the event loop: parsing is synchronous work.
            return await asyncio.to_thread(parse_page, html, code)
        except SpeeDeeDeliveryParseError as err:
            _warn_template(str(err))
            raise SpeeDeeDeliveryApiError(f"unexpected page ({err})") from err


def _warn_template(reason: str) -> None:
    """Log one value-free warning per kind of changed template."""
    warn_once(
        f"template:{reason}",
        "Spee-Dee Delivery page did not have the expected shape (%s)",
        reason,
    )
