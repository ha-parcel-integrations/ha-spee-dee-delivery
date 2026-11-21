"""Canonical parcel shape, status mapping and list helpers.

Everything in this module is a **pure function** — no I/O, no Home Assistant
objects beyond the config entry's options. That is deliberate: it keeps the
carrier-specific mapping (which you rewrite per carrier) apart from the
coordinator (which is nearly identical everywhere), and it makes the mapping
trivially unit-testable without spinning up HA.

Two things here are carrier-specific: :func:`classify_status` and
:func:`normalize_parcel`. Everything else — the
timestamp parsing, the history builder, the sort contract, the delivered
filter, the one-shot warning for unmapped statuses — is suite-wide machinery
and should be left alone.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, date, datetime, time, timedelta, tzinfo
import logging
import re
from typing import Any
from urllib.parse import quote

from homeassistant.config_entries import ConfigEntry

from .const import (
    CONF_DELIVERED_FILTER_AMOUNT,
    CONF_DELIVERED_FILTER_TYPE,
    DEFAULT_DELIVERED_FILTER_AMOUNT,
    DEFAULT_DELIVERED_FILTER_TYPE,
    HISTORY_MAX_EVENTS,
    TRACKING_URL,
    ParcelStatus,
)

_LOGGER = logging.getLogger(__name__)

# Where users report a status we do not map yet. Rewritten by the bootstrap
# script; it must point at the carrier's own repo so the log line is
# copy-pasteable straight into a new issue.
#
# The ``?template=`` parameter matters: without it the link opens a blank form,
# and the report comes back missing the version and the log line we need.
NEW_ISSUE_URL = (
    "https://github.com/ha-parcel-integrations/ha-spee-dee-delivery/issues/new"
    "?template=unrecognised_status.yml"
)

_EXACT_STATUSES: dict[str, ParcelStatus] = {
    "delivered": ParcelStatus.DELIVERED,
    "arrival scan": ParcelStatus.IN_TRANSIT,
    "in transit": ParcelStatus.IN_TRANSIT,
    "** missort scans **": ParcelStatus.PROBLEM,
}
# The varying tail of a scan line: everything from the first "(" or ":".
_DETAIL_SUFFIX_RE = re.compile(r"\s*[(:].*$", re.DOTALL)

_PREFIX_STATUSES: tuple[tuple[str, ParcelStatus], ...] = (
    ("out for delivery", ParcelStatus.OUT_FOR_DELIVERY),
    ("unload scan", ParcelStatus.IN_TRANSIT),
)

# Status texts we have already warned about, so each unmapped one is logged
# only once per HA session instead of on every poll.
_unmapped_statuses_logged: set[str] = set()


def _normalize_status(text: str | None) -> str:
    """Collapse whitespace and case so matching ignores presentation."""
    return re.sub(r"\s+", " ", text or "").strip().lower()


def classify_status(text: str | None) -> ParcelStatus | None:
    """Map one status or activity text, or return ``None`` when unrecognised.

    Matching is case-insensitive on whitespace-collapsed text. The prefix
    entries carry a trailing detail (``OUT FOR DELIVERY (...)``,
    ``UNLOAD SCAN: ...``) that varies per scan.
    """
    key = _normalize_status(text)
    if key in _EXACT_STATUSES:
        return _EXACT_STATUSES[key]
    for prefix, status in _PREFIX_STATUSES:
        if key.startswith(prefix):
            return status
    return None


def _warn_unmapped_status(text: str) -> None:
    """Log an unmapped carrier status once, with a copy-paste issue link."""
    # Only the text before any detail suffix is logged: that suffix can carry
    # a place name.
    stem = re.split(r"[(:]", _normalize_status(text), maxsplit=1)[0].strip()[:40]
    if stem in _unmapped_statuses_logged:
        return
    _unmapped_statuses_logged.add(stem)
    _LOGGER.warning(
        "Unrecognised Spee-Dee Delivery status — help us map it. Open an issue "
        "and paste this line: %s\n  status=%s → reported as 'unknown'",
        NEW_ISSUE_URL,
        stem,
    )


def map_parcel_status(text: str | None) -> ParcelStatus:
    """Map the summary status to a canonical :class:`ParcelStatus`.

    An empty status reports ``unknown`` silently; an unrecognised one reports
    ``unknown`` with a one-shot warning.
    """
    if not _normalize_status(text):
        return ParcelStatus.UNKNOWN
    mapped = classify_status(text)
    if mapped is not None:
        return mapped
    _warn_unmapped_status(text or "")
    return ParcelStatus.UNKNOWN


def _status_from_history(raw: dict) -> ParcelStatus | None:
    """Return the newest progress row that does map, else ``None``.

    A parcel whose summary carries a wording we have never seen is almost
    never a parcel in an unknown state — its scans still say where it is.
    """
    events = raw.get("events")
    if not isinstance(events, list):
        return None
    for event in reversed(events):
        if not isinstance(event, dict):
            continue
        mapped = classify_status(event.get("activity"))
        if mapped is not None:
            return mapped
    return None


def map_event_status(text: str | None) -> ParcelStatus | None:
    """Map a history entry's activity to a canonical status, or ``None``.

    Unmapped text keeps ``status: null`` on the history entry (rather than
    ``unknown``, so a consumer can tell "no mapping" from "mapped to unknown")
    and warns once, reusing the parcel-status one-shot set.
    """
    if not _normalize_status(text):
        return None
    mapped = classify_status(text)
    if mapped is not None:
        return mapped
    _warn_unmapped_status(text or "")
    return None


def parse_iso(value: str | None) -> datetime | None:
    """Parse an ISO 8601 string to an aware datetime, or ``None`` on failure.

    Naive values are treated as UTC so a list always sorts without crashing on
    a mixed set.
    """
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def build_history(
    events: list | None, *, max_events: int = HISTORY_MAX_EVENTS
) -> list[dict]:
    """Build the canonical ``history`` list from the carrier's event list.

    Each entry is ``{timestamp, status, raw_status}`` — identical across all
    suite carriers, and top-level (not under ``raw``) so it survives the
    aggregator's ``strip_raw()``. ``raw_status`` is the carrier's own text, or
    its event code when the API has no human-readable text. Sorted oldest →
    newest and capped to the most recent ``max_events``.

    Events arrive oldest first with an ISO timestamp (or ``None`` when the
    page's time zone could not be trusted, in which case they are skipped).
    """
    parseable: list[tuple[datetime, dict]] = []
    unparseable: list[dict] = []
    for event in events or []:
        if not isinstance(event, dict):
            continue
        timestamp = event.get("timestamp")
        if not timestamp:
            continue
        entry = {
            "timestamp": timestamp,
            "status": map_event_status(event.get("activity")),
            "raw_status": _normalize_status_text(event.get("activity")),
        }
        parsed = parse_iso(timestamp)
        if parsed is None:
            unparseable.append(entry)
        else:
            parseable.append((parsed, entry))
    parseable.sort(key=lambda item: item[0])
    ordered = [entry for _, entry in parseable] + unparseable
    return ordered[-max_events:]


def tracking_url(tracking_code: str | None) -> str | None:
    """Construct the consumer tracking deep-link for a parcel."""
    if not tracking_code:
        return None
    return TRACKING_URL.format(tracking_code=quote(tracking_code, safe=""))


def _normalize_status_text(text: str | None) -> str | None:
    """Return the carrier's own text without its per-scan detail suffix.

    ``OUT FOR DELIVERY (22045)`` and ``UNLOAD SCAN: 01055`` end in a code that
    cannot be ruled out as a location, and this value rides in the top-level
    ``raw_status`` and ``history`` — the part that survives the aggregator's
    ``strip_raw()`` and reaches entity attributes. ``raw`` keeps the full text.
    """
    collapsed = re.sub(r"\s+", " ", text or "").strip()
    return _DETAIL_SUFFIX_RE.sub("", collapsed).strip() or None


def _moment(value: Any, zone: tzinfo) -> tuple[datetime, bool, str] | None:
    """Return ``(aware datetime, date_only, text)`` for a carrier time, or ``None``.

    A naive time or a bare date gets ``zone``; an aware time keeps its offset
    and, when it came in as a string, its own spelling.
    """
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        moment = datetime.combine(value, time.min, tzinfo=zone)
        return moment, True, moment.isoformat()
    elif isinstance(value, str) and value:
        try:
            if len(value) == 10:
                moment = datetime.combine(
                    date.fromisoformat(value), time.min, tzinfo=zone
                )
                return moment, True, moment.isoformat()
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
        if parsed.tzinfo:
            return parsed, False, value
    else:
        return None
    moment = parsed if parsed.tzinfo else parsed.replace(tzinfo=zone)
    return moment, False, moment.isoformat()


def _end_of_day(moment: datetime, zone: tzinfo) -> datetime:
    local = moment.astimezone(zone)
    return datetime.combine(local.date(), time(23, 59, 59), tzinfo=zone)


def planned_window(
    start: Any, end: Any, *, zone: tzinfo
) -> tuple[str | None, str | None]:
    """Return ``(planned_from, planned_to)`` in the canonical window shape.

    ``start`` and ``end`` are ISO strings, dates or datetimes, or ``None``; an
    epoch number must be converted first (see ``to_iso_timestamp``). A window
    keeps both ends. One moment with a time ends at ``23:59:59`` the same local
    day; a date covers ``00:00:00`` to ``23:59:59``; two dates run from the
    first day's start to the last day's end. Equal values are one moment,
    reversed values are swapped, and ``planned_to`` is never set without
    ``planned_from``. ``zone`` is the carrier's own zone: it dates naive times
    and bare dates and decides which day a moment ends on, so a ``Z`` moment
    still ends on the carrier's local day. When nothing is left of that day
    after the moment, there is no ``planned_to``.
    """
    first = _moment(start, zone)
    last = _moment(end, zone)
    if first is None:
        first, last = last, None
    if first is None:
        return None, None

    def closing(moment: tuple[datetime, bool, str]) -> datetime:
        return _end_of_day(moment[0], zone) if moment[1] else moment[0]

    if last is not None and closing(last) < first[0]:
        first, last = last, first
    if last is not None and closing(last) == first[0]:
        last = None

    if last is None:
        end_of_day = _end_of_day(first[0], zone)
        return first[2], end_of_day.isoformat() if end_of_day > first[0] else None
    return first[2], closing(last).isoformat() if last[1] else last[2]


_NO_WINDOW_STATUSES = frozenset(
    {ParcelStatus.AT_PICKUP_POINT, ParcelStatus.RETURNING, ParcelStatus.DELIVERED}
)


def planned_for_status(
    status: ParcelStatus, planned_from: str | None, planned_to: str | None
) -> tuple[str | None, str | None]:
    """Empty the delivery window once the parcel is at its destination or done."""
    if status in _NO_WINDOW_STATUSES:
        return None, None
    return planned_from, planned_to


def is_pickup(
    route_flag: bool | None,
    status: ParcelStatus,
    event_statuses: Iterable[ParcelStatus | None] = (),
) -> bool:
    """Whether the delivery route ends at a pickup point.

    The carrier's own route flag wins; otherwise the parcel having reached
    ``AT_PICKUP_POINT`` at any point counts, so the value survives collection.
    """
    if route_flag:
        return True
    return status is ParcelStatus.AT_PICKUP_POINT or any(
        event is ParcelStatus.AT_PICKUP_POINT for event in event_statuses
    )


def normalize_parcel(raw: dict, *, include_history: bool = False) -> dict:
    """Return a carrier-agnostic parcel dict with the payload under ``raw``.

    The **keys of the returned dict are the contract**: every carrier in the
    suite returns exactly these, in this order. ``raw`` carries the page's
    recipient, destination, signature and per-scan locations untrimmed; none
    of them is read into a normalised field, and ``diagnostics.py`` redacts
    them.
    """
    tracking_code = raw.get("trackingNumber")
    summary_status = raw.get("summaryStatus")
    status = map_parcel_status(summary_status)
    if status is ParcelStatus.UNKNOWN:
        status = _status_from_history(raw) or ParcelStatus.UNKNOWN
    delivered = status is ParcelStatus.DELIVERED
    pickup = is_pickup(
        None,
        status,
        (entry["status"] for entry in build_history(raw.get("events"))),
    )

    return {
        "carrier": "Spee-Dee Delivery",
        "barcode": tracking_code,
        "sender": None,
        "receiver": None,
        "status": status,
        "raw_status": _normalize_status_text(summary_status),
        "delivered": delivered,
        "delivered_at": raw.get("deliveredOn") if delivered else None,
        "planned_from": None,
        "planned_to": None,
        "pickup": pickup,
        "pickup_point": None,
        "url": tracking_url(tracking_code),
        "weight": None,
        "dimensions": None,
        "history": build_history(raw.get("events")) if include_history else None,
        "raw": raw,
    }


def sort_parcels_by_ts(
    parcels: list[dict], key_field: str, *, descending: bool = False
) -> list[dict]:
    """Return normalised parcels sorted by the ISO timestamp at ``key_field``.

    The suite's sort contract: incoming/outgoing ascending on ``planned_from``,
    delivered descending on ``delivered_at``. Parcels whose value is missing or
    unparseable always sort to the end, regardless of ``descending``.
    """
    with_ts: list[tuple[datetime, dict]] = []
    without_ts: list[dict] = []
    for parcel in parcels:
        parsed = parse_iso(parcel.get(key_field))
        if parsed is None:
            without_ts.append(parcel)
        else:
            with_ts.append((parsed, parcel))
    with_ts.sort(key=lambda item: item[0], reverse=descending)
    return [parcel for _, parcel in with_ts] + without_ts


def apply_delivered_filter(parcels: list[dict], entry: ConfigEntry) -> list[dict]:
    """Trim the delivered list per the entry's retention option.

    ``parcels`` must already be sorted newest-first. ``days`` keeps deliveries
    from the last N days (an unparseable ``delivered_at`` is kept rather than
    silently dropped); the ``parcels`` type keeps the N most recent. Parcels
    stay *tracked* either way — this only controls what the delivered sensor
    shows.
    """
    options = entry.options
    filter_type = options.get(CONF_DELIVERED_FILTER_TYPE, DEFAULT_DELIVERED_FILTER_TYPE)
    amount = int(
        options.get(CONF_DELIVERED_FILTER_AMOUNT, DEFAULT_DELIVERED_FILTER_AMOUNT)
    )
    if filter_type == "days":
        cutoff = datetime.now(UTC) - timedelta(days=amount)
        return [
            parcel
            for parcel in parcels
            if (parsed := parse_iso(parcel.get("delivered_at"))) is None
            or parsed >= cutoff
        ]
    return parcels[:amount]
