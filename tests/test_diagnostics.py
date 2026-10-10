"""Tests for Spee-Dee Delivery diagnostics."""
from datetime import timedelta
from unittest.mock import MagicMock

from custom_components.spee_dee_delivery.diagnostics import (
    async_get_config_entry_diagnostics,
)


async def test_diagnostics_redacts_and_counts(hass):
    """Diagnostics get pasted into public issues — nothing identifying may survive."""
    entry = MagicMock()
    entry.options = {"parcels": [{"tracking_code": "EXAMPLE123456"}]}
    entry.runtime_data.coordinator.current_tier_minutes = 15
    entry.runtime_data.coordinator.update_interval = timedelta(minutes=15)
    entry.runtime_data.coordinator.data = [
        {
            "barcode": "EXAMPLE123456",
            "sender": "Example Shop",
            "receiver": "Jane Doe",
            "status": "out_for_delivery",
            "raw": {
                "trackingNumber": "EXAMPLE123456",
                "recipient": "Jane Doe",
                "deliveryAddress": {"city": "Rotterdam", "street": "Coolsingel 1"},
                "summaryStatus": "OUT FOR DELIVERY (Route 12)",
                "signedBy": "Pat Example",
                "deliveredTo": "1 Invented Way Zed Sample, KS",
                "events": [
                    {
                        "timestamp": "2026-04-29T08:46:00-05:00",
                        "location": "Fakeville Depot",
                        "activity": "ARRIVAL SCAN",
                    }
                ],
            },
            "history": [{"timestamp": "2026-04-29T08:46:00-05:00", "raw_status": "ARRIVAL SCAN"}],
        }
    ]
    entry.runtime_data.coordinator.delivered = []
    entry.runtime_data.coordinator.delivered_codes = set()

    result = await async_get_config_entry_diagnostics(hass, entry)

    assert result["counts"] == {
        "incoming_active": 1,
        "delivered": 0,
        "skipped_from_fetch": 0,
    }
    assert result["polling"] == {
        "tier_minutes": 15,
        "update_interval_seconds": 900.0,
        "suspended": False,
    }
    # tracking codes and payload PII are redacted, at every nesting level
    assert result["entry_options"]["parcels"][0]["tracking_code"] == "**REDACTED**"
    assert result["incoming"][0]["barcode"] == "**REDACTED**"
    assert result["incoming"][0]["receiver"] == "**REDACTED**"
    assert result["incoming"][0]["raw"]["recipient"] == "**REDACTED**"
    assert result["incoming"][0]["raw"]["deliveryAddress"] == "**REDACTED**"
    # every summary and progress value is redacted
    assert result["incoming"][0]["raw"]["summaryStatus"] == "**REDACTED**"
    assert result["incoming"][0]["raw"]["events"] == "**REDACTED**"
    # ``raw`` keeps the signature, destination and per-scan locations, so this
    # is the only thing standing between them and a pasted public issue
    assert result["incoming"][0]["raw"]["signedBy"] == "**REDACTED**"
    assert result["incoming"][0]["raw"]["deliveredTo"] == "**REDACTED**"
    assert result["incoming"][0]["history"] == "**REDACTED**"
    # non-identifying fields survive, or the diagnostics would be useless
    assert result["incoming"][0]["status"] == "out_for_delivery"


async def test_diagnostics_reports_suspended_polling(hass):
    """update_interval None (Section 2.1's full stop) must be visible, not just absent."""
    entry = MagicMock()
    entry.options = {"parcels": []}
    entry.runtime_data.coordinator.current_tier_minutes = None
    entry.runtime_data.coordinator.update_interval = None
    entry.runtime_data.coordinator.data = []
    entry.runtime_data.coordinator.delivered = []

    result = await async_get_config_entry_diagnostics(hass, entry)

    assert result["polling"] == {
        "tier_minutes": None,
        "update_interval_seconds": None,
        "suspended": True,
    }
