"""Client for the Ecotricity customer portal (my.ecotricity.co.uk).

Ecotricity has no public API. The portal is a Salesforce Experience Cloud site.
All data comes from Salesforce Aura calls that run OmniStudio Integration
Procedures. This client logs in like the browser does and calls the same
procedures.
"""

from __future__ import annotations

import json
import logging
import re
import urllib.parse
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

import aiohttp

_LOGGER = logging.getLogger(__name__)

BASE_URL = "https://my.ecotricity.co.uk"
AURA_URL = f"{BASE_URL}/s/sfsites/aura"
IP_CLASS = "vlocity_cmt.IntegrationProcedureService"
TIMEOUT = aiohttp.ClientTimeout(total=60)

_CONTEXT_RE = re.compile(r'"context"\s*:\s*\{')
_BOOTSTRAP_RE = re.compile(r"/s/sfsites/l/([^/\"']+)/(?:bootstrap|app)\.js")
_TOKEN_COOKIE_RE = re.compile(r'"eikoocnekot"\s*:\s*"([^"]+)"')
_USER_ID_RE = re.compile(r'setIsolation\("00D[A-Za-z0-9]{12}0DB[A-Za-z0-9]{12}(005[A-Za-z0-9]{12})')
_AURA_GUARD_RE = re.compile(r"^\s*(while\(1\);|\)\]\}'|/\*-secure-)\s*")


class EcotricityError(Exception):
    """Base error."""


class EcotricityAuthError(EcotricityError):
    """The username or password is wrong."""


class EcotricitySessionError(EcotricityError):
    """The session is not valid (for example, it expired)."""


class EcotricityApiError(EcotricityError):
    """The portal returned something we did not expect."""


@dataclass(frozen=True)
class AuraContext:
    """Values that identify the Aura framework and app version."""

    mode: str
    fwuid: str
    app: str
    loaded: dict[str, str]

    def to_json(self) -> str:
        return json.dumps(
            {
                "mode": self.mode,
                "fwuid": self.fwuid,
                "app": self.app,
                "loaded": self.loaded,
                "dn": [],
                "globals": {},
                "uad": True,
            },
            separators=(",", ":"),
        )


@dataclass(frozen=True)
class Premise:
    """A supply address with its Junifer IDs."""

    account_id: str
    property_id: int
    account_number: str
    address: str
    status: str

    @property
    def is_active(self) -> bool:
        return self.status == "Active"


@dataclass(frozen=True)
class MeterPoint:
    """A registered meter point."""

    id: str
    mpan: str
    fuel: str
    segment: str


@dataclass(frozen=True)
class MeterReading:
    """One meter read. `cumulative` is the register value in kWh."""

    reading_at: datetime
    from_at: datetime | None
    cumulative: float
    consumption: float | None
    source: str
    status: str
    unit: str


@dataclass(frozen=True)
class Tariff:
    """Current tariff. Rates are in pence."""

    name: str
    code: str
    unit_rate: float
    standing_charge: float
    end_date: str
    fuel: str


def find_aura_context(html: str) -> AuraContext:
    """Find the `auraConfig.context` object in a portal page."""
    decoder = json.JSONDecoder()
    for match in _CONTEXT_RE.finditer(html):
        try:
            obj, _ = decoder.raw_decode(html, match.end() - 1)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and obj.get("fwuid") and obj.get("app") and obj.get("loaded"):
            return AuraContext(obj.get("mode", "PROD"), obj["fwuid"], obj["app"], obj["loaded"])
    for match in _BOOTSTRAP_RE.finditer(html):
        try:
            obj = json.loads(urllib.parse.unquote(match.group(1)))
        except json.JSONDecodeError:
            continue
        if obj.get("fwuid") and obj.get("app") and obj.get("loaded"):
            return AuraContext(obj.get("mode", "PROD"), obj["fwuid"], obj["app"], obj["loaded"])
    raise EcotricityApiError("Could not find the Aura context in the page")


