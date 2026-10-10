"""Synthetic Spee-Dee Delivery payloads shared by the test modules.

Everything here is invented: barcodes, names, addresses, signatures, depots
and dates. Only the page's structure is modelled — element order, the
``u-full-width`` summary table, the ``Package Progress`` heading, header
labels, ``<br>`` and whitespace quirks. Keep them in one module rather than
inline in each test so there is exactly one place to fix.
"""
from __future__ import annotations

ACTIVE_CODE = "EXAMPLE999999"
DELIVERED_CODE = "EXAMPLE123456"

# Invented personal data. It belongs in ``raw`` — that is the whole record,
# untrimmed — so the tests assert instead that none of it reaches a
# normalised field, the history or unredacted diagnostics.
PII = ("Pat Example", "1 Invented Way", "Zed Sample", "Fakeville Depot")

FOOTER = "<p>All times shown are Central Time (USA).</p>"


def summary_html(rows: list[tuple[str, str]]) -> str:
    """The label/value summary table."""
    body = "".join(
        f'<tr class="{"odd" if i % 2 else "even"}">'
        f"<td>\n    {label}\n  </td><td>\n    {value}\n  </td></tr>\n"
        for i, (label, value) in enumerate(rows)
    )
    return f'<table class="u-full-width">\n<tbody>\n{body}</tbody>\n</table>\n'


def progress_html(
    events: list[tuple[str, str, str]],
    headers: tuple[str, ...] = ("Date / Time", "Location", "Activity"),
) -> str:
    """The progress table, rows exactly as given (newest first on the page)."""
    head = "".join(f"<th>{h}</th>" for h in headers)
    body = "".join(
        f'<tr class="{"odd" if i % 2 else "even"}">'
        f"<td>{when}</td><td>{where}</td><td>{what}</td></tr>\n"
        for i, (when, where, what) in enumerate(events)
    )
    return (
        "<h3>Package Progress</h3>\n"
        f'<table class="u-full-width">\n<thead><tr>{head}</tr></thead>\n'
        f"<tbody>\n{body}</tbody>\n</table>\n"
    )


def page(summary: str, progress: str, footer: str = FOOTER) -> str:
    """A whole package page around the two tables."""
    return (
        "<!DOCTYPE html>\n<html><head><title>Package Progress</title>"
        "<style>.u-full-width{width:100%}</style></head>\n<body>\n"
        '<div class="container">\n<h1>Track a Shipment</h1>\n'
        f"{summary}{progress}{footer}\n</div>\n"
        "<script>var x = 'Central Time (USA)';</script></body></html>\n"
    )


DELIVERED_EVENTS = [
    ("04/29/2026 01:12:42 PM", "Zed Sample, KS", "Delivered"),
    ("04/29/2026 08:46:00 AM", "Fakeville Depot", "OUT FOR DELIVERY (Route 12)"),
    ("04/28/2026 03:52:17 PM", "Fakeville Depot", "UNLOAD SCAN: Dock 4"),
    ("04/28/2026 06:01:00 AM", "Fakeville Depot", "ARRIVAL SCAN"),
]


def delivered_html(code: str = DELIVERED_CODE) -> str:
    """A delivered parcel page with recipient and signature data."""
    return page(
        summary_html(
            [
                ("Status:", "Delivered"),
                ("Delivered on:", "04/29/2026 01:12 PM"),
                ("Signed by:", "Pat<br>Example"),
                ("Delivered to:", "1 Invented Way<br/>Zed Sample, KS"),
                ("Tracking Number:", code),
            ]
        ),
        progress_html(DELIVERED_EVENTS),
    )


def in_transit_html(code: str = ACTIVE_CODE) -> str:
    """An out-for-delivery page with no delivery rows."""
    return page(
        summary_html(
            [
                ("Status:", "OUT FOR DELIVERY (Route 12)"),
                ("Tracking Number:", code),
            ]
        ),
        progress_html(DELIVERED_EVENTS[1:]),
    )


def event(activity: str, timestamp: str | None, location: str | None = None) -> dict:
    """One entry of the parsed, oldest-first event list."""
    return {"timestamp": timestamp, "location": location, "activity": activity}


def delivered_sample(code: str = DELIVERED_CODE) -> dict:
    """The parsed raw payload of a delivered parcel."""
    return {
        "trackingNumber": code,
        "summaryStatus": "Delivered",
        "deliveredOn": "2026-04-29T13:12:00-05:00",
        "signedBy": "Pat Example",
        "deliveredTo": "1 Invented Way Zed Sample, KS",
        "events": [
            event("ARRIVAL SCAN", "2026-04-28T06:01:00-05:00", "Fakeville Depot"),
            event("UNLOAD SCAN: Dock 4", "2026-04-28T15:52:17-05:00", "Fakeville Depot"),
            event(
                "OUT FOR DELIVERY (Route 12)",
                "2026-04-29T08:46:00-05:00",
                "Fakeville Depot",
            ),
            event("Delivered", "2026-04-29T13:12:42-05:00", "Zed Sample, KS"),
        ],
    }


def active_sample(code: str = ACTIVE_CODE) -> dict:
    """The parsed raw payload of a parcel out for delivery."""
    sample = delivered_sample(code)
    sample.update(
        {
            "summaryStatus": "OUT FOR DELIVERY (Route 12)",
            "deliveredOn": None,
            "signedBy": None,
            "deliveredTo": None,
            "events": sample["events"][:-1],
        }
    )
    return sample
