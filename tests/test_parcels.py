"""Tests for the pure parcel-mapping helpers.

These need no Home Assistant instance — the whole point of keeping
``parcels.py`` free of I/O is that the carrier-specific mapping can be tested
as plain functions.
"""

from datetime import UTC, datetime, timedelta

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.spee_dee_delivery.const import (
    CAPABILITIES,
    CONF_DELIVERED_FILTER_AMOUNT,
    CONF_DELIVERED_FILTER_TYPE,
    DOMAIN,
    KNOWN_CAPABILITIES,
    PENDING_CAPABILITIES,
    ParcelStatus,
)
from custom_components.spee_dee_delivery.parcels import (
    apply_delivered_filter,
    build_history,
    map_event_status,
    map_parcel_status,
    normalize_parcel,
    parse_iso,
    sort_parcels_by_ts,
    tracking_url,
)

from .payloads import PII, active_sample, delivered_sample, event

# ---------------------------------------------------------------------------
# map_parcel_status / map_event_status
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Delivered", ParcelStatus.DELIVERED),
        ("  DELIVERED ", ParcelStatus.DELIVERED),
        ("OUT FOR DELIVERY (Route 12)", ParcelStatus.OUT_FOR_DELIVERY),
        ("out  for\ndelivery (Route 3)", ParcelStatus.OUT_FOR_DELIVERY),
        ("ARRIVAL SCAN", ParcelStatus.IN_TRANSIT),
        ("arrival scan", ParcelStatus.IN_TRANSIT),
        ("UNLOAD SCAN: Dock 4", ParcelStatus.IN_TRANSIT),
        # a bare scan word, seen on a real parcel between two depot arrivals
        ("IN TRANSIT", ParcelStatus.IN_TRANSIT),
        ("** MISSORT SCANS **", ParcelStatus.PROBLEM),
        ("**  missort scans **", ParcelStatus.PROBLEM),
    ],
)
def test_map_parcel_status_known(text, expected):
    assert map_parcel_status(text) == expected
    assert map_event_status(text) == expected


def test_map_parcel_status_missing_is_unknown():
    assert map_parcel_status(None) == ParcelStatus.UNKNOWN
    assert map_parcel_status("  ") == ParcelStatus.UNKNOWN


def test_map_parcel_status_unmapped_is_unknown():
    assert map_parcel_status("TELEPORTED") == ParcelStatus.UNKNOWN


def test_an_unmapped_summary_falls_back_to_the_newest_mapped_scan(caplog):
    raw = delivered_sample()
    raw["summaryStatus"] = "TELEPORTED"
    raw["events"] = raw["events"][:-1]  # no delivered scan left
    with caplog.at_level("WARNING"):
        parcel = normalize_parcel(raw)
    assert parcel["status"] == ParcelStatus.OUT_FOR_DELIVERY
    assert parcel["raw_status"] == "TELEPORTED"
    assert parcel["delivered"] is False
    assert "Unrecognised" in caplog.text


def test_an_unmapped_summary_without_a_mappable_scan_stays_unknown():
    raw = delivered_sample()
    raw["summaryStatus"] = "TELEPORTED"
    raw["events"] = [event("WARPED", "2026-04-28T06:01:00-05:00", "Fakeville Depot")]
    assert normalize_parcel(raw)["status"] == ParcelStatus.UNKNOWN


def test_delivered_is_an_exact_match():
    """Only the bare word is terminal; a longer sentence must not count."""
    assert map_parcel_status("Delivered to neighbour") == ParcelStatus.UNKNOWN


def test_map_event_status_missing_and_unmapped_are_none():
    """History keeps ``null`` rather than ``unknown`` so consumers can tell
    "no mapping" from "mapped to unknown"."""
    assert map_event_status(None) is None
    assert map_event_status("SOMETHING NEW") is None


def test_unmapped_status_warns_only_once_and_without_the_detail(caplog):
    assert map_parcel_status("ABDUCTED (Fakeville Depot)") == ParcelStatus.UNKNOWN
    assert map_event_status("ABDUCTED: Zed Sample") is None
    assert caplog.text.count("status=abducted") == 1
    assert "Fakeville" not in caplog.text
    assert "Zed Sample" not in caplog.text
    assert "unrecognised_status.yml" in caplog.text


# ---------------------------------------------------------------------------
# timestamp helpers
# ---------------------------------------------------------------------------


def test_parse_iso_handles_z_naive_and_garbage():
    assert parse_iso(None) is None
    assert parse_iso("nonsense") is None
    assert parse_iso("2026-01-01T10:00:00Z").utcoffset() == timedelta(0)
    assert parse_iso("2026-01-01T10:00:00").tzinfo is UTC


# ---------------------------------------------------------------------------
# build_history
# ---------------------------------------------------------------------------


