"""Tests for the config flow."""

from __future__ import annotations

from collections.abc import Generator
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import SOURCE_USER
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.ecotricity.const import CONF_ACCOUNT_ID, CONF_ACCOUNT_NUMBER, CONF_PROPERTY_ID, DOMAIN

from .common import ACCOUNT_ID, ACCOUNT_NUMBER, PASSWORD, PREMISE, PROPERTY_ID, USERNAME, FakePortal, ip

ENTRY_DATA = {
    CONF_USERNAME: USERNAME,
    CONF_PASSWORD: PASSWORD,
    CONF_ACCOUNT_ID: ACCOUNT_ID,
    CONF_PROPERTY_ID: PROPERTY_ID,
    CONF_ACCOUNT_NUMBER: ACCOUNT_NUMBER,
}


@pytest.fixture(autouse=True)
def mock_setup_entry() -> Generator[AsyncMock]:
    with patch("custom_components.ecotricity.async_setup_entry", return_value=True) as mock:
        yield mock


@pytest.fixture
def portal(aioclient_mock: AiohttpClientMocker) -> FakePortal:
    return FakePortal(aioclient_mock)


async def _start(hass: HomeAssistant):
    return await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})


async def test_single_premise(hass: HomeAssistant, portal: FakePortal) -> None:
    result = await _start(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_USERNAME: f" {USERNAME} ", CONF_PASSWORD: PASSWORD}
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == PREMISE["AddressDetail"]
    assert result["data"] == ENTRY_DATA
    assert result["result"].unique_id == ACCOUNT_ID


async def test_multiple_premises(hass: HomeAssistant, portal: FakePortal) -> None:
    other = {**PREMISE, "AccountId": "7654321", "PropertyId": 111, "AddressDetail": "2 Other Road"}
    closed = {**PREMISE, "AccountId": "999", "AccountStatus": "Closed"}
    portal.handlers["ECO_IP_Get360Premises"] = lambda _: ip([PREMISE, other, closed])

    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_USERNAME: USERNAME, CONF_PASSWORD: PASSWORD}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "premise"
    options = result["data_schema"].schema["premise"].config["options"]
    assert [o["value"] for o in options] == [ACCOUNT_ID, "7654321"]

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"premise": "7654321"})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "2 Other Road"
    assert result["data"][CONF_PROPERTY_ID] == 111


async def test_invalid_auth_then_recover(hass: HomeAssistant, portal: FakePortal) -> None:
    portal.login_ok = False
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_USERNAME: USERNAME, CONF_PASSWORD: "wrong"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}

    portal.login_ok = True
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_USERNAME: USERNAME, CONF_PASSWORD: PASSWORD}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_cannot_connect(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    aioclient_mock.get("https://my.ecotricity.co.uk/s/login/", exc=TimeoutError)
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_USERNAME: USERNAME, CONF_PASSWORD: PASSWORD}
    )
    assert result["errors"] == {"base": "cannot_connect"}


async def test_unexpected_response(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    aioclient_mock.get("https://my.ecotricity.co.uk/s/login/", text="<html>maintenance</html>")
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_USERNAME: USERNAME, CONF_PASSWORD: PASSWORD}
    )
    assert result["errors"] == {"base": "unknown"}


async def test_no_active_premises(hass: HomeAssistant, portal: FakePortal) -> None:
    portal.handlers["ECO_IP_Get360Premises"] = lambda _: ip([{**PREMISE, "AccountStatus": "Closed"}])
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_USERNAME: USERNAME, CONF_PASSWORD: PASSWORD}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_premises"


async def test_already_configured(hass: HomeAssistant, portal: FakePortal) -> None:
    MockConfigEntry(domain=DOMAIN, unique_id=ACCOUNT_ID, data=ENTRY_DATA).add_to_hass(hass)
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_USERNAME: USERNAME, CONF_PASSWORD: PASSWORD}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_reauth(hass: HomeAssistant, portal: FakePortal) -> None:
    entry = MockConfigEntry(domain=DOMAIN, unique_id=ACCOUNT_ID, data={**ENTRY_DATA, CONF_PASSWORD: "old"})
    entry.add_to_hass(hass)

    result = await entry.start_reauth_flow(hass)
    assert result["step_id"] == "reauth_confirm"
    assert result["description_placeholders"]["username"] == USERNAME

    portal.login_ok = False
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_PASSWORD: "still-wrong"})
    assert result["errors"] == {"base": "invalid_auth"}

    portal.login_ok = True
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_PASSWORD: "new"})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_PASSWORD] == "new"


async def test_reauth_wrong_account(hass: HomeAssistant, portal: FakePortal) -> None:
    entry = MockConfigEntry(domain=DOMAIN, unique_id="other", data={**ENTRY_DATA, CONF_ACCOUNT_ID: "other"})
    entry.add_to_hass(hass)

    result = await entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_PASSWORD: PASSWORD})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "wrong_account"
