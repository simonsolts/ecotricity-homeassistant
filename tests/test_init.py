"""Tests for setup, sensors and the statistics import."""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed
from pytest_homeassistant_custom_component.components.recorder.common import async_wait_recording_done
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.ecotricity.const import BACKFILL_DAYS, DOMAIN, REFRESH_DAYS, UPDATE_INTERVAL
from custom_components.ecotricity.diagnostics import async_get_config_entry_diagnostics

from .common import ACCOUNT_ID, MPAN, READS, FakePortal, ip
from .test_config_flow import ENTRY_DATA

STATISTIC_ID = f"ecotricity:electricity_{MPAN}"
LONDON = dt_util.get_time_zone("Europe/London")


@pytest.fixture
def portal(aioclient_mock: AiohttpClientMocker) -> FakePortal:
    return FakePortal(aioclient_mock)


@pytest.fixture
def entry(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, unique_id=ACCOUNT_ID, data=ENTRY_DATA, title="1 Test Street")
    entry.add_to_hass(hass)
    return entry


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def _statistics(hass: HomeAssistant) -> list[dict]:
    await async_wait_recording_done(hass)
    result = await hass.async_add_executor_job(
        statistics_during_period,
        hass,
        datetime(2026, 1, 1, tzinfo=dt_util.UTC),
        None,
        {STATISTIC_ID},
        "hour",
        None,
        {"state", "sum"},
    )
    return result.get(STATISTIC_ID, [])


@pytest.mark.freeze_time("2026-09-28T09:00:00+01:00")
async def test_setup_creates_sensors(hass: HomeAssistant, portal: FakePortal, entry: MockConfigEntry) -> None:
    await _setup(hass, entry)
    assert entry.state is ConfigEntryState.LOADED

    reading = hass.states.get("sensor.1_test_street_meter_reading")
    assert reading is not None
    assert float(reading.state) == 29115.1
    assert reading.attributes["unit_of_measurement"] == "kWh"
    assert "state_class" not in reading.attributes
    assert reading.attributes["statistic_id"] == STATISTIC_ID

    assert float(hass.states.get("sensor.1_test_street_last_daily_consumption").state) == 10.5
    assert hass.states.get("sensor.1_test_street_last_reading_time").state == "2026-09-21T23:00:00+00:00"

    unit_rate = hass.states.get("sensor.1_test_street_unit_rate")
    assert float(unit_rate.state) == 0.2188
    assert unit_rate.attributes["unit_of_measurement"] == "GBP/kWh"
    assert unit_rate.attributes["tariff_name"] == "EcoFixed Test"
    assert float(hass.states.get("sensor.1_test_street_standing_charge").state) == 0.5049

    registry = er.async_get(hass)
    assert registry.async_get("sensor.1_test_street_unit_rate").unique_id == f"{ACCOUNT_ID}_electricity_unit_rate"


@pytest.mark.freeze_time("2026-09-28T09:00:00+01:00")
async def test_backfill_then_refresh_window(
    hass: HomeAssistant, portal: FakePortal, entry: MockConfigEntry, freezer: FrozenDateTimeFactory
) -> None:
    await _setup(hass, entry)
    first = [c[1] for c in portal.calls if c[0] == "ECO_IP_GetMeterReading"]
    assert first[0]["fromDate"] == (datetime(2026, 9, 28) - timedelta(days=BACKFILL_DAYS)).date().isoformat()
    assert first[0]["toDate"] == "2026-09-28"

    await _statistics(hass)
    freezer.tick(UPDATE_INTERVAL + timedelta(minutes=1))
    async_fire_time_changed(hass)
    # Scheduled refreshes run as background tasks.
    await hass.async_block_till_done(wait_background_tasks=True)

    reads = [c[1] for c in portal.calls if c[0] == "ECO_IP_GetMeterReading"]
    assert len(reads) == 2
    assert reads[1]["fromDate"] == (datetime(2026, 9, 28) - timedelta(days=REFRESH_DAYS)).date().isoformat()


@pytest.mark.freeze_time("2026-09-28T09:00:00+01:00")
async def test_statistics_import(hass: HomeAssistant, portal: FakePortal, entry: MockConfigEntry) -> None:
    await _setup(hass, entry)
    stats = await _statistics(hass)

    # One point per read, in the hour that ends at the read time (UK midnight).
    starts = [dt_util.utc_from_timestamp(s["start"]).astimezone(LONDON) for s in stats]
    assert [s.isoformat() for s in starts] == [
        "2026-09-18T23:00:00+01:00",
        "2026-09-20T23:00:00+01:00",
        "2026-09-21T23:00:00+01:00",
    ]
    assert [s["state"] for s in stats] == [29082.8, 29104.6, 29115.1]
    # The sum starts at 0, so the Energy dashboard does not see a huge first day.
    assert [s["sum"] for s in stats] == [0.0, pytest.approx(21.8), pytest.approx(32.3)]


@pytest.mark.freeze_time("2026-09-28T09:00:00+01:00")
async def test_statistics_continue_after_restart_window(
    hass: HomeAssistant, portal: FakePortal, entry: MockConfigEntry, freezer: FrozenDateTimeFactory
) -> None:
    """A later update with only new reads keeps the same offset."""
    await _setup(hass, entry)
    await _statistics(hass)

    newer = {
        "readingDttm": "2026-09-23T00:00:00.000",
        "fromDttm": "2026-09-22T00:00:00.000",
        "consumption": 7.3,
        "cumulative": 29122.4,
        "source": "SMR",
        "status": "Accepted",
    }
    portal.handlers["ECO_IP_GetMeterReading"] = lambda _: ip({"results": json.dumps([READS[2], newer])})
    freezer.tick(UPDATE_INTERVAL + timedelta(minutes=1))
    async_fire_time_changed(hass)
    # Scheduled refreshes run as background tasks.
    await hass.async_block_till_done(wait_background_tasks=True)

    stats = await _statistics(hass)
    assert [s["sum"] for s in stats] == [0.0, pytest.approx(21.8), pytest.approx(32.3), pytest.approx(39.6)]


async def test_auth_failure_starts_reauth(hass: HomeAssistant, portal: FakePortal, entry: MockConfigEntry) -> None:
    portal.login_ok = False
    await _setup(hass, entry)

    assert entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert [f["context"]["source"] for f in flows] == [SOURCE_REAUTH]


async def test_portal_error_retries(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, entry) -> None:
    aioclient_mock.get("https://my.ecotricity.co.uk/s/login/", exc=TimeoutError)
    await _setup(hass, entry)
    assert entry.state is ConfigEntryState.SETUP_RETRY


async def test_unload(hass: HomeAssistant, portal: FakePortal, entry: MockConfigEntry) -> None:
    await _setup(hass, entry)
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED


@pytest.mark.freeze_time("2026-09-28T09:00:00+01:00")
async def test_diagnostics_redact_personal_data(
    hass: HomeAssistant, portal: FakePortal, entry: MockConfigEntry
) -> None:
    await _setup(hass, entry)
    diag = await async_get_config_entry_diagnostics(hass, entry)

    text = json.dumps(diag, default=str)
    for secret in (ACCOUNT_ID, MPAN, "user@example.com", "not-a-real-password", "1 Test Street", "654321"):
        assert secret not in text
    assert diag["meters"][0]["reading_count"] == 3
    assert diag["tariffs"][0]["unit_rate"] == 21.88