def to_18_char_id(sf_id: str) -> str:
    """Add the Salesforce case-safe suffix to a 15-character record ID."""
    if len(sf_id) == 18:
        return sf_id
    if len(sf_id) != 15:
        raise ValueError(f"Not a Salesforce ID: length {len(sf_id)}")
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZ012345"
    suffix = ""
    for chunk in (sf_id[0:5], sf_id[5:10], sf_id[10:15]):
        bits = sum(1 << i for i, ch in enumerate(chunk) if "A" <= ch <= "Z")
        suffix += alphabet[bits]
    return sf_id + suffix


def find_user_id(html: str) -> str:
    """Read the logged-in user ID from `$A.storageService.setIsolation(...)`."""
    match = _USER_ID_RE.search(html)
    if not match:
        raise EcotricityApiError("Could not find the user ID in the page")
    return to_18_char_id(match.group(1))


def _parse_dttm(value: str | None) -> datetime | None:
    """Parse a Junifer time such as 2026-09-21T00:00:00.000. It has no time zone."""
    if not value:
        return None
    return datetime.fromisoformat(value)


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


class EcotricityClient:
    """Async client. Pass an `aiohttp.ClientSession` with its own cookie jar."""

    def __init__(self, session: aiohttp.ClientSession, username: str, password: str) -> None:
        self._session = session
        self._username = username
        self._password = password
        self._context: AuraContext | None = None
        self._token = "null"
        self._page_uri = "/s/login/"
        self._counter = 0
        self._user_id: str | None = None

    @property
    def logged_in(self) -> bool:
        return self._user_id is not None

    # -- Transport ---------------------------------------------------------------

    async def _get(self, url: str) -> tuple[str, str, dict[str, str]]:
        """GET a page. Return the final URL, the body and the cookies the response set."""
        async with self._session.get(url, timeout=TIMEOUT) as resp:
            if resp.status >= 400:
                raise EcotricityApiError(f"GET {urllib.parse.urlsplit(url).path}: HTTP {resp.status}")
            cookies = {name: morsel.value for name, morsel in resp.cookies.items()}
            return str(resp.url), await resp.text(), cookies

    async def _generic_invoke(
        self,
        class_name: str,
        method_name: str,
        input_: dict[str, Any],
        options: dict[str, Any] | None = None,
    ) -> Any:
        """Call `GenericInvoke2NoCont` and return the unwrapped result."""
        if self._context is None:
            raise EcotricitySessionError("No Aura context")
        if options is None:
            options = {
                "useFuture": False,
                "preTransformBundle": "",
                "postTransformBundle": "",
                "chainable": False,
                "useQueueableApexRemoting": False,
                "ignoreCache": False,
                "vlcClass": class_name,
                "useContinuation": False,
            }
        self._counter += 1
        action = {
            "id": f"{self._counter};a",
            "descriptor": "aura://ApexActionController/ACTION$execute",
            "callingDescriptor": "UNKNOWN",
            "params": {
                "namespace": "vlocity_cmt",
                "classname": "BusinessProcessDisplayController",
                "method": "GenericInvoke2NoCont",
                "params": {
                    "input": json.dumps(input_, separators=(",", ":")),
                    "options": json.dumps(options, separators=(",", ":")),
                    "sClassName": class_name,
                    "sMethodName": method_name,
                },
                "cacheable": False,
                "isContinuation": False,
            },
        }
        data = {
            "message": json.dumps({"actions": [action]}, separators=(",", ":")),
            "aura.context": self._context.to_json(),
            "aura.pageURI": self._page_uri,
            "aura.token": self._token,
        }
        async with self._session.post(
            AURA_URL,
            params={"r": str(self._counter), "aura.ApexAction.execute": "1"},
            data=data,
            headers={
                "X-SFDC-LDS-Endpoints": (
                    "ApexActionController.execute:BusinessProcessDisplayController.GenericInvoke2NoCont"
                ),
                "Origin": BASE_URL,
                "Referer": BASE_URL + self._page_uri,
            },
            allow_redirects=False,
            timeout=TIMEOUT,
        ) as resp:
            text = await resp.text()
            status = resp.status
        if status in (301, 302, 401, 403):
            raise EcotricitySessionError(f"{method_name}: HTTP {status}")
        if status != 200:
            raise EcotricityApiError(f"{method_name}: HTTP {status}")
        text = _AURA_GUARD_RE.sub("", text)
        text = re.sub(r"\*/\s*$", "", text)
        try:
            outer = json.loads(text)
        except json.JSONDecodeError as err:
            raise EcotricityApiError(f"{method_name}: response is not JSON") from err
        if "exceptionEvent" in outer or "event" in outer:
            descriptor = json.dumps(outer.get("event", outer.get("exceptionEvent")))[:200]
            if "invalidSession" in descriptor or "clientOutOfSync" in descriptor:
                raise EcotricitySessionError(f"{method_name}: {descriptor}")
            raise EcotricityApiError(f"{method_name}: Aura event {descriptor}")
        try:
            act = outer["actions"][0]
        except (KeyError, IndexError, TypeError) as err:
            raise EcotricityApiError(f"{method_name}: no action in response") from err
        if act.get("state") != "SUCCESS":
            raise EcotricityApiError(f"{method_name}: state {act.get('state')}")
        rv = act.get("returnValue", {}).get("returnValue")
        inner = json.loads(rv) if isinstance(rv, str) else rv
        if isinstance(inner, dict) and inner.get("error") not in (None, "OK"):
            raise EcotricityApiError(f"{method_name}: error {inner.get('error')!r}")
        return inner

    async def _procedure(self, name: str, input_: dict[str, Any]) -> Any:
        inner = await self._generic_invoke(IP_CLASS, name, input_)
        return inner.get("IPResult", inner) if isinstance(inner, dict) else inner

    async def _call(self, name: str, input_: dict[str, Any]) -> Any:
        """Call a procedure. Log in first if needed, and once more if the session expired."""
        if not self.logged_in:
            await self.login()
        try:
            return await self._procedure(name, input_)
        except EcotricitySessionError:
            _LOGGER.debug("Session expired during %s; logging in again", name)
            await self.login()
            return await self._procedure(name, input_)

    # -- Login -------------------------------------------------------------------

    async def login(self) -> None:
        """Log in and prepare the Aura context, token and user ID."""
        self._session.cookie_jar.clear()
        self._user_id = None
        self._token = "null"
        self._page_uri = "/s/login/"
        _, html, _ = await self._get(f"{BASE_URL}/s/login/")
        self._context = find_aura_context(html)

        result = await self._generic_invoke(
            "ECO_LoginRemoteAction",
            "userLogin",
            {"UserName": self._username, "RedirectUrl": "/s/selectaddress", "Password": self._password},
            options={
                "preTransformBundle": "",
                "postTransformBundle": "",
                "useQueueableApexRemoting": False,
                "ignoreCache": False,
                "vlcClass": "ECO_LoginRemoteAction",
                "useContinuation": False,
            },
        )
        if not isinstance(result, dict) or not result.get("Status") or not result.get("PageRef"):
            raise EcotricityAuthError("Login failed")

        # frontdoor.jsp sets the session cookies. Its script redirect is not needed.
        await self._get(result["PageRef"])

        url, html, cookies = await self._get(f"{BASE_URL}/s/")
        if "/s/login" in url or len(html) < 20000:
            raise EcotricitySessionError("Login did not create a session")
        self._context = find_aura_context(html)
        self._page_uri = "/s/"
        match = _TOKEN_COOKIE_RE.search(html)
        if not match:
            raise EcotricityApiError("Could not find the Aura token cookie name")
        token = cookies.get(match.group(1))
        if not token:
            morsel = self._session.cookie_jar.filter_cookies(BASE_URL).get(match.group(1))
            token = morsel.value if morsel is not None else None
        if not token:
            raise EcotricityApiError("The Aura token cookie was not set")
        self._token = token
        self._user_id = find_user_id(html)
        _LOGGER.debug("Logged in to the Ecotricity portal")

    # -- Data --------------------------------------------------------------------

    async def get_premises(self) -> list[Premise]:
        """Return the supply addresses of the account."""
        if not self.logged_in:
            await self.login()
        user = await self._call("ECO_IP_GetUserDetails", {"Id": self._user_id})
        user = user[0] if isinstance(user, list) and len(user) == 1 else user
        if not isinstance(user, dict) or not user.get("userAccountId"):
            raise EcotricityApiError("ECO_IP_GetUserDetails returned no account")
        items = await self._call("ECO_IP_Get360Premises", {"accountId": user["userAccountId"][:15]})
        premises = []
        for item in _as_list(items):
            if not isinstance(item, dict) or "AccountId" not in item:
                continue
            premises.append(
                Premise(
                    account_id=str(item["AccountId"]),
                    property_id=int(item["PropertyId"]),
                    account_number=str(item.get("AccountNumber", "")),
                    address=str(item.get("AddressDetail", "")),
                    status=str(item.get("AccountStatus", "")),
                )
            )
        return premises

    async def get_meter_points(self, account_id: str, property_id: int) -> list[MeterPoint]:
        """Return the registered meter points of a premise."""
        result = await self._call("ECO_IP_GetMeterPoints", {"PropId": property_id, "AccountId": account_id})
        points = []
        for item in _as_list(result.get("RegisteredMetersResponseItems") if isinstance(result, dict) else None):
            points.append(
                MeterPoint(
                    id=str(item["id"]),
                    mpan=str(item.get("MPAN") or item.get("MPRN") or ""),
                    fuel=str(item.get("type", "")),
                    segment=str(item.get("Segment", "")),
                )
            )
        return points

    async def get_meter_readings(
        self, account_id: str, meter_point: MeterPoint, from_date: date, to_date: date
    ) -> list[MeterReading]:
        """Return meter reads between two dates (both included), oldest first."""
        result = await self._call(
            "ECO_IP_GetMeterReading",
            {
                "toDate": to_date.isoformat(),
                "meterType": meter_point.fuel,
                "meterPointId": meter_point.id,
                "juniferAccountId": account_id,
                "fromDate": from_date.isoformat(),
                "IsGreaterThirteenMonths": "13",
            },
        )
        raw = result.get("results") if isinstance(result, dict) else None
        if not raw:
            return []
        items = json.loads(raw) if isinstance(raw, str) else raw
        readings = []
        for item in _as_list(items):
            reading_at = _parse_dttm(item.get("readingDttm"))
            if reading_at is None or item.get("cumulative") is None:
                continue
            readings.append(
                MeterReading(
                    reading_at=reading_at,
                    from_at=_parse_dttm(item.get("fromDttm")),
                    cumulative=float(item["cumulative"]),
                    consumption=None if item.get("consumption") is None else float(item["consumption"]),
                    source=str(item.get("source", "")),
                    status=str(item.get("status", "")),
                    unit=str(item.get("unit", "kWh")),
                )
            )
        readings.sort(key=lambda r: r.reading_at)
        return readings

    async def get_tariffs(self, account_id: str) -> list[Tariff]:
        """Return the current tariffs of the account."""
        result = await self._call("ECO_IP_GetTariffDetails", {"isExportAccount": False, "accountId": account_id})
        tariffs = []
        # The server spells the key "tarrifDetails".
        for item in _as_list(result.get("tarrifDetails") if isinstance(result, dict) else None):
            try:
                tariffs.append(
                    Tariff(
                        name=str(item.get("tariffName", "")),
                        code=str(item.get("tariffCode", "")),
                        unit_rate=float(item["tariffRate"]),
                        standing_charge=float(item["standingChargeRate"]),
                        end_date=str(item.get("tariffEndDate", "")),
                        fuel=str(item.get("subType", "")),
                    )
                )
            except (KeyError, TypeError, ValueError):
                _LOGGER.debug("Skipping a tariff with missing rates")
        return tariffs