def test_build_history_keeps_oldest_to_newest_and_maps_status():
    history = build_history(delivered_sample()["events"])
    # the per-scan suffix is cut here, never in ``raw``: history survives the
    # aggregator's strip_raw() and reaches entity attributes
    assert [h["raw_status"] for h in history] == [
        "ARRIVAL SCAN",
        "UNLOAD SCAN",
        "OUT FOR DELIVERY",
        "Delivered",
    ]
    assert history[-1]["status"] == ParcelStatus.DELIVERED
    assert set(history[0]) == {"timestamp", "status", "raw_status"}


def test_build_history_caps_to_most_recent_20():
    events = [
        event("ARRIVAL SCAN", f"2026-04-{day:02d}T08:00:00-05:00")
        for day in range(1, 26)
    ]
    history = build_history(events)
    assert len(history) == 20
    assert history[0]["timestamp"].startswith("2026-04-06")
    assert history[-1]["timestamp"].startswith("2026-04-25")


def test_build_history_missort_stays_a_problem_even_after_movement():
    events = [
        event("ARRIVAL SCAN", "2026-04-28T06:00:00-05:00"),
        event("** MISSORT SCANS **", "2026-04-28T07:00:00-05:00"),
        event("ARRIVAL SCAN", "2026-04-28T09:00:00-05:00"),
    ]
    statuses = [h["status"] for h in build_history(events)]
    assert statuses == [
        ParcelStatus.IN_TRANSIT,
        ParcelStatus.PROBLEM,
        ParcelStatus.IN_TRANSIT,
    ]


def test_build_history_handles_missing_and_malformed():
    events = [
        "junk",
        event("ARRIVAL SCAN", None),
        event("ARRIVAL SCAN", "2026-04-28T06:00:00-05:00"),
    ]
    assert len(build_history(events)) == 1
    assert build_history(None) == []


def test_build_history_keeps_unparseable_timestamp_last():
    history = build_history(
        [
            event("ARRIVAL SCAN", "nonsense"),
            event("Delivered", "2026-04-28T06:00:00-05:00"),
        ]
    )
    assert [h["raw_status"] for h in history] == ["Delivered", "ARRIVAL SCAN"]


def test_build_history_collapses_whitespace_and_nulls_blank_text():
    history = build_history(
        [
            event("ARRIVAL   SCAN\n", "2026-04-28T06:00:00-05:00"),
            event("  ", "2026-04-28T07:00:00-05:00"),
        ]
    )
    assert history[0]["raw_status"] == "ARRIVAL SCAN"
    assert history[1] == {
        "timestamp": "2026-04-28T07:00:00-05:00",
        "status": None,
        "raw_status": None,
    }


def test_tracking_url_encodes_once_and_handles_none():
    assert tracking_url(None) is None
    assert tracking_url("ab c/1") == (
        "https://packages.speedeedelivery.com/package_progress.php?barcode=ab%20c%2F1"
    )


# ---------------------------------------------------------------------------
# normalize_parcel — the canonical contract
# ---------------------------------------------------------------------------

