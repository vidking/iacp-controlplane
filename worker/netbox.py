"""Cliente NetBox que conserva las reservas ante resultados inciertos.

El cliente reserva una dirección exacta del rango autorizado y conserva un
marcador por instancia para reconciliar resultados inciertos.
"""

from __future__ import annotations

import json
import os
import re
import socket
import ssl
import time
from dataclasses import dataclass, field
from ipaddress import IPv4Address, IPv4Network, ip_address, ip_network
from typing import TypeAlias, TypeGuard
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, build_opener, HTTPSHandler

from api.catalog import PROD_STATIC_FIRST_HOST, PROD_STATIC_LAST_HOST, PROD_STATIC_PREFIX


JsonObject: TypeAlias = dict[str, object]


def _is_json_object(value: object) -> TypeGuard[JsonObject]:
    return isinstance(value, dict) and all(isinstance(key, str) for key in value)


def _response_int(value: object) -> int:
    if isinstance(value, (int, float, str)):
        return int(value)
    raise TypeError("El valor de respuesta no es un entero convertible.")


class NetBoxError(RuntimeError):
    def __init__(
        self,
        code: str,
        status: int | None = None,
        *,
        uncertain: bool = False,
        detail: str = "",
    ):
        super().__init__(code)
        self.code = code
        self.status = status
        self.uncertain = uncertain
        self.detail = detail[:256]


@dataclass(frozen=True)
class NetBoxSettings:
    base_url: str
    token: str = field(repr=False)
    expected_prefix: str = PROD_STATIC_PREFIX
    prefix_length: int = 24
    gateway_host: int = 1
    allocation_tag: str = "ocupada"
    gateway_tag: str = "reservada"
    timeout: float = 15.0
    attempts: int = 3
    verify_tls: bool = True
    ca_file: str | None = None
    excluded_addresses: frozenset[IPv4Address] = field(default_factory=frozenset)


@dataclass(frozen=True)
class NetBoxAllocation:
    ip_id: int
    address: str
    prefix: str
    gateway: str
    marker: str
    profile: str = "prod_static"


def _bool_env(name: str, default: bool = True) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() not in {"0", "false", "no", "off"}


def settings() -> NetBoxSettings:
    base_url = (os.getenv("IACP_NETBOX_URL") or "").strip().rstrip("/")
    token = (os.getenv("IACP_NETBOX_TOKEN") or "").strip()
    if not base_url or not token:
        raise NetBoxError("IPAM_NOT_CONFIGURED")
    try:
        prefix_length = int(os.getenv("IACP_NETBOX_PREFIX_LENGTH", "24"))
        gateway_host = int(os.getenv("IACP_NETBOX_GATEWAY_HOST", "1"))
        timeout = max(1.0, float(os.getenv("IACP_NETBOX_TIMEOUT", "15")))
        attempts = max(1, int(os.getenv("IACP_NETBOX_RETRY_ATTEMPTS", "3")))
    except ValueError as error:
        raise NetBoxError("IPAM_CONFIGURATION_INVALID") from error
    if prefix_length != 24 or gateway_host != 1:
        raise NetBoxError("IPAM_CONFIGURATION_INVALID")
    # Accept both the API root and the status URL shown by NetBox's UI. The
    # client always talks to the API root; `/api/status/` is only a health
    # endpoint and must not become part of resource paths.
    if base_url.endswith("/status"):
        base_url = base_url[: -len("/status")].rstrip("/")
    excluded_raw = os.getenv("IACP_NETBOX_EXCLUDED_ADDRESSES", "").strip()
    if not excluded_raw and os.getenv("IACP_RUNTIME_STAGE", "").strip().lower() == "develop":
        excluded_raw = "190.0.231.2,190.0.231.3"
    try:
        excluded_addresses = frozenset(
            ip_address(value.strip())
            for value in excluded_raw.split(",")
            if value.strip()
        )
    except ValueError as error:
        raise NetBoxError("IPAM_CONFIGURATION_INVALID") from error
    expected_network = ip_network(PROD_STATIC_PREFIX)
    if any(
        not isinstance(address, IPv4Address) or address not in expected_network
        for address in excluded_addresses
    ):
        raise NetBoxError("IPAM_CONFIGURATION_INVALID")
    return NetBoxSettings(
        base_url=base_url if base_url.endswith("/api") else f"{base_url}/api",
        token=token,
        prefix_length=prefix_length,
        gateway_host=gateway_host,
        allocation_tag=(
            os.getenv("IACP_NETBOX_ALLOCATION_TAG", "ocupada").strip() or "ocupada"
        ),
        gateway_tag=(
            os.getenv("IACP_NETBOX_GATEWAY_TAG", "reservada").strip() or "reservada"
        ),
        timeout=timeout,
        attempts=attempts,
        verify_tls=_bool_env("IACP_NETBOX_VERIFY_TLS", True),
        ca_file=(os.getenv("IACP_NETBOX_CA_FILE") or "").strip() or None,
        excluded_addresses=excluded_addresses,
    )


