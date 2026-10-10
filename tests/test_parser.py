"""Tests for the package page parser, on synthetic pages only."""
import pytest

from custom_components.spee_dee_delivery.parser import (
    SpeeDeeDeliveryParseError,
    parse_page,
)

from .payloads import (
    ACTIVE_CODE,
    DELIVERED_CODE,
    PII,
    delivered_html,
    in_transit_html,
    page,
    progress_html,
    summary_html,
)

SUMMARY = summary_html([("Status:", "ARRIVAL SCAN"), ("Tracking Number:", ACTIVE_CODE)])
PROGRESS = progress_html([("01/15/2026 09:30:00 AM", "Fakeville Depot", "ARRIVAL SCAN")])


def _parse(html: str, code: str = ACTIVE_CODE) -> dict:
    return parse_page(html, code)


def test_delivered_page_is_parsed_oldest_first():
    raw = _parse(delivered_html(), DELIVERED_CODE)

    assert raw["trackingNumber"] == DELIVERED_CODE
    assert raw["summaryStatus"] == "Delivered"
    assert raw["deliveredOn"] == "2026-04-29T13:12:00-05:00"
    assert [e["activity"] for e in raw["events"]] == [
        "ARRIVAL SCAN",
        "UNLOAD SCAN: Dock 4",
        "OUT FOR DELIVERY (Route 12)",
        "Delivered",
    ]
    assert raw["events"][0]["timestamp"] == "2026-04-28T06:01:00-05:00"
    assert raw["events"][-1]["timestamp"] == "2026-04-29T13:12:42-05:00"
    assert set(raw) == {
        "trackingNumber",
        "summaryStatus",
        "deliveredOn",
        "signedBy",
        "deliveredTo",
        "events",
    }


def test_raw_carries_the_whole_record_untrimmed():
    raw = _parse(delivered_html(), DELIVERED_CODE)

    # ``raw`` is never trimmed for privacy — diagnostics redaction is where
    # that lives — so every value the page shows has to survive the parse.
    assert raw["signedBy"] == "Pat Example"
    assert raw["deliveredTo"] == "1 Invented Way Zed Sample, KS"
    assert [e["location"] for e in raw["events"]] == [
        "Fakeville Depot",
        "Fakeville Depot",
        "Fakeville Depot",
        "Zed Sample, KS",
    ]
    assert all(value in repr(raw) for value in PII)


def test_absent_signature_and_destination_are_none():
    raw = _parse(in_transit_html())
    assert raw["signedBy"] is None
    assert raw["deliveredTo"] is None


def test_in_transit_page_has_no_delivery_moment():
    raw = _parse(in_transit_html())
    assert raw["summaryStatus"] == "OUT FOR DELIVERY (Route 12)"
    assert raw["deliveredOn"] is None
    assert len(raw["events"]) == 3


def test_unrecognised_status_text_passes_through_unmapped():
    html = page(
        summary_html([("Status:", "TELEPORTED"), ("Tracking Number:", ACTIVE_CODE)]),
        PROGRESS,
    )
    assert _parse(html)["summaryStatus"] == "TELEPORTED"


def test_whitespace_and_br_are_normalised():
    html = page(
        summary_html([("Status:", "OUT FOR<br>\n   DELIVERY&nbsp;(Route 1)"), ("Tracking Number:", ACTIVE_CODE)]),
        PROGRESS,
    )
    assert _parse(html)["summaryStatus"] == "OUT FOR DELIVERY (Route 1)"


def test_labels_and_headers_are_case_insensitive_and_position_independent():
    html = page(
        summary_html([("tracking number", ACTIVE_CODE), ("STATUS :", "Delivered")]),
        progress_html(
            [("Zed", "07/04/2026 10:00 AM", "Delivered")],
            headers=("Location", "date / time", "ACTIVITY"),
        ),
    )
    raw = _parse(html)
    assert raw["summaryStatus"] == "Delivered"
    assert raw["events"][0]["activity"] == "Delivered"
    assert raw["events"][0]["timestamp"] == "2026-07-04T10:00:00-05:00"


def test_daylight_saving_is_applied_not_a_fixed_offset():
    html = page(
        summary_html([("Status:", "Delivered"), ("Delivered on:", "11/01/2026 01:30 PM"), ("Tracking Number:", ACTIVE_CODE)]),
        progress_html(
            [
                ("11/01/2026 01:30:00 PM", "X", "Delivered"),
                ("03/08/2026 03:30:00 AM", "X", "ARRIVAL SCAN"),
                ("03/08/2026 01:30:00 AM", "X", "ARRIVAL SCAN"),
                ("01/15/2026 09:30:00 AM", "X", "ARRIVAL SCAN"),
                ("07/15/2026 09:30:00 AM", "X", "ARRIVAL SCAN"),
            ]
        ),
    )
    raw = _parse(html)
    offsets = {e["timestamp"][:19]: e["timestamp"][19:] for e in raw["events"]}
    assert offsets["2026-01-15T09:30:00"] == "-06:00"
    assert offsets["2026-07-15T09:30:00"] == "-05:00"
    assert offsets["2026-03-08T01:30:00"] == "-06:00"
    assert offsets["2026-03-08T03:30:00"] == "-05:00"
    assert raw["deliveredOn"] == "2026-11-01T13:30:00-06:00"


def test_missing_timezone_footer_drops_timestamps_and_warns_once(caplog):
    html = page(SUMMARY, PROGRESS, footer="")
    html = html.replace("Central Time (USA)", "Elsewhere")
    first = _parse(html)
    _parse(html)
    assert first["events"][0]["timestamp"] is None
    assert caplog.text.count("lacks the expected time zone") == 1