CANONICAL_KEYS = [
    "carrier",
    "barcode",
    "sender",
    "receiver",
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


def test_normalize_publishes_exactly_the_canonical_keys():
    """The aggregator and cross-carrier dashboards depend on this key set."""
    assert list(normalize_parcel(delivered_sample())) == CANONICAL_KEYS


def test_capabilities_are_known_values():
    """A typo here would silently misreport this carrier on the docs site."""
    assert CAPABILITIES <= KNOWN_CAPABILITIES
    assert PENDING_CAPABILITIES <= KNOWN_CAPABILITIES


def test_a_capability_is_never_both_populated_and_pending():
    assert not CAPABILITIES & PENDING_CAPABILITIES


def test_capabilities_match_what_normalize_parcel_actually_returns():
    """Declared fields are populated; every undeclared optional field is None."""
    delivered = normalize_parcel(delivered_sample())
    with_history = normalize_parcel(delivered_sample(), include_history=True)

    assert {"url", "history"} == CAPABILITIES
    assert delivered["url"] is not None
    assert with_history["history"]
    for field in ("weight", "dimensions", "pickup_point", "planned_from", "planned_to"):
        assert delivered[field] is None


def test_normalize_delivered_parcel():
    parcel = normalize_parcel(delivered_sample())
    assert parcel["carrier"] == "Spee-Dee Delivery"
    assert parcel["barcode"] == "EXAMPLE123456"
    assert parcel["status"] == ParcelStatus.DELIVERED
    assert parcel["raw_status"] == "Delivered"
    assert parcel["delivered"] is True
    assert parcel["delivered_at"] == "2026-04-29T13:12:00-05:00"
    assert parcel["pickup"] is False
    assert parcel["history"] is None


def test_normalize_active_parcel_has_no_delivery_data():
    parcel = normalize_parcel(active_sample())
    assert parcel["status"] == ParcelStatus.OUT_FOR_DELIVERY
    assert parcel["delivered"] is False
    assert parcel["delivered_at"] is None
    assert parcel["sender"] is None
    assert parcel["receiver"] is None


def test_delivered_on_is_ignored_for_a_non_delivered_summary():
    raw = active_sample()
    raw["deliveredOn"] = "2026-04-29T13:12:00-05:00"
    assert normalize_parcel(raw)["delivered_at"] is None


def test_summary_status_wins_over_the_newest_event():
    """After a missort the summary can be back in transit; history keeps it."""
    raw = delivered_sample()
    raw.update(summaryStatus="ARRIVAL SCAN", deliveredOn=None)
    raw["events"].append(event("** MISSORT SCANS **", "2026-04-30T06:00:00-05:00"))
    parcel = normalize_parcel(raw, include_history=True)
    assert parcel["status"] == ParcelStatus.IN_TRANSIT
    assert parcel["history"][-1]["status"] == ParcelStatus.PROBLEM


def test_normalize_history_is_opt_in():
    assert normalize_parcel(delivered_sample())["history"] is None
    history = normalize_parcel(delivered_sample(), include_history=True)["history"]
    assert len(history) == 4


def test_normalize_pending_placeholder():
    """A tracked-but-not-yet-known code still yields a full parcel dict."""
    parcel = normalize_parcel({"trackingNumber": "EXAMPLE000001"})
    assert parcel["status"] == ParcelStatus.UNKNOWN
    assert parcel["delivered"] is False
    assert parcel["raw_status"] is None
    assert parcel["history"] is None


def test_detail_suffix_is_cut_from_raw_status_but_kept_in_raw():
    """A five-digit scan suffix could be a location, so it stays in ``raw``."""
    sample = active_sample()
    sample["summaryStatus"] = "OUT FOR DELIVERY (22045)"
    sample["events"] = [event("UNLOAD SCAN: 01055", "2026-04-29T08:46:00-05:00")]

    parcel = normalize_parcel(sample, include_history=True)

    assert parcel["raw_status"] == "OUT FOR DELIVERY"
    assert parcel["history"][0]["raw_status"] == "UNLOAD SCAN"
    assert parcel["history"][0]["status"] == ParcelStatus.IN_TRANSIT
    assert parcel["raw"]["summaryStatus"] == "OUT FOR DELIVERY (22045)"
    assert parcel["raw"]["events"][0]["activity"] == "UNLOAD SCAN: 01055"


def test_normalize_keeps_raw_payload():
    raw = active_sample()
    assert normalize_parcel(raw)["raw"] is raw


def test_no_personal_data_reaches_a_normalised_field():
    parcel = normalize_parcel(delivered_sample(), include_history=True)

    # ``raw`` is the whole record by design; every field above it — including
    # the history, which survives the aggregator's strip_raw() — must be free
    # of the signature, the destination and the per-scan locations.
    canonical = {key: value for key, value in parcel.items() if key != "raw"}
    assert not any(value in repr(canonical) for value in PII)


# ---------------------------------------------------------------------------
# sort_parcels_by_ts
# ---------------------------------------------------------------------------


def test_sort_parcels_ascending_puts_unparseable_last():
    parcels = [
        {"barcode": "a", "planned_from": "2026-05-02T10:00:00Z"},
        {"barcode": "b", "planned_from": None},
        {"barcode": "c", "planned_from": "2026-05-01T10:00:00Z"},
    ]
    ordered = [p["barcode"] for p in sort_parcels_by_ts(parcels, "planned_from")]
    assert ordered == ["c", "a", "b"]


def test_sort_parcels_descending_still_puts_unparseable_last():
    parcels = [
        {"barcode": "a", "delivered_at": "2026-05-02T10:00:00Z"},
        {"barcode": "b", "delivered_at": "nonsense"},
        {"barcode": "c", "delivered_at": "2026-05-01T10:00:00Z"},
    ]
    ordered = [
        p["barcode"]
        for p in sort_parcels_by_ts(parcels, "delivered_at", descending=True)
    ]
    assert ordered == ["a", "c", "b"]


# ---------------------------------------------------------------------------
# apply_delivered_filter
# ---------------------------------------------------------------------------


def _entry(filter_type: str, amount: int) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        options={
            CONF_DELIVERED_FILTER_TYPE: filter_type,
            CONF_DELIVERED_FILTER_AMOUNT: amount,
        },
        unique_id=DOMAIN,
    )


def _delivered_pair() -> list[dict]:
    now = datetime.now(UTC)
    return [
        {"barcode": "RECENT", "delivered_at": (now - timedelta(days=1)).isoformat()},
        {"barcode": "OLD", "delivered_at": (now - timedelta(days=30)).isoformat()},
    ]


def test_delivered_filter_by_days():
    kept = apply_delivered_filter(_delivered_pair(), _entry("days", 7))
    assert [p["barcode"] for p in kept] == ["RECENT"]


def test_delivered_filter_by_count():
    parcels = _delivered_pair()
    assert apply_delivered_filter(parcels, _entry("parcels", 1)) == parcels[:1]


def test_delivered_filter_keeps_unparseable_timestamp():
    """Better to show a parcel with a broken date than to silently drop it."""
    parcels = [{"barcode": "WEIRD", "delivered_at": "nonsense"}]
    assert apply_delivered_filter(parcels, _entry("days", 7)) == parcels
