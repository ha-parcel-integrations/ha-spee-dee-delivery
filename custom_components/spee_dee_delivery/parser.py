"""Parser for the Spee-Dee Delivery package page.

The page has no structured alternative, so the parser
fails closed: anything it cannot positively identify raises
:class:`SpeeDeeDeliveryParseError` instead of yielding a partial parcel. Values
are matched on label text and column headers, never on table position or CSS
row classes. Every value the page carries reaches ``raw`` untrimmed, including
the recipient, destination, signature and per-scan location; keeping them out
of diagnostics is ``diagnostics.py``'s job, not the parser's.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime
from html.parser import HTMLParser
from zoneinfo import ZoneInfo

_LOGGER = logging.getLogger(__name__)

DISPLAY_TIMEZONE = "America/Chicago"
_TIMEZONE_FOOTER = "central time (usa)"
_PROGRESS_HEADING = "package progress"
_SUMMARY_CLASS = "u-full-width"
_PROGRESS_HEADERS = ("date / time", "location", "activity")
_KNOWN_LABELS = frozenset(
    {"status", "delivered on", "signed by", "delivered to", "tracking number"}
)
_TIMESTAMP_FORMATS = ("%m/%d/%Y %I:%M:%S %p", "%m/%d/%Y %I:%M %p")
_VOID_TAGS = frozenset(
    {"br", "hr", "img", "input", "link", "meta", "area", "base", "col", "wbr"}
)
_IGNORED_TEXT_TAGS = frozenset({"script", "style"})

_warned: set[str] = set()


class SpeeDeeDeliveryParseError(Exception):
    """Raised when the page does not have the expected tracking shape."""


def clean_text(value: str) -> str:
    """Collapse whitespace (including non-breaking spaces) to single spaces."""
    return re.sub(r"\s+", " ", value.replace("\xa0", " ")).strip()


def warn_once(key: str, message: str, *args: object) -> None:
    """Log a value-free schema warning once per Home Assistant session."""
    if key in _warned:
        return
    _warned.add(key)
    _LOGGER.warning(message, *args)


class _Table:
    """One table: its classes, the heading before it and its rows of cells."""

    def __init__(self, classes: set[str], heading: str) -> None:
        self.classes = classes
        self.heading = heading
        self.rows: list[list[str]] = []


class _PageParser(HTMLParser):
    """Collect every table with the last ``h3`` text seen before it."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: list[_Table] = []
        self.text_parts: list[str] = []
        self._heading = ""
        self._heading_parts: list[str] | None = None
        self._table: _Table | None = None
        self._row: list[str] | None = None
        self._cell_parts: list[str] | None = None
        self._ignored = 0

    def handle_starttag(self, tag, attrs):
        if tag in _IGNORED_TEXT_TAGS:
            self._ignored += 1
        elif tag == "table":
            if self._table is not None:
                raise SpeeDeeDeliveryParseError("nested table")
            classes = set((dict(attrs).get("class") or "").split())
            self._table = _Table(classes, self._heading)
            self.tables.append(self._table)
        elif tag == "tr" and self._table is not None:
            self._close_row()
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._close_cell()
            self._cell_parts = []
        elif tag == "h3":
            self._heading_parts = []
        elif tag == "br":
            self._text(" ")

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in _VOID_TAGS:
            self.handle_endtag(tag)

    def _text(self, data: str) -> None:
        if self._ignored:
            return
        self.text_parts.append(data)
        if self._cell_parts is not None:
            self._cell_parts.append(data)
        if self._heading_parts is not None:
            self._heading_parts.append(data)

    def handle_data(self, data):
        self._text(data)

    def _close_cell(self) -> None:
        if self._cell_parts is not None and self._row is not None:
            self._row.append(clean_text("".join(self._cell_parts)))
        self._cell_parts = None

    def _close_row(self) -> None:
        self._close_cell()
        if self._row is not None and self._table is not None:
            self._table.rows.append(self._row)
        self._row = None

    def handle_endtag(self, tag):
        if tag in _IGNORED_TEXT_TAGS:
            self._ignored = max(0, self._ignored - 1)
        elif tag in ("td", "th"):
            self._close_cell()
        elif tag == "tr":
            self._close_row()
        elif tag == "table" and self._table is not None:
            self._close_row()
            self._table = None
        elif tag == "h3" and self._heading_parts is not None:
            self._heading = clean_text("".join(self._heading_parts)).lower()
            self._heading_parts = None

    def close(self):
        super().close()
        self._close_row()


