"""Tests for the portal client."""

from __future__ import annotations

import asyncio
from datetime import date, datetime

import pytest
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.ecotricity.api import (
    USER_AGENT,
    EcotricityAuthError,
    EcotricityClient,
    MeterPoint,
    find_aura_context,
    find_user_id,
    to_18_char_id,
)

from .common import (
    ACCOUNT_ID,
    COMMUNITY_HTML,
    FWUID,
    LOGIN_HTML,
    METER_POINT_ID,
    MPAN,
    PASSWORD,
    PROPERTY_ID,
    TOKEN,
    USERNAME,
    FakePortal,
)


def test_to_18_char_id() -> None:
    # Values from Salesforce's documented algorithm.
    assert to_18_char_id("005Pz00000TlNq5") == "005Pz00000TlNq5IAF"
    assert to_18_char_id("001Pz00001EbSEX") == "001Pz00001EbSEXIA3"
    assert to_18_char_id("005Pz00000TlNq5IAF") == "005Pz00000TlNq5IAF"
    with pytest.raises(ValueError):
        to_18_char_id("short")


def test_find_aura_context() -> None:
    ctx = find_aura_context(LOGIN_HTML)
    assert ctx.app == "siteforce:loginApp2"
    assert ctx.fwuid == FWUID
    assert ctx.loaded == {"APPLICATION@markup://siteforce:loginApp2": "1_login"}


def test_find_user_id_skips_trailing_values() -> None:
    # The isolation string has more values after the user ID.
    assert find_user_id(COMMUNITY_HTML) == to_18_char_id("005Pz00000AbCdE")


@pytest.fixture
def portal(aioclient_mock: AiohttpClientMocker) -> FakePortal:
    return FakePortal(aioclient_mock)


@pytest.fixture
async def client(portal: FakePortal, aioclient_mock: AiohttpClientMocker):
    session = aioclient_mock.create_session(asyncio.get_running_loop())
    yield EcotricityClient(session, USERNAME, PASSWORD)
    await session.close()


async def test_login_and_premises(portal: FakePortal, client: EcotricityClient) -> None:
    premises = await client.get_premises()

    assert [p.account_id for p in premises] == [ACCOUNT_ID]
    assert premises[0].property_id == PROPERTY_ID
    assert premises[0].is_active

    login = portal.calls[0]
    assert login[0] == "userLogin"
    assert login[1]["UserName"] == USERNAME
    assert login[2]["aura.token"] == "null"
    assert login[2]["aura.pageURI"] == "/s/login/"

    user = portal.calls[1]
    assert user[0] == "ECO_IP_GetUserDetails"
    assert user[1] == {"Id": to_18_char_id("005Pz00000AbCdE")}
    assert user[2]["aura.token"] == TOKEN
    assert '"app":"siteforce:communityApp"' in user[2]["aura.context"]

    # Get360Premises takes the 15-character contact ID.
    assert portal.calls[2][1] == {"accountId": "001Pz00000XyZaB"}


async def test_login_failure(portal: FakePortal, client: EcotricityClient) -> None:
    portal.login_ok = False
    with pytest.raises(EcotricityAuthError):
        await client.login()


async def test_meter_points_readings_and_tariffs(portal: FakePortal, client: EcotricityClient) -> None:
    points = await client.get_meter_points(ACCOUNT_ID, PROPERTY_ID)
    assert points == [MeterPoint(id=str(METER_POINT_ID), mpan=MPAN, fuel="Electricity", segment="Smart Credit")]

    reads = await client.get_meter_readings(ACCOUNT_ID, points[0], date(2026, 9, 19), date(2026, 9, 22))
    assert [r.reading_at for r in reads] == [
        datetime(2026, 9, 19),
        datetime(2026, 9, 21),
        datetime(2026, 9, 22),
    ]
    assert reads[0].consumption is None
    assert reads[1].consumption == 21.8
    assert reads[2].cumulative == 29115.1

    name, input_, _ = portal.calls[-1]
    assert name == "ECO_IP_GetMeterReading"
    assert input_["fromDate"] == "2026-09-19"
    assert input_["toDate"] == "2026-09-22"
    assert input_["meterPointId"] == str(METER_POINT_ID)
    assert input_["juniferAccountId"] == ACCOUNT_ID

    tariffs = await client.get_tariffs(ACCOUNT_ID)
    assert tariffs[0].unit_rate == 21.88
    assert tariffs[0].standing_charge == 50.49


async def test_relogin_after_session_expiry(portal: FakePortal, client: EcotricityClient) -> None:
    await client.login()
    portal.session_expired_once.add("ECO_IP_GetTariffDetails")

    tariffs = await client.get_tariffs(ACCOUNT_ID)

    assert len(tariffs) == 1
    assert portal.names().count("userLogin") == 2


async def test_every_request_sends_a_browser_user_agent(
    portal: FakePortal, client: EcotricityClient, aioclient_mock: AiohttpClientMocker
) -> None:
    # The portal serves a page without the token cookie setting to non-browser clients.
    await client.get_premises()
    assert aioclient_mock.mock_calls
    for _method, url, _data, headers in aioclient_mock.mock_calls:
        assert headers["User-Agent"] == USER_AGENT, url
