"""The parcel contract's checks, run over every normalised sample."""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from custom_components.spee_dee_delivery import parcels, sensor
from custom_components.spee_dee_delivery.const import ParcelStatus
from custom_components.spee_dee_delivery.parcels import (
    is_pickup,
    normalize_parcel,
    parse_iso,
    planned_for_status,
    planned_window,
)

from .payloads import active_sample, delivered_sample

CHICAGO = ZoneInfo("America/Chicago")

CANONICAL_KEYS = [
    "carrier",
    "barcode",
    "sender",
    "receiver",
    "contents",
    "status",
    "raw_status",
    "delivered",
    "delivered_at",
    "planned_from",
    "planned_to",
    "pickup",
    "pickup_point",
    "url",
    "weight",
    "dimensions",
    "history",
    "raw",
]


def _with_summary(text: str) -> dict:
    sample = active_sample()
    sample["summaryStatus"] = text
    return sample


def _stale_delivery_time() -> dict:
    sample = active_sample()
    sample["deliveredOn"] = "2026-04-29T13:12:00-05:00"
    return sample


SAMPLES = {
    "delivered": delivered_sample,
    "active": active_sample,
    "in_transit": lambda: _with_summary("ARRIVAL SCAN"),
    "problem": lambda: _with_summary("** MISSORT SCANS **"),
    "stale_delivery_time": _stale_delivery_time,
    "unknown": lambda: _with_summary("SOMETHING NEW"),
    "bare": lambda: {"trackingNumber": "EXAMPLE000001"},
}


@pytest.fixture(params=SAMPLES)
def parcel(request) -> dict:
    return normalize_parcel(SAMPLES[request.param]())


def test_keys_and_order(parcel):
    assert list(parcel) == CANONICAL_KEYS


def test_delivered_follows_status(parcel):
    assert parcel["delivered"] is (parcel["status"] is ParcelStatus.DELIVERED)


def test_delivered_at_only_when_delivered(parcel):
    if not parcel["delivered"]:
        assert parcel["delivered_at"] is None


def test_delivered_at_keeps_the_carrier_zone():
    parcel = normalize_parcel(delivered_sample())
    stamp = parse_iso(parcel["delivered_at"])
    assert stamp == datetime(2026, 4, 29, 13, 12, tzinfo=CHICAGO)
    assert stamp.utcoffset().total_seconds() == -5 * 3600


def test_planned_window_is_ordered_and_anchored(parcel):
    assert parcel["planned_from"] is None
    assert parcel["planned_to"] is None


def test_pickup_is_false_without_a_pickup_status_and_has_no_point(parcel):
    assert parcel["pickup"] is False
    assert parcel["pickup_point"] is None


def test_a_parcel_that_reached_a_pickup_point_is_a_pickup_parcel(monkeypatch):
    monkeypatch.setattr(
        parcels, "map_event_status", lambda _text: ParcelStatus.AT_PICKUP_POINT
    )
    parcel = normalize_parcel(active_sample())
    assert parcel["pickup"] is True
    assert parcel["pickup_point"] is None


def test_the_pickup_sensors_are_exposed():
    assert sensor.SpeeDeeDeliveryAwaitingPickupSensor
    assert sensor.SpeeDeeDeliveryEnRouteToPickupPointSensor


def test_a_moment_ends_at_the_end_of_that_day():
    start, end = planned_window("2099-04-29T13:00:00Z", None, zone=CHICAGO)
    assert start == "2099-04-29T13:00:00Z"
    stop = datetime.fromisoformat(end)
    assert (stop.hour, stop.minute, stop.second, stop.microsecond) == (23, 59, 59, 0)
    assert stop.date() == parse_iso(start).astimezone(CHICAGO).date()


def test_a_date_covers_the_whole_day():
    start, end = planned_window("2099-04-29", None, zone=CHICAGO)
    first = datetime.fromisoformat(start)
    last = datetime.fromisoformat(end)
    assert (first.hour, first.minute, first.second) == (0, 0, 0)
    assert (last.hour, last.minute, last.second, last.microsecond) == (23, 59, 59, 0)
    assert first.date() == last.date()
    assert first.utcoffset() == CHICAGO.utcoffset(datetime(2099, 4, 29))


@pytest.mark.parametrize(
    "status",
    [ParcelStatus.DELIVERED, ParcelStatus.AT_PICKUP_POINT, ParcelStatus.RETURNING],
)
def test_the_window_is_emptied_when_done_or_waiting(status):
    assert planned_for_status(status, "2099-01-01", "2099-01-02") == (None, None)
    assert planned_for_status(ParcelStatus.IN_TRANSIT, "a", "b") == ("a", "b")


def test_pickup_stays_true_after_collection():
    assert is_pickup(None, ParcelStatus.DELIVERED, [ParcelStatus.AT_PICKUP_POINT])
    assert not is_pickup(None, ParcelStatus.DELIVERED, [ParcelStatus.IN_TRANSIT])
    assert planned_window(None, None, zone=UTC) == (None, None)
