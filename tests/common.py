"""Fake portal responses for tests. All account values are made up."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Any
from urllib.parse import parse_qs

from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
    AiohttpClientMockResponse,
)
from yarl import URL

BASE = "https://my.ecotricity.co.uk"
AURA_RE = re.compile(r"^https://my\.ecotricity\.co\.uk/s/sfsites/aura")
FRONTDOOR_RE = re.compile(r"^https://my\.ecotricity\.co\.uk/secur/frontdoor\.jsp")

USERNAME = "user@example.com"
PASSWORD = "not-a-real-password"
USER_ID_15 = "005Pz00000AbCdE"
CONTACT_ID_18 = "001Pz00000XyZaBIAB"
ACCOUNT_ID = "1234567"
PROPERTY_ID = 654321
ACCOUNT_NUMBER = "200123456"
METER_POINT_ID = 876543
MPAN = "2700001234567"
TOKEN_COOKIE = "__Host-ERIC_PROD1234567890"
TOKEN = "eyJub25jZSI6InRlc3QifQ..signature"
FWUID = "ZmFrZS1md3VpZA"

PREMISE = {
    "IsExportAccount": False,
    "ShowAccount": True,
    "AddressDetail": "1 Test Street London, AB1 2CD",
    "PostCode": "AB1 2CD",
    "AccountStatus": "Active",
    "fromDt": "2026-09-19",
    "AccountNumber": ACCOUNT_NUMBER,
    "AccountId": ACCOUNT_ID,
    "PropertyId": PROPERTY_ID,
}

METER_ITEM = {
    "Segment": "Smart Credit",
    "Name": "MPAN",
    "MPAN": MPAN,
    "type": "Electricity",
    "supplyStatus": "Registered",
    "operationType": "Credit",
    "meterPointServiceType": "DCC",
    "id": METER_POINT_ID,
}

READS = [
    {
        "readingDttm": "2026-09-19T00:00:00.000",
        "cumulative": 29082.8,
        "unit": "kWh",
        "source": "SMR",
        "sequenceType": "First",
        "status": "Accepted",
    },
    {
        "readingDttm": "2026-09-21T00:00:00.000",
        "fromDttm": "2026-09-19T00:00:00.000",
        "consumption": 21.8,
        "cumulative": 29104.6,
        "unit": "kWh",
        "source": "SMR",
        "sequenceType": "Normal",
        "status": "Accepted",
    },
    {
        "readingDttm": "2026-09-22T00:00:00.000",
        "fromDttm": "2026-09-21T00:00:00.000",
        "consumption": 10.5,
        "cumulative": 29115.1,
        "unit": "kWh",
        "source": "SMR",
        "sequenceType": "Normal",
        "status": "Accepted",
    },
]

TARIFF = {
    "tariffName": "EcoFixed Test",
    "tariffCode": "ED_TEST",
    "tariffRate": "21.88",
    "standingChargeRate": "50.49",
    "tariffEndDate": "18 September 2028",
    "subType": "Electricity",
    "meterPoints": [METER_POINT_ID],
}

AGREEMENT = {
    "fromDt": "2026-09-19",
    "toDt": "2028-09-19",
    "products": [{"reference": "ED_TEST", "assets": [{"id": METER_POINT_ID, "identifier": MPAN}]}],
}


def _context(app: str, version: str) -> dict[str, Any]:
    return {"mode": "PROD", "fwuid": FWUID, "app": app, "loaded": {f"APPLICATION@markup://{app}": version}}


LOGIN_HTML = (
    "<html><script>var auraConfig = {"
    f'"context":{json.dumps(_context("siteforce:loginApp2", "1_login"))},'
    '"eikoocnekot":"__Host-ERIC_PRODGUEST"};</script></html>'
)

COMMUNITY_HTML = (
    "<html><script>var auraConfig = {"
    f'"eikoocnekot":"{TOKEN_COOKIE}",'
    f'"context":{json.dumps(_context("siteforce:communityApp", "2_community"))}'
    "};"
    f'$A.storageService.setIsolation("00D8d000004J3nt0DB8d0000004E3c{USER_ID_15}157en_US4e3");'
    "</script>" + " " * 25000 + "</html>"
)

GUEST_REDIRECT_HTML = "<script>window.location.replace('https://my.ecotricity.co.uk/s/login?ec=302');</script>"


def aura_ok(return_value: Any) -> dict[str, Any]:
    """Wrap a value the way Aura returns it."""
    return {"actions": [{"id": "1;a", "state": "SUCCESS", "returnValue": {"returnValue": json.dumps(return_value)}}]}


def ip(result: Any) -> dict[str, Any]:
    """Wrap an Integration Procedure result."""
    return aura_ok({"IPResult": result, "error": "OK"})


def procedure_name(data: Any) -> tuple[str, dict[str, Any], dict[str, str]]:
    """Return the method name, input and form fields of an Aura request."""
    form = {k: v[0] for k, v in parse_qs(data).items()} if isinstance(data, str) else dict(data)
    action = json.loads(form["message"])["actions"][0]
    params = action["params"]["params"]
    return params["sMethodName"], json.loads(params["input"]), form


class FakePortal:
    """Serve a fake portal through Home Assistant's aiohttp mocker."""

    def __init__(self, mock: AiohttpClientMocker) -> None:
        self.mock = mock
        self.calls: list[tuple[str, dict[str, Any], dict[str, str]]] = []
        self.login_ok = True
        self.session_expired_once: set[str] = set()
        self.handlers: dict[str, Callable[[dict[str, Any]], Any]] = {
            "ECO_IP_GetUserDetails": lambda _: ip({"userAccountId": CONTACT_ID_18, "userType": "CspLitePortal"}),
            "ECO_IP_Get360Premises": lambda _: ip([PREMISE]),
            "ECO_IP_GetMeterPoints": lambda _: ip({"RegisteredMetersResponseItems": [METER_ITEM]}),
            "ECO_IP_GetMeterReading": lambda _: ip({"result": "", "results": json.dumps(READS)}),
            "ECO_IP_GetTariffDetails": lambda _: ip({"tarrifDetails": [TARIFF]}),
            "ECO_IP_GetAccountAgreements": lambda _: ip({"results": [AGREEMENT], "httpCode": 200}),
        }
        mock.get(f"{BASE}/s/login/", text=LOGIN_HTML)
        mock.get(FRONTDOOR_RE, text="ok")
        mock.get(f"{BASE}/s/", text=COMMUNITY_HTML, cookies={TOKEN_COOKIE: TOKEN})
        mock.post(AURA_RE, side_effect=self._aura)

    async def _aura(self, method: str, url: URL, data: Any) -> AiohttpClientMockResponse:
        name, input_, form = procedure_name(data)
        self.calls.append((name, input_, form))
        if name == "userLogin":
            if not self.login_ok:
                return self._json(url, aura_ok({"Status": False, "error": "OK"}))
            return self._json(url, aura_ok({"Status": True, "PageRef": f"{BASE}/secur/frontdoor.jsp?sid=fake"}))
        if name in self.session_expired_once:
            self.session_expired_once.discard(name)
            return self._json(url, {"event": {"descriptor": "markup://aura:invalidSession"}})
        return self._json(url, self.handlers[name](input_))

    @staticmethod
    def _json(url: URL, payload: Any) -> AiohttpClientMockResponse:
        return AiohttpClientMockResponse("POST", url, json=payload)

    def names(self) -> list[str]:
        return [c[0] for c in self.calls]
