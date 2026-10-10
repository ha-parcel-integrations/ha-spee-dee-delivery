# Spee-Dee Delivery Parcel Tracker

[![Release](https://img.shields.io/github/v/release/ha-parcel-integrations/ha-spee-dee-delivery.svg)](https://github.com/ha-parcel-integrations/ha-spee-dee-delivery/releases)
[![Downloads](https://img.shields.io/github/downloads/ha-parcel-integrations/ha-spee-dee-delivery/total.svg)](https://github.com/ha-parcel-integrations/ha-spee-dee-delivery/releases)
[![HACS](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/integration)
[![License](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

> 💬 Questions or feedback? Join the discussion on the [Home Assistant community](https://community.home-assistant.io/t/packages-postnl-dhl-nl-dpd-and-gls-parcel-integration/112433/).

A custom Home Assistant integration that tracks your [Spee-Dee Delivery](https://speedeedelivery.com) parcels (US Midwest). No account is needed — you enter the tracking code yourself, just like on the Spee-Dee Delivery website.

> **Pre-1.0.** Spee-Dee Delivery publishes no documented API, and only a handful of its statuses have been seen so far. A status that is not recognised shows as `unknown` and one warning is logged; if the tracking page changes shape, the last known data is kept and one warning is logged. Warnings name the kind of problem, never the values. Please [open an issue](https://github.com/ha-parcel-integrations/ha-spee-dee-delivery/issues/new?template=unrecognised_status.yml) when you see one.

Part of the [ha-parcel-integrations](https://ha-parcel-integrations.github.io/) family: it publishes the same canonical parcel format, statuses and events as the other carrier integrations, so it plugs straight into the [Parcel Aggregator](https://github.com/ha-parcel-integrations/ha-parcel-aggregator) and cross-carrier automations.

## Contents

- [Features](#features)
- [Requirements](#requirements)
- [Installation](#installation)
- [Configuration](#configuration)
- [Options](#options)
- [Removal](#removal)
- [Sensors](#sensors)
- [Parcel status reference](#parcel-status-reference)
- [Events](#events)
- [Services](#services)
- [Examples](#examples)
- [Debugging](#debugging)
- [Troubleshooting](#troubleshooting)
- [Related integrations](#related-integrations)
- [Disclaimer](#disclaimer)
- [Contributing](#contributing)
- [License](#license)

## Features

- Track any number of Spee-Dee Delivery parcels by tracking code — no account needed
- Per-parcel sensor with the canonical status (`registered` / `in_transit` / `out_for_delivery` / `delivered` / …), the carrier's own status text and a tracking deep-link
- Summary sensors: incoming parcels, next delivery, recently delivered parcels
- `spee_dee_delivery.track_parcel` / `spee_dee_delivery.untrack_parcel` services, so a dashboard button can add a parcel
- Events + device triggers for no-code automations (parcel registered, status changed, delivered)
- Opt-in per-parcel status history
- Manual refresh button and a diagnostic last-update sensor

## Requirements

- Home Assistant 2024.12 or newer
- A parcel sent to you by Spee-Dee Delivery and its tracking code — no
  account needed. Spee-Dee Delivery's tracking page is meant for shipments
  tendered by or for you, so only add codes of your own shipments

## Installation

### HACS (recommended)

1. In HACS, choose the three-dot menu → **Custom repositories**.
2. Add `https://github.com/ha-parcel-integrations/ha-spee-dee-delivery` as an **Integration**.
3. Install **Spee-Dee Delivery** and restart Home Assistant.

### Manual

Copy `custom_components/spee_dee_delivery` into your `config/custom_components/` folder and restart Home Assistant.

## Configuration

Add the integration via **Settings → Devices & Services → Add Integration → Spee-Dee Delivery**. There is nothing to fill in: the hub is created immediately (Spee-Dee Delivery tracking needs no account).

Then add parcels via the integration's **Configure** dialog, the [`spee_dee_delivery.track_parcel`](#services) service, or a [dashboard button](examples/dashboards/add_parcel_card.yaml). Enter the tracking code exactly as it was given to you; it is not changed in any way except for trimming surrounding spaces. Codes are 6 to 40 characters.

## Options

Open **Configure** on the integration entry:

| Section | Option | Default | Description |
|---|---|---|---|
| Parcels | Add / remove | — | Manage the tracked tracking codes. Changes apply immediately, no restart. |
| Delivered parcels | Filter by / amount | last 7 days | How long delivered parcels stay visible on the delivered sensor. |
| Parcel history | Include status history | off | Adds a `history` attribute per parcel with each status update. |

Polling isn't one of these settings: the integration polls on a dynamic,
status-driven schedule with nothing to configure.

## Dynamic polling

Polling isn't a setting here — the integration adjusts its own cadence to
what your tracked parcels are actually doing:

- **Quiet hours** — no polling between 00:00–06:00 local time, aside from one
  catch-up check at each end of that window (around midnight and around 6
  AM), so an overnight update is never missed.
- **Hot (every 15 minutes)** — while any tracked parcel is out for delivery
  today, starting an hour before its delivery window opens (or immediately if
  no window is known yet).
- **Normal (every 45 minutes)** — for anything else still on its way.
- **Fully paused** — once every tracked parcel has been delivered, or nothing
  is tracked at all, polling stops until you add a parcel back (adding one
  always triggers an immediate check, regardless of the pause).
- A small, fixed per-hub offset is added on top, so not every Spee-Dee Delivery
  hub out there polls at exactly the same second.

Spee-Dee Delivery publishes no delivery window, so a parcel that is out for delivery is always in the 15-minute tier until it is delivered. The page's times are US Central time and are converted correctly across daylight saving changes.

## Removal

Standard HA removal applies: **Settings → Devices & Services → Spee-Dee Delivery → ⋮ → Delete**. Nothing is stored on Spee-Dee Delivery's side.

## Sensors

| Entity | Description |
|---|---|
| `sensor.spee_dee_delivery_incoming_parcels` | Number of active tracked parcels, full list under the `parcels` attribute |
| `sensor.spee_dee_delivery_parcel_<code>` | One per tracked parcel; state is the canonical status, attributes carry the full normalised parcel |
| `sensor.spee_dee_delivery_next_delivery` | Earliest expected delivery moment across all active parcels |
| `sensor.spee_dee_delivery_delivered_parcels` | Recently delivered parcels (see the retention option) |
| `sensor.spee_dee_delivery_last_successful_update` | Diagnostic: when Spee-Dee Delivery was last polled successfully |

A delivered parcel moves from its per-parcel sensor to the delivered sensor automatically.

## Parcel status reference

The `status` field is the carrier-agnostic enum shared by the whole integration family:

| Status | Meaning |
|---|---|
| `in_transit` | Scanned on arrival, in transit between facilities, or unloaded at a Spee-Dee Delivery facility |
| `out_for_delivery` | With the courier today |
| `delivered` | Delivered |
| `problem` | A misrouted-parcel scan was recorded; the current status can return to `in_transit` while the history keeps the incident |
| `unknown` | A status we have not mapped yet |

`registered`, `at_pickup_point` and `returning` never occur: Spee-Dee Delivery's tracking page shows none of them.

The carrier's own human-readable text is available as `raw_status`, without the
per-scan code some scans end in (`OUT FOR DELIVERY (22045)` reads
`OUT FOR DELIVERY`), since that code may say where your parcel is.

## Events

The integration fires these on the event bus (also available as device triggers on the Spee-Dee Delivery device):

| Event | When |
|---|---|
| `spee_dee_delivery_parcel_registered` | A new parcel appears in the active list |
| `spee_dee_delivery_parcel_status_changed` | A parcel's canonical status changes (`old_status` / `new_status` in the payload), except the final hop to delivered |
| `spee_dee_delivery_parcel_delivered` | A parcel is delivered |
| `spee_dee_delivery_parcel_delivery_time_changed` | The expected delivery window changes |

Every payload is the full normalised parcel plus the hub's `device_id`. Events are suppressed on the first refresh after start-up.

## Services

| Service | Fields | Description |
|---|---|---|
| `spee_dee_delivery.track_parcel` | `tracking_code` | Start tracking a parcel |
| `spee_dee_delivery.untrack_parcel` | `tracking_code` | Stop tracking a parcel |

## Examples

Ready-to-paste automations and dashboard snippets live in [`examples/`](examples/), including tracking a new parcel straight from a dashboard.

### Community Lovelace cards

Third-party cards that work with this integration's sensors:

- [jonisnet/hki-parcels-card](https://github.com/jonisnet/hki-parcels-card)
- [klaptafel/ha-package-tracker-card](https://github.com/klaptafel/ha-package-tracker-card)

## Debugging

```yaml
logger:
  logs:
    custom_components.spee_dee_delivery: debug
```

## Troubleshooting

- **A parcel shows `unknown`** — the code is wrong, or Spee-Dee Delivery has not published it yet. If the page cannot be read, the last known data is kept.
- **A status logs "Unrecognised Spee-Dee Delivery status"** — please [open an issue](https://github.com/ha-parcel-integrations/ha-spee-dee-delivery/issues/new?template=unrecognised_status.yml) with the logged line so the mapping can be extended.

## Related integrations

This integration is part of [**ha-parcel-integrations**](https://ha-parcel-integrations.github.io/) — a family of
parcel-carrier integrations that all publish the same canonical parcel format,
statuses and events.

- [**Parcel Aggregator**](https://github.com/ha-parcel-integrations/ha-parcel-aggregator) rolls every installed carrier
  up into one set of sensors.
- Browse [the organisation](https://ha-parcel-integrations.github.io/) for the current list of supported carriers.

## Disclaimer

This is an independent, community-built project. It is not affiliated with, endorsed by, sponsored by, or supported by Spee-Dee Delivery, Home Assistant, or any other third party referenced in this project. Please don't contact Spee-Dee Delivery for support with this integration.

All third-party trademarks, trade names, product names, logos, and other brand assets are the property of their respective owners. References to them are solely to identify the relevant carrier or service and do not imply affiliation, sponsorship, or endorsement. Nothing in this project grants or implies any licence or right to use third-party brand assets.

This integration may rely on public, unofficial, or undocumented carrier interfaces, accessed with your own account or API key where required. These may change or be withdrawn without notice and may be subject to Spee-Dee Delivery's terms. Data is sent only to Spee-Dee Delivery's own services or those of its group; this project operates no servers of its own. You are responsible for ensuring that your use complies with applicable law and those terms. Use is at your own risk; see the [licence](LICENSE) for warranty limitations.

This integration reads only the public tracking page of the codes you register yourself, one request per code and never in bulk. It does not look up codes you did not supply.

This integration uses the same public tracking endpoint as the Spee-Dee Delivery consumer website.

## Contributing

Pull requests and issues are welcome. Please open an issue before
submitting a large change.

## License

[MIT](LICENSE)
