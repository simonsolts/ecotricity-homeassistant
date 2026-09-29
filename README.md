<img src="https://raw.githubusercontent.com/simonsolts/ecotricity-homeassistant/main/custom_components/ecotricity/brand/icon.png" alt="Ecotricity for Home Assistant (unofficial)" width="96">

# Ecotricity for Home Assistant

Unofficial Home Assistant integration for [Ecotricity](https://www.ecotricity.co.uk/) (UK).
It reads your smart meter data and tariff from your Ecotricity online account and shows
your electricity usage and cost in the Energy dashboard, with up to 13 months of history.

- **Supported:** single-rate electricity smart meters (credit).
- **Limitation:** the data is **daily only**. Ecotricity's website has no half-hourly
  data. If they add it, I'll try to support it.
- **No official API:** the integration uses the same calls as the Ecotricity website, so
  it can stop working when Ecotricity changes the website. This project is not affiliated
  with or endorsed by Ecotricity.

## Install

You need an Ecotricity online account for a supply address with a smart meter, and Home
Assistant 2026.2 or newer.

**HACS (recommended)**

1. In HACS, open the menu (⋮) and select **Custom repositories**.
2. Add `https://github.com/simonsolts/ecotricity-homeassistant` with the type
   **Integration**.
3. Find **Ecotricity** in HACS and select **Download**.
4. Restart Home Assistant.

**Manual:** copy `custom_components/ecotricity` into the `custom_components` folder of
your Home Assistant configuration, then restart.

## Set up

1. Go to **Settings → Devices & services → Add integration** and select **Ecotricity**.
2. Enter the email address and password of your Ecotricity online account.
3. If your account has more than one supply address, choose one. To add another, add the
   integration again.

If you change your Ecotricity password, Home Assistant asks you for the new one.

## Energy dashboard

1. Go to **Settings → Dashboards → Energy**.
2. Under **Electricity grid → Grid consumption**, select **Add consumption**.
3. For the energy, select the statistic **Ecotricity electricity &lt;your MPAN&gt;**.
   Do not select the *Meter reading* sensor.
4. For the cost, select **Use an entity tracking the total costs** and choose
   **Ecotricity electricity cost &lt;your MPAN&gt;**.

About the cost:

- Cost is usage × unit rate. The standing charge is not included.
- Each day is priced once, at the unit rate when the day is imported. A later rate
  change does not change past days.
- History from before your current tariff has no cost. The kWh history is still there.

## Statistics and sensors

The integration writes two statistics itself:

| Statistic | Unit | Contents |
|---|---|---|
| `ecotricity:electricity_<mpan>` | kWh | Daily usage |
| `ecotricity:electricity_cost_<mpan>` | GBP | Daily usage × unit rate |

Sensors: meter reading (kWh), last daily consumption (kWh), last reading time, unit rate
(GBP/kWh) and standing charge (GBP/d).

**Why statistics and not a sensor?** Ecotricity gets one meter read per day, at midnight.
It arrives on the website at about 02:00 to 03:00. History made from a sensor would put
each day's usage in the wrong hours. So the integration writes the history itself, with
the correct time for each read. For this reason the *Meter reading* sensor has no state
class. The Energy dashboard shows each day's usage and cost in one hour, just before
midnight.

## How it works

The integration logs in to my.ecotricity.co.uk when it starts and when the session
expires. It checks for new data every 3 hours. On first set up it imports up to 13 months
of reads. After that, it fetches the last 14 days on each check (more after a long gap).

## Troubleshooting

- **Debug logs:** add this to `configuration.yaml`:

  ```yaml
  logger:
    logs:
      custom_components.ecotricity: debug
  ```

- **Diagnostics:** on the integration page, select ⋮ → **Download diagnostics**. The
  file has no email address, password, address, account number or MPAN. Attach it when
  you open an issue.

## Privacy

Your email address and password are stored in the Home Assistant configuration, like for
other cloud integrations. The integration sends them only to my.ecotricity.co.uk.

## License

Copyright (C) 2026 Simon Solts

This project uses the
[GNU Affero General Public License v3.0](https://github.com/simonsolts/ecotricity-homeassistant/blob/main/LICENSE),
with one additional term under Section 7(b): you must keep the attribution to the
author. See
[NOTICE](https://github.com/simonsolts/ecotricity-homeassistant/blob/main/NOTICE) for
the details.
