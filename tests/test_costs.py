"""Tests for the cost statistic."""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.recorder.models import StatisticData, StatisticMeanType, StatisticMetaData
from homeassistant.components.recorder.statistics import async_add_external_statistics, statistics_during_period
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed
from pytest_homeassistant_custom_component.components.recorder.common import async_wait_recording_done
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.ecotricity.const import DOMAIN, UPDATE_INTERVAL

from .common import ACCOUNT_ID, AGREEMENT, MPAN, READS, TARIFF, FakePortal, ip
from .test_config_flow import ENTRY_DATA

ENERGY_ID = f"ecotricity:electricity_{MPAN}"
COST_ID = f"ecotricity:electricity_cost_{MPAN}"
LONDON = dt_util.get_time_zone("Europe/London")
RATE = 0.2188  # GBP/kWh, from TARIFF

pytestmark = pytest.mark.freeze_time("2026-09-28T09:00:00+01:00")


@pytest.fixture
def portal(aioclient_mock: AiohttpClientMocker) -> FakePortal:
    return FakePortal(aioclient_mock)


@pytest.fixture
def entry(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, unique_id=ACCOUNT_ID, data=ENTRY_DATA, title="1 Test Street")
    entry.add_to_hass(hass)
    return entry


async def _stats(hass: HomeAssistant, statistic_id: str) -> list[dict]:
    await async_wait_recording_done(hass)
    result = await hass.async_add_executor_job(
        statistics_during_period,
        hass,
        datetime(2026, 1, 1, tzinfo=dt_util.UTC),
        None,
        {statistic_id},
        "hour",
        None,
        {"state", "sum"},
    )
    return result.get(statistic_id, [])


def _days(stats: list[dict]) -> list[str]:
    return [dt_util.utc_from_timestamp(s["start"]).astimezone(LONDON).isoformat() for s in stats]


async def _refresh(hass: HomeAssistant, freezer: FrozenDateTimeFactory) -> None:
    freezer.tick(UPDATE_INTERVAL + timedelta(minutes=1))
    async_fire_time_changed(hass)
    await hass.async_block_till_done(wait_background_tasks=True)


async def test_cost_statistic(hass: HomeAssistant, portal: FakePortal, entry: MockConfigEntry) -> None:
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    stats = await _stats(hass, COST_ID)
    # Same hours as the energy statistic.
    assert _days(stats) == _days(await _stats(hass, ENERGY_ID))
    assert [s["sum"] for s in stats] == [
        0.0,
        pytest.approx(21.8 * RATE, abs=1e-4),
        pytest.approx((21.8 + 10.5) * RATE, abs=1e-4),
    ]
    # The state is the cost of each read.
    assert [s["state"] for s in stats] == [
        0.0,
        pytest.approx(21.8 * RATE, abs=1e-4),
        pytest.approx(10.5 * RATE, abs=1e-4),
    ]

    agreements = [c[1] for c in portal.calls if c[0] == "ECO_IP_GetAccountAgreements"]
    assert agreements == [{"IsDualOffer": False, "currentaggrement": ["ED_TEST"], "juniferAccountId": ACCOUNT_ID}]

    meter = hass.states.get("sensor.1_test_street_meter_reading")
    assert meter.attributes["cost_statistic_id"] == COST_ID


async def test_reads_before_the_agreement_are_not_priced(
    hass: HomeAssistant, portal: FakePortal, entry: MockConfigEntry
) -> None:
    portal.handlers["ECO_IP_GetAccountAgreements"] = lambda _: ip({"results": [{**AGREEMENT, "fromDt": "2026-09-21"}]})
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    stats = await _stats(hass, COST_ID)
    # The read at the agreement start is the starting point.
    assert _days(stats) == ["2026-09-20T23:00:00+01:00", "2026-09-21T23:00:00+01:00"]
    assert [s["sum"] for s in stats] == [0.0, pytest.approx(10.5 * RATE, abs=1e-4)]


async def test_rate_change_does_not_rewrite_past_days(
    hass: HomeAssistant, portal: FakePortal, entry: MockConfigEntry, freezer: FrozenDateTimeFactory
) -> None:
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    await _stats(hass, COST_ID)

    newer = {
        "readingDttm": "2026-09-23T00:00:00.000",
        "fromDttm": "2026-09-22T00:00:00.000",
        "consumption": 7.3,
        "cumulative": 29122.4,
        "source": "SMR",
        "status": "Accepted",
    }
    portal.handlers["ECO_IP_GetMeterReading"] = lambda _: ip({"results": json.dumps([*READS, newer])})
    portal.handlers["ECO_IP_GetTariffDetails"] = lambda _: ip({"tarrifDetails": [{**TARIFF, "tariffRate": "30.00"}]})
    await _refresh(hass, freezer)

    stats = await _stats(hass, COST_ID)
    before = (21.8 + 10.5) * RATE
    assert [s["sum"] for s in stats] == [
        0.0,
        pytest.approx(21.8 * RATE, abs=1e-4),
        pytest.approx(before, abs=1e-4),
        pytest.approx(before + 7.3 * 0.30, abs=1e-3),
    ]


async def test_new_cost_statistic_backfills_from_the_agreement_start(
    hass: HomeAssistant, portal: FakePortal, entry: MockConfigEntry
) -> None:
    """Upgrade from a version without costs: the energy statistic exists, the cost one does not."""
    metadata = StatisticMetaData(
        mean_type=StatisticMeanType.NONE,
        has_sum=True,
        name="old",
        source=DOMAIN,
        statistic_id=ENERGY_ID,
        unit_class="energy",
        unit_of_measurement="kWh",
    )
    start = datetime(2026, 9, 21, 23, tzinfo=LONDON)
    async_add_external_statistics(hass, metadata, [StatisticData(start=start, state=29115.1, sum=32.3)])
    await async_wait_recording_done(hass)
    # An agreement that started before the normal 14-day window.
    portal.handlers["ECO_IP_GetAccountAgreements"] = lambda _: ip({"results": [{**AGREEMENT, "fromDt": "2026-09-01"}]})

    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    reads = [c[1] for c in portal.calls if c[0] == "ECO_IP_GetMeterReading"]
    # The window reaches back to the agreement start, not only 14 days (2026-09-14).
    assert reads[0]["fromDate"] == "2026-09-01"
    assert len(await _stats(hass, COST_ID)) == 3


async def test_no_cost_without_agreement_data(hass: HomeAssistant, portal: FakePortal, entry: MockConfigEntry) -> None:
    portal.handlers["ECO_IP_GetAccountAgreements"] = lambda _: {"actions": [{"state": "ERROR"}]}
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state.value == "loaded"
    assert await _stats(hass, COST_ID) == []
    assert len(await _stats(hass, ENERGY_ID)) == 3
