"""Tests for the Spee-Dee Delivery API client."""
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest

from custom_components.spee_dee_delivery import api
from custom_components.spee_dee_delivery.api import (
    SpeeDeeDeliveryApiClient,
    SpeeDeeDeliveryApiError,
)

from .payloads import (
    ACTIVE_CODE,
    DELIVERED_CODE,
    PII,
    delivered_html,
    in_transit_html,
)


def _session_returning(
    status: int, body: str | bytes = "", headers: dict | None = None
) -> MagicMock:
    response = AsyncMock()
    response.status = status
    response.headers = headers or {}
    response.read = AsyncMock(
        return_value=body.encode() if isinstance(body, str) else body
    )
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=response)
    ctx.__aexit__ = AsyncMock(return_value=False)
    session = MagicMock()
    session.get = MagicMock(return_value=ctx)
    return session


async def test_get_parcel_returns_parsed_payload():
    session = _session_returning(200, delivered_html())
    client = SpeeDeeDeliveryApiClient(session)

    parcel = await client.async_get_parcel(DELIVERED_CODE)

    assert parcel["trackingNumber"] == DELIVERED_CODE
    assert parcel["summaryStatus"] == "Delivered"
    # the client returns the untrimmed record; redaction is diagnostics' job
    assert all(value in repr(parcel) for value in PII)


async def test_request_is_one_direct_get_without_redirects():
    session = _session_returning(200, in_transit_html())
    await SpeeDeeDeliveryApiClient(session).async_get_parcel(f"  {ACTIVE_CODE} ")

    session.get.assert_called_once()
    url = session.get.call_args[0][0]
    kwargs = session.get.call_args[1]
    assert url == (
        "https://packages.speedeedelivery.com/package_progress.php"
        f"?barcode={ACTIVE_CODE}"
    )
    assert kwargs["allow_redirects"] is False
    assert kwargs["headers"]["User-Agent"] == api.USER_AGENT
    assert "Home Assistant" in api.USER_AGENT


async def test_code_is_url_encoded_exactly_once_and_case_is_kept():
    session = _session_returning(200, in_transit_html("aB c&d=1"))
    await SpeeDeeDeliveryApiClient(session).async_get_parcel("aB c&d=1")
    assert session.get.call_args[0][0].endswith("?barcode=aB%20c%26d%3D1")


async def test_get_parcel_returns_none_on_404():
    """404 leaves the manually registered code pending, never an error."""
    client = SpeeDeeDeliveryApiClient(_session_returning(404))
    assert await client.async_get_parcel(ACTIVE_CODE) is None


@pytest.mark.parametrize("status", [301, 302, 400, 403, 410, 500, 503])
async def test_get_parcel_raises_on_other_statuses(status):
    client = SpeeDeeDeliveryApiClient(_session_returning(status))
    with pytest.raises(SpeeDeeDeliveryApiError) as err:
        await client.async_get_parcel(ACTIVE_CODE)
    assert err.value.status_code == status
    assert err.value.retry_after is None


async def test_429_carries_retry_after_seconds():
    client = SpeeDeeDeliveryApiClient(
        _session_returning(429, headers={"Retry-After": "120"})
    )
    with pytest.raises(SpeeDeeDeliveryApiError) as err:
        await client.async_get_parcel(ACTIVE_CODE)
    assert err.value.status_code == 429
    assert err.value.retry_after == 120.0


@pytest.mark.parametrize("headers", [{}, {"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}])
async def test_429_without_usable_retry_after(headers):
    client = SpeeDeeDeliveryApiClient(_session_returning(429, headers=headers))
    with pytest.raises(SpeeDeeDeliveryApiError) as err:
        await client.async_get_parcel(ACTIVE_CODE)
    assert err.value.retry_after is None


@pytest.mark.parametrize(
    "body",
    [
        "<html><body>Please verify you are human</body></html>",
        "",
        "<html><body>No package found</body></html>",
    ],
)
async def test_changed_template_raises_and_warns_once_without_values(body, caplog):
    client = SpeeDeeDeliveryApiClient(_session_returning(200, body))
    for _ in range(2):
        with pytest.raises(SpeeDeeDeliveryApiError) as err:
            await client.async_get_parcel(ACTIVE_CODE)
    assert err.value.status_code is None
    assert caplog.text.count("did not have the expected shape") == 1
    assert ACTIVE_CODE not in caplog.text


async def test_oversized_body_is_refused():
    client = SpeeDeeDeliveryApiClient(
        _session_returning(200, b"x" * (api.MAX_BODY_BYTES + 1))
    )
    with pytest.raises(SpeeDeeDeliveryApiError):
        await client.async_get_parcel(ACTIVE_CODE)


async def test_undecodable_bytes_do_not_crash_the_decode():
    html = delivered_html().encode() + b"\xff\xfe"
    parcel = await SpeeDeeDeliveryApiClient(
        _session_returning(200, html)
    ).async_get_parcel(DELIVERED_CODE)
    assert parcel["summaryStatus"] == "Delivered"


async def test_get_parcel_propagates_network_error():
    """ClientError is left alone — DataUpdateCoordinator already wraps it."""
    session = MagicMock()
    session.get = MagicMock(side_effect=aiohttp.ClientError("boom"))
    client = SpeeDeeDeliveryApiClient(session)
    with pytest.raises(aiohttp.ClientError):
        await client.async_get_parcel(ACTIVE_CODE)
