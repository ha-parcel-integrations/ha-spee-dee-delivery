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

from datetime import UTC, datetime, timedelta
import logging
import re
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
        "pickup": False,
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