def test_unparseable_timestamp_is_dropped_with_one_warning(caplog):
    html = page(SUMMARY, progress_html([("yesterday-ish", "X", "ARRIVAL SCAN")]))
    assert _parse(html)["events"][0]["timestamp"] is None
    _parse(html)
    assert caplog.text.count("unparseable timestamp") == 1
    assert "yesterday" not in caplog.text


def test_blank_timestamp_is_silently_none(caplog):
    html = page(SUMMARY, progress_html([("", "X", "ARRIVAL SCAN")]))
    assert _parse(html)["events"][0]["timestamp"] is None
    assert "unparseable" not in caplog.text


def test_unexpected_label_warns_once_without_values(caplog):
    html = page(
        summary_html([("Status:", "ARRIVAL SCAN"), ("Tracking Number:", ACTIVE_CODE), ("Weight:", "4 lb")]),
        PROGRESS,
    )
    _parse(html)
    _parse(html)
    assert caplog.text.count("unexpected labels") == 1
    assert "4 lb" not in caplog.text


def test_tracking_number_mismatch_warns_once_without_values(caplog):
    _parse(page(SUMMARY, PROGRESS), "EXAMPLE000000")
    _parse(page(SUMMARY, PROGRESS), "EXAMPLE000000")
    assert caplog.text.count("does not match") == 1
    assert ACTIVE_CODE not in caplog.text
    assert "EXAMPLE000000" not in caplog.text


def test_missing_tracking_number_label_warns(caplog):
    html = page(summary_html([("Status:", "ARRIVAL SCAN")]), PROGRESS)
    assert _parse(html)["trackingNumber"] == ACTIVE_CODE
    assert "does not match" in caplog.text


def test_registered_code_is_always_the_barcode():
    assert _parse(page(SUMMARY, PROGRESS), "EXAMPLE000000")["trackingNumber"] == "EXAMPLE000000"


def test_empty_progress_table_is_valid_history():
    assert _parse(page(SUMMARY, progress_html([])))["events"] == []


def test_blank_progress_rows_are_skipped():
    html = page(SUMMARY, progress_html([("", "", ""), ("01/15/2026 09:30:00 AM", "X", "ARRIVAL SCAN")]))
    assert len(_parse(html)["events"]) == 1


def test_script_and_style_text_is_not_page_text():
    html = page(SUMMARY, PROGRESS, footer="").replace("<p></p>", "")
    html = html.replace("Central Time (USA)", "nothing")
    # the footer phrase only inside <style>/<script> must not count
    html = html.replace("</head>", "<style>/* Central Time (USA) */</style></head>")
    assert _parse(html)["events"][0]["timestamp"] is None


def test_void_and_self_closing_tags_and_implicit_cell_ends():
    html = (
        '<h3>Package Progress</h3><table class="x"><tr><th>Date / Time<th>Location<th>Activity'
        "<tr><td>01/15/2026 09:30 AM<td>X<td>ARRIVAL<br/>SCAN</table>"
    )
    summary = '<table class="u-full-width"><tr><td>Status:<td>Delivered<tr><td>Tracking Number:<td>' + ACTIVE_CODE + "</table>"
    raw = _parse("<img src=x><p>Central Time (USA)</p>" + summary + html)
    assert raw["events"][0]["activity"] == "ARRIVAL SCAN"
    assert raw["events"][0]["timestamp"] == "2026-01-15T09:30:00-06:00"


@pytest.mark.parametrize(
    "html",
    [
        "",
        "<html><body>Sorry, nothing here</body></html>",
        page("", PROGRESS),  # no summary table
        page('<table class="other"><tr><td>Status:</td><td>X</td></tr></table>', PROGRESS),
        page(summary_html([("Tracking Number:", ACTIVE_CODE)]), PROGRESS),  # no Status
        page(summary_html([("Status:", ""), ("Tracking Number:", ACTIVE_CODE)]), PROGRESS),
        page(SUMMARY, ""),  # no progress table
        page(SUMMARY, progress_html([], headers=("Date / Time", "Activity"))),
        page(SUMMARY, progress_html([], headers=())),
        page(SUMMARY, PROGRESS.replace("Package Progress", "Something else")),
        page(SUMMARY, progress_html([("01/15/2026 09:30:00 AM", "X", "ARRIVAL SCAN")]).replace("<td>X</td>", "")),
        page(summary_html([("Status:", "A"), ("Status:", "B")]), PROGRESS),
        page(SUMMARY + "<table><tr><td><table></table></td></tr></table>", PROGRESS),
    ],
)
def test_changed_template_fails_closed(html):
    with pytest.raises(SpeeDeeDeliveryParseError):
        _parse(html)


def test_header_row_missing_in_progress_table_fails_closed():
    html = SUMMARY + '<h3>Package Progress</h3><table class="u-full-width"></table>'
    with pytest.raises(SpeeDeeDeliveryParseError):
        _parse(html)


def test_summary_rows_without_a_label_pair_are_ignored():
    summary = (
        '<table class="u-full-width"><tr><td>just a note</td></tr>'
        "<tr><td></td><td>orphan value</td></tr>"
        f"<tr><td>Status:</td><td>Delivered</td></tr>"
        f"<tr><td>Tracking Number:</td><td>{ACTIVE_CODE}</td></tr></table>"
    )
    assert _parse(page(summary, PROGRESS))["summaryStatus"] == "Delivered"


def test_self_closing_non_void_tag_is_opened_and_closed():
    html = page(SUMMARY, PROGRESS).replace("<h1>", "<div/><h1>")
    assert _parse(html)["summaryStatus"] == "ARRIVAL SCAN"