def configured() -> bool:
    return bool(
        (os.getenv("IACP_NETBOX_URL") or "").strip()
        and (os.getenv("IACP_NETBOX_TOKEN") or "").strip()
    )


def _context(config: NetBoxSettings) -> ssl.SSLContext:
    if not config.verify_tls:
        # Explicitly opt-in only for local diagnostics. Production examples
        # keep verification enabled and should provide IACP_NETBOX_CA_FILE.
        return ssl._create_unverified_context()
    return ssl.create_default_context(cafile=config.ca_file)


def _error_code(status: int | None) -> str:
    if status in {401, 403}:
        return "IPAM_PERMISSION_DENIED"
    if status == 404:
        return "IPAM_NOT_FOUND"
    if status in {400, 409}:
        return "IPAM_ADDRESS_CONFLICT"
    if status is not None and 500 <= status < 600:
        return "IPAM_UNAVAILABLE"
    return "IPAM_REQUEST_FAILED"


class NetBoxClient:
    def __init__(self, config: NetBoxSettings | None = None):
        self.config = config or settings()
        self.opener = build_opener(HTTPSHandler(context=_context(self.config)))

    def _request(
        self,
        method: str,
        path: str,
        payload: object | None = None,
        *,
        retry_get: bool = False,
    ) -> object:
        body = (
            None
            if payload is None
            else json.dumps(payload, separators=(",", ":")).encode()
        )
        headers = {
            "Authorization": f"Bearer {self.config.token}",
            "Accept": "application/json",
        }
        if body is not None:
            headers["Content-Type"] = "application/json"
        request = Request(
            f"{self.config.base_url}{path}", data=body, headers=headers, method=method
        )
        for attempt in range(self.config.attempts):
            try:
                with self.opener.open(request, timeout=self.config.timeout) as response:
                    raw = response.read()
                if not raw:
                    return None
                return json.loads(raw)
            except HTTPError as error:
                status = error.code
                error.close()
                if (
                    retry_get
                    and 500 <= status < 600
                    and attempt + 1 < self.config.attempts
                ):
                    time.sleep(min(4.0, 0.5 * (2**attempt)))
                    continue
                raise NetBoxError(
                    _error_code(status),
                    status,
                    uncertain=method != "GET" and status >= 500,
                ) from None
            except (URLError, TimeoutError, socket.timeout, ValueError):
                if retry_get and attempt + 1 < self.config.attempts:
                    time.sleep(min(4.0, 0.5 * (2**attempt)))
                    continue
                raise NetBoxError(
                    "IPAM_UNAVAILABLE" if method == "GET" else "IPAM_REQUEST_UNCERTAIN",
                    uncertain=method != "GET",
                ) from None
        raise NetBoxError("IPAM_UNAVAILABLE")

    def _tag_id(self, slug: str) -> int:
        query = urlencode({"slug": slug, "limit": 2, "brief": "true"})
        response = self._request("GET", f"/extras/tags/?{query}", retry_get=True)
        rows = response.get("results", []) if _is_json_object(response) else []
        if not isinstance(rows, list) or len(rows) != 1:
            raise NetBoxError("IPAM_TAG_NOT_FOUND")
        row = rows[0]
        if not _is_json_object(row) or row.get("id") is None:
            raise NetBoxError("IPAM_TAG_NOT_FOUND")
        return _response_int(row["id"])

    @staticmethod
    def _address_value(value: object) -> tuple[IPv4Address, int] | None:
        raw = str(value or "")
        try:
            if "/" not in raw:
                return None
            address, length = raw.split("/", 1)
            parsed = ip_address(address)
            return (parsed, int(length)) if isinstance(parsed, IPv4Address) else None
        except (ValueError, TypeError):
            return None

    def _ip_records(self, parent: IPv4Network) -> list[JsonObject]:
        offset = 0
        records: list[JsonObject] = []
        while True:
            query = urlencode(
                {"parent": str(parent), "ordering": "id", "limit": 100, "offset": offset}
            )
            response = self._request("GET", f"/ipam/ip-addresses/?{query}", retry_get=True)
            if not _is_json_object(response):
                raise NetBoxError("IPAM_RESPONSE_INVALID")
            rows = response.get("results")
            if not isinstance(rows, list):
                raise NetBoxError("IPAM_RESPONSE_INVALID")
            for row in rows:
                if not _is_json_object(row):
                    raise NetBoxError("IPAM_RESPONSE_INVALID")
                if row.get("vrf") is not None:
                    continue
                value = self._address_value(row.get("address"))
                if value is None or value[0] not in parent:
                    raise NetBoxError("IPAM_RESPONSE_INVALID")
                records.append(row)
            if not response.get("next"):
                return records
            if not rows:
                raise NetBoxError("IPAM_RESPONSE_INVALID")
            offset += len(rows)

    @staticmethod
    def _in_pool(address: IPv4Address, prefix: IPv4Network) -> bool:
        return (
            prefix.network_address + PROD_STATIC_FIRST_HOST
            <= address
            <= prefix.network_address + PROD_STATIC_LAST_HOST
        )

    def available_addresses(self, prefix: IPv4Network) -> list[tuple[IPv4Address, int]]:
        if str(prefix) != self.config.expected_prefix:
            raise NetBoxError("IPAM_PREFIX_INVALID")
        occupied: set[IPv4Address] = set()
        for row in self._ip_records(prefix):
            value = self._address_value(row.get("address"))
            if value is None:
                raise NetBoxError("IPAM_RESPONSE_INVALID")
            occupied.add(value[0])
        return [
            (address, prefix.prefixlen)
            for address in prefix.hosts()
            if self._in_pool(address, prefix)
            and address not in occupied
            and address not in self.config.excluded_addresses
        ]

    def get_ip(self, address: str) -> JsonObject | None:
        query = urlencode({"address": address, "limit": 2})
        response = self._request("GET", f"/ipam/ip-addresses/?{query}", retry_get=True)
        rows = response.get("results", []) if _is_json_object(response) else []
        if not isinstance(rows, list) or len(rows) != 1:
            return None
        record = rows[0]
        if _is_json_object(record) and record.get("address") == address:
            return record
        return None

    @staticmethod
    def _marker_matches(record: JsonObject | None, marker: str) -> bool:
        if not record:
            return False
        text = " ".join(
            str(record.get(key) or "") for key in ("description", "comments")
        )
        return (
            re.search(r"(?:^|[\s;|])" + re.escape(marker) + r"(?:$|[\s;|])", text)
            is not None
        )

    def _is_reserved(self, record: JsonObject) -> bool:
        tags = record.get("tags")
        tag_slugs: set[str] = set()
        if isinstance(tags, list):
            tag_slugs = {
                str(tag.get("slug") or tag.get("name"))
                for tag in tags
                if _is_json_object(tag)
            }
        status = record.get("status")
        status_value = status.get("value") if _is_json_object(status) else status
        return self.config.gateway_tag in tag_slugs or status_value == "reserved"

    def validate_gateway(self, prefix: IPv4Network) -> str:
        gateway = prefix.network_address + self.config.gateway_host
        address = f"{gateway}/{self.config.prefix_length}"
        record = self.get_ip(address)
        if record is None or record.get("vrf") is not None:
            raise NetBoxError(
                "IPAM_GATEWAY_NOT_FOUND",
                detail=f"No se encontró la dirección del gateway {address} en NetBox.",
            )
        if not self._is_reserved(record):
            raise NetBoxError("IPAM_GATEWAY_NOT_RESERVED")
        return str(gateway)

    def ensure_identity_tag(self, slug: str) -> int:
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,62}", slug):
            raise NetBoxError("IPAM_TAG_INVALID")
        try:
            return self._tag_id(slug)
        except NetBoxError as error:
            if error.code != "IPAM_TAG_NOT_FOUND":
                raise
        try:
            result = self._request(
                "POST", "/extras/tags/", {"name": slug, "slug": slug}
            )
            if not _is_json_object(result):
                raise NetBoxError("IPAM_RESPONSE_INVALID")
            return _response_int(result["id"])
        except NetBoxError as error:
            if error.code not in {"IPAM_ADDRESS_CONFLICT", "IPAM_REQUEST_UNCERTAIN"}:
                raise
            return self._tag_id(slug)

    def owned_allocation(self, prefix: IPv4Network, marker: str) -> JsonObject | None:
        # Recuperar una reserva cuyo POST terminó antes de persistirla localmente.
        matches = [
            row for row in self._ip_records(prefix) if self._marker_matches(row, marker)
        ]
        if len(matches) > 1:
            raise NetBoxError("IPAM_ALLOCATION_CONFLICT", uncertain=True)
        return matches[0] if matches else None

    def _confirm_allocation(
        self,
        record: object,
        prefix: IPv4Network,
        gateway: str,
        marker: str,
        expected_address: str | None = None,
    ) -> NetBoxAllocation:
        if not _is_json_object(record):
            raise NetBoxError("IPAM_RESPONSE_INVALID", uncertain=True)
        address = record.get("address")
        if not isinstance(address, str):
            raise NetBoxError("IPAM_RESPONSE_INVALID", uncertain=True)
        value = self._address_value(address)
        ip_id = record.get("id")
        if (
            type(ip_id) is not int
            or ip_id <= 0
            or value is None
            or value[1] != prefix.prefixlen
            or not self._in_pool(value[0], prefix)
            or (expected_address is not None and address != expected_address)
            or record.get("vrf") is not None
            or self._is_reserved(record)
            or not self._marker_matches(record, marker)
        ):
            raise NetBoxError("IPAM_RESPONSE_INVALID", uncertain=True)
        try:
            parent = ip_network(f"{value[0]}/32")
            if not isinstance(parent, IPv4Network):
                raise NetBoxError("IPAM_RESPONSE_INVALID", uncertain=True)
            existing = self._ip_records(parent)
        except NetBoxError as error:
            raise NetBoxError("IPAM_REQUEST_UNCERTAIN", uncertain=True) from error
        if (
            len(existing) != 1
            or existing[0].get("id") != ip_id
            or existing[0].get("address") != address
            or self._is_reserved(existing[0])
            or not self._marker_matches(existing[0], marker)
        ):
            raise NetBoxError("IPAM_ALLOCATION_CONFLICT", uncertain=True)
        return NetBoxAllocation(ip_id, address, str(prefix), gateway, marker)

    def allocate(self, **kwargs) -> NetBoxAllocation:
        # Serializa los aprovisionadores IACP del mismo prefijo durante la llamada remota.
        from api.db import database

        with database() as db:
            db.execute("BEGIN")
            db.execute(
                "SELECT pg_advisory_xact_lock(105, hashtext(?))",
                (self.config.expected_prefix,),
            )
            try:
                result = self._allocate(**kwargs)
                db.execute("COMMIT")
                return result
            except Exception:
                db.execute("ROLLBACK")
                raise

    def _allocate(
        self,
        *,
        instance_id: str,
        operation_id: str,
        vmid: int,
        name: str,
        customer_tag: str | None = None,
        tags: list[str] | None = None,
    ) -> NetBoxAllocation:
        parsed_prefix = ip_network(self.config.expected_prefix)
        if not isinstance(parsed_prefix, IPv4Network):
            raise NetBoxError("IPAM_PREFIX_INVALID")
        prefix = parsed_prefix
        gateway = self.validate_gateway(prefix)
        tag_id = self._tag_id(self.config.allocation_tag)
        marker = f"iacp-instance={instance_id}"
        identity_tags = list(
            dict.fromkeys(([customer_tag] if customer_tag else []) + (tags or []))
        )
        tag_ids = list(
            dict.fromkeys(
                [tag_id] + [self.ensure_identity_tag(tag) for tag in identity_tags]
            )
        )
        owned = self.owned_allocation(prefix, marker)
        if owned:
            allocation = self._confirm_allocation(owned, prefix, gateway, marker)
            existing_tags: list[int] = []
            existing_tag_values = owned.get("tags", [])
            if isinstance(existing_tag_values, list):
                existing_tags = [
                    _response_int(tag["id"])
                    for tag in existing_tag_values
                    if _is_json_object(tag) and "id" in tag
                ]
            try:
                self._request(
                    "PATCH",
                    f"/ipam/ip-addresses/{allocation.ip_id}/",
                    {"tags": list(dict.fromkeys(existing_tags + tag_ids))},
                )
            except NetBoxError as error:
                raise NetBoxError(error.code, error.status, uncertain=True) from error
            return allocation
        description = (
            f"IACP {customer_tag or 'customer'} | {name} | vmid:{vmid} | {marker}"[:200]
        )
        comments = (
            f"{marker}; operation={operation_id}; customer_tag={customer_tag or ''}"
        )
        payload = {
            "status": "active",
            "description": description,
            "comments": comments,
            "tags": tag_ids,
            "vrf": None,
        }
        candidates = self.available_addresses(prefix)
        attempted = set()
        while candidates:
            candidate = candidates[0]
            attempted.add(candidate)
            address = f"{candidate[0]}/{candidate[1]}"
            try:
                record = self._request(
                    "POST", "/ipam/ip-addresses/", {**payload, "address": address}
                )
            except NetBoxError as error:
                if error.uncertain:
                    # Resolver por propietario antes de considerar otra escritura.
                    try:
                        record = self.owned_allocation(prefix, marker)
                    except NetBoxError as lookup_error:
                        raise error from lookup_error
                    if record is None:
                        raise
                elif error.status in {400, 409}:
                    available = self.available_addresses(prefix)
                    if candidate in available:
                        raise
                    candidates = [value for value in available if value not in attempted]
                    continue
                else:
                    raise
            return self._confirm_allocation(record, prefix, gateway, marker, address)
        raise NetBoxError("IPAM_POOL_EXHAUSTED", 409)

    def release(self, allocation: JsonObject, *, marker: str) -> None:
        ip_id = _response_int(
            allocation.get("netbox_ip_id") or allocation.get("ip_id") or 0
        )
        parsed_prefix = ip_network(self.config.expected_prefix)
        if not isinstance(parsed_prefix, IPv4Network):
            raise NetBoxError("IPAM_PREFIX_INVALID")
        prefix = parsed_prefix
        if ip_id <= 0:
            record = self.owned_allocation(prefix, marker)
            if record is None:
                if allocation.get("status") == "release_pending":
                    raise NetBoxError("IPAM_REQUEST_UNCERTAIN", uncertain=True)
                return
            ip_id = _response_int(record["id"])
            allocation = dict(allocation, address=record["address"])
        path = f"/ipam/ip-addresses/{ip_id}/"
        try:
            record = self._request("GET", path, retry_get=True)
        except NetBoxError as error:
            if error.status == 404:
                return
            raise
        value = (
            self._address_value(record.get("address"))
            if _is_json_object(record)
            else None
        )
        if (
            not _is_json_object(record)
            or record.get("id") != ip_id
            or not self._marker_matches(record, marker)
            or value is None
            or not self._in_pool(value[0], prefix)
            or value[1] != prefix.prefixlen
            or record.get("vrf") is not None
            or self._is_reserved(record)
            or not allocation.get("address")
            or record.get("address") != allocation["address"]
        ):
            raise NetBoxError("IPAM_IDENTITY_MISMATCH")
        try:
            self._request("DELETE", path)
        except NetBoxError as error:
            if error.status == 404:
                return
            raise