def _summary(tables: list[_Table]) -> dict[str, str]:
    """Return the label-keyed summary of the first ``u-full-width`` table."""
    for table in tables:
        if _SUMMARY_CLASS not in table.classes:
            continue
        found: dict[str, str] = {}
        for row in table.rows:
            if len(row) < 2:
                continue
            label = row[0].rstrip(":").strip().lower()
            if not label:
                continue
            if label in found:
                raise SpeeDeeDeliveryParseError("duplicate summary label")
            found[label] = row[1]
        return found
    raise SpeeDeeDeliveryParseError("summary table not found")


def _progress(tables: list[_Table]) -> list[tuple[str, str, str]]:
    """Return ``(date_time, location, activity)``, newest first as on the page."""
    for table in tables:
        if table.heading != _PROGRESS_HEADING:
            continue
        if not table.rows:
            raise SpeeDeeDeliveryParseError("progress table has no header row")
        header = [cell.lower() for cell in table.rows[0]]
        if any(name not in header for name in _PROGRESS_HEADERS):
            raise SpeeDeeDeliveryParseError("progress headers missing")
        date_at, location_at, activity_at = (
            header.index(n) for n in _PROGRESS_HEADERS
        )
        width = len(header)
        events = []
        for row in table.rows[1:]:
            if not any(row):
                continue
            if len(row) != width:
                raise SpeeDeeDeliveryParseError("progress row has the wrong width")
            events.append((row[date_at], row[location_at], row[activity_at]))
        return events
    raise SpeeDeeDeliveryParseError("progress table not found")


def _to_iso(value: str, zone: ZoneInfo | None) -> str | None:
    """Return the page's naive local timestamp as an aware ISO string."""
    if zone is None or not value:
        return None
    for fmt in _TIMESTAMP_FORMATS:
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=zone).isoformat()
        except ValueError:
            continue
    warn_once("timestamp", "Spee-Dee Delivery returned an unparseable timestamp")
    return None


def parse_page(html: str, tracking_code: str) -> dict:
    """Parse a package page into the ``raw`` payload.

    The result carries every summary label and progress cell the page has —
    status, delivery moment, signature, destination and the per-scan
    locations — oldest first. Raises
    :class:`SpeeDeeDeliveryParseError` for any page without both the summary
    and the progress table.
    """
    parser = _PageParser()
    parser.feed(html)
    parser.close()

    summary = _summary(parser.tables)
    status = summary.get("status", "")
    if not status:
        raise SpeeDeeDeliveryParseError("summary status missing")
    unexpected = sorted(set(summary) - _KNOWN_LABELS)
    if unexpected:
        warn_once(
            "labels",
            "Spee-Dee Delivery summary has unexpected labels: %d unknown",
            len(unexpected),
        )
    if summary.get("tracking number") != tracking_code.strip():
        warn_once(
            "mismatch", "Spee-Dee Delivery summary tracking number does not match"
        )

    events = _progress(parser.tables)

    zone: ZoneInfo | None = None
    if _TIMEZONE_FOOTER in clean_text(" ".join(parser.text_parts)).lower():
        zone = ZoneInfo(DISPLAY_TIMEZONE)
    else:
        warn_once("timezone", "Spee-Dee Delivery page lacks the expected time zone")

    return {
        "trackingNumber": tracking_code,
        "summaryStatus": status,
        "deliveredOn": _to_iso(summary.get("delivered on", ""), zone),
        "signedBy": summary.get("signed by") or None,
        "deliveredTo": summary.get("delivered to") or None,
        "events": [
            {
                "timestamp": _to_iso(when, zone),
                "location": where or None,
                "activity": activity,
            }
            for when, where, activity in reversed(events)
        ],
    }
