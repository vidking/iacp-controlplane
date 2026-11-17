"""Cliente NetBox que conserva las reservas ante resultados inciertos.

El endpoint available-ips elige y crea la dirección bajo el lock de NetBox.
El marcador permite recuperar una respuesta perdida sin reservar otra IP.
"""

from __future__ import annotations

import json
import os
import re
import socket
import ssl
import time
from dataclasses import dataclass
from ipaddress import IPv4Address, IPv4Network, ip_address, ip_network
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, build_opener, HTTPSHandler


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
    token: str
    prefix_id: int
    expected_prefix: str = "190.0.231.0/24"
    prefix_length: int = 24
    gateway_host: int = 1
    allocation_tag: str = "ocupada"
    gateway_tag: str = "reservada"
    timeout: float = 15.0
    attempts: int = 3
    verify_tls: bool = True
    ca_file: str | None = None


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
    prefix_raw = (os.getenv("IACP_NETBOX_PREFIX_ID") or "").strip()
    if not base_url or not token or not prefix_raw:
        raise NetBoxError("IPAM_NOT_CONFIGURED")
    try:
        prefix_id = int(prefix_raw)
        prefix_length = int(os.getenv("IACP_NETBOX_PREFIX_LENGTH", "24"))
        gateway_host = int(os.getenv("IACP_NETBOX_GATEWAY_HOST", "1"))
        timeout = max(1.0, float(os.getenv("IACP_NETBOX_TIMEOUT", "15")))
        attempts = max(1, int(os.getenv("IACP_NETBOX_RETRY_ATTEMPTS", "3")))
    except ValueError as error:
        raise NetBoxError("IPAM_CONFIGURATION_INVALID") from error
    if not 1 <= prefix_id or prefix_length != 24 or gateway_host != 1:
        raise NetBoxError("IPAM_CONFIGURATION_INVALID")
    # Accept both the API root and the status URL shown by NetBox's UI. The
    # client always talks to the API root; `/api/status/` is only a health
    # endpoint and must not become part of resource paths.
    if base_url.endswith("/status"):
        base_url = base_url[: -len("/status")].rstrip("/")
    return NetBoxSettings(
        base_url=base_url if base_url.endswith("/api") else f"{base_url}/api",
        token=token,
        prefix_id=prefix_id,
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
    )


def configured() -> bool:
    return bool(
        (os.getenv("IACP_NETBOX_URL") or "").strip()
        and (os.getenv("IACP_NETBOX_TOKEN") or "").strip()
        and (os.getenv("IACP_NETBOX_PREFIX_ID") or "").strip()
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
                if (
                    retry_get
                    and 500 <= status < 600
                    and attempt + 1 < self.config.attempts
                ):
                    time.sleep(min(4.0, 0.5 * (2**attempt)))
                    continue
                detail = ""
                try:
                    detail = error.read().decode("utf-8", "replace")
                except Exception:
                    pass
                raise NetBoxError(
                    _error_code(status),
                    status,
                    uncertain=method != "GET" and status >= 500,
                    detail=detail,
                ) from error
            except (URLError, TimeoutError, socket.timeout, ValueError) as error:
                if retry_get and attempt + 1 < self.config.attempts:
                    time.sleep(min(4.0, 0.5 * (2**attempt)))
                    continue
                raise NetBoxError(
                    "IPAM_UNAVAILABLE" if method == "GET" else "IPAM_REQUEST_UNCERTAIN",
                    uncertain=method != "GET",
                    detail=str(error),
                ) from error
        raise NetBoxError("IPAM_UNAVAILABLE")

    def get_prefix(self) -> tuple[IPv4Network, dict]:
        response = self._request(
            "GET",
            f"/ipam/prefixes/{self.config.prefix_id}/?fields=id,prefix,is_pool,status,vrf",
            retry_get=True,
        )
        if not isinstance(response, dict):
            raise NetBoxError("IPAM_PREFIX_INVALID")
        raw_prefix = response.get("prefix")
        try:
            prefix = ip_network(str(raw_prefix), strict=True)
        except ValueError as error:
            raise NetBoxError("IPAM_PREFIX_INVALID") from error
        if (
            prefix.version != 4
            or prefix.prefixlen != self.config.prefix_length
            or str(prefix) != self.config.expected_prefix
        ):
            raise NetBoxError("IPAM_PREFIX_INVALID")
        if response.get("vrf") is not None:
            raise NetBoxError("IPAM_PREFIX_VRF_UNSUPPORTED")
        if response.get("is_pool"):
            raise NetBoxError("IPAM_PREFIX_POOL_UNSAFE")
        return prefix, response

    def _tag_id(self, slug: str) -> int:
        query = urlencode({"slug": slug, "limit": 2, "brief": "true"})
        response = self._request("GET", f"/extras/tags/?{query}", retry_get=True)
        rows = response.get("results", []) if isinstance(response, dict) else []
        if len(rows) != 1 or not isinstance(rows[0], dict) or rows[0].get("id") is None:
            raise NetBoxError("IPAM_TAG_NOT_FOUND")
        return int(rows[0]["id"])

    @staticmethod
    def _address_value(value: object) -> tuple[IPv4Address, int] | None:
        raw = str(value or "")
        try:
            if "/" not in raw:
                return None
            address, length = raw.split("/", 1)
            parsed = ip_address(address)
            return (parsed, int(length)) if parsed.version == 4 else None
        except (ValueError, TypeError):
            return None

    def available_addresses(self, prefix: IPv4Network) -> list[tuple[IPv4Address, int]]:
        response = self._request(
            "GET",
            f"/ipam/prefixes/{self.config.prefix_id}/available-ips/",
            retry_get=True,
        )
        rows = response if isinstance(response, list) else []
        candidates = []
        for row in rows:
            value = self._address_value(
                row.get("address") if isinstance(row, dict) else row
            )
            if value is None:
                continue
            address, length = value
            if (
                address in prefix
                and length == self.config.prefix_length
                and address
                not in {
                    prefix.network_address,
                    prefix.broadcast_address,
                    prefix.network_address + self.config.gateway_host,
                }
            ):
                candidates.append(value)
        return candidates

    def get_ip(self, address: str) -> dict | None:
        query = urlencode({"address": address, "limit": 2})
        response = self._request("GET", f"/ipam/ip-addresses/?{query}", retry_get=True)
        rows = response.get("results", []) if isinstance(response, dict) else []
        return rows[0] if len(rows) == 1 and isinstance(rows[0], dict) else None

    @staticmethod
    def _marker_matches(record: dict | None, marker: str) -> bool:
        if not record:
            return False
        text = " ".join(
            str(record.get(key) or "") for key in ("description", "comments")
        )
        return (
            re.search(r"(?:^|[\s;|])" + re.escape(marker) + r"(?:$|[\s;|])", text)
            is not None
        )

    def validate_gateway(self, prefix: IPv4Network) -> str:
        gateway = prefix.network_address + self.config.gateway_host
        address = f"{gateway}/{self.config.prefix_length}"
        record = self.get_ip(address)
        if record is None:
            raise NetBoxError("IPAM_GATEWAY_NOT_RESERVED")
        tags = record.get("tags") or []
        tag_slugs = {
            str(tag.get("slug") or tag.get("name"))
            for tag in tags
            if isinstance(tag, dict)
        }
        status = record.get("status")
        status_value = status.get("value") if isinstance(status, dict) else status
        if self.config.gateway_tag not in tag_slugs and status_value != "reserved":
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
            return int(result["id"])
        except NetBoxError as error:
            if error.code not in {"IPAM_ADDRESS_CONFLICT", "IPAM_REQUEST_UNCERTAIN"}:
                raise
            return self._tag_id(slug)

    def owned_allocation(self, prefix: IPv4Network, marker: str) -> dict | None:
        # Recuperar una reserva cuyo POST terminó antes de persistirla localmente.
        query = urlencode({"parent": str(prefix), "limit": 100, "offset": 0})
        offset = 0
        matches = []
        while True:
            response = self._request(
                "GET", f"/ipam/ip-addresses/?{query}", retry_get=True
            )
            if not isinstance(response, dict) or not isinstance(
                response.get("results"), list
            ):
                raise NetBoxError("IPAM_RESPONSE_INVALID")
            rows = response["results"]
            matches.extend(
                row
                for row in rows
                if isinstance(row, dict)
                and row.get("vrf") is None
                and self._marker_matches(row, marker)
            )
            if not isinstance(response, dict) or not response.get("next"):
                break
            offset += 100
            query = urlencode({"parent": str(prefix), "limit": 100, "offset": offset})
        if len(matches) > 1:
            raise NetBoxError("IPAM_ALLOCATION_CONFLICT")
        return matches[0] if matches else None

    def allocate(self, **kwargs) -> NetBoxAllocation:
        # Serializa los aprovisionadores IACP del mismo prefijo durante la llamada remota.
        from api.db import database

        with database() as db:
            db.execute("BEGIN")
            db.execute("SELECT pg_advisory_xact_lock(105, ?)", (self.config.prefix_id,))
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
        prefix, _ = self.get_prefix()
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
            value = self._address_value(owned.get("address"))
            if value is None or value[0] not in prefix or value[1] != prefix.prefixlen:
                raise NetBoxError("IPAM_IDENTITY_MISMATCH")
            existing_tags = [
                int(tag["id"])
                for tag in owned.get("tags", [])
                if isinstance(tag, dict) and "id" in tag
            ]
            self._request(
                "PATCH",
                f"/ipam/ip-addresses/{int(owned['id'])}/",
                {"tags": list(dict.fromkeys(existing_tags + tag_ids))},
            )
            return NetBoxAllocation(
                int(owned["id"]), str(owned["address"]), str(prefix), gateway, marker
            )
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
            "prefix_length": prefix.prefixlen,
        }
        try:
            response = self._request(
                "POST",
                f"/ipam/prefixes/{self.config.prefix_id}/available-ips/",
                payload,
            )
        except NetBoxError as error:
            if error.uncertain:
                # No repetir un POST cuyo resultado aún no conocemos.
                record = self.owned_allocation(prefix, marker)
                if record is None:
                    raise
                response = record
            elif error.status == 409:
                raise NetBoxError("IPAM_POOL_EXHAUSTED", 409) from error
            else:
                raise
        record = (
            response[0]
            if isinstance(response, list) and len(response) == 1
            else response
        )
        try:
            value = self._address_value(record["address"])
            ip_id = int(record["id"])
            valid = (
                ip_id > 0
                and value is not None
                and value[0] in prefix
                and value[1] == prefix.prefixlen
                and value[0]
                not in {
                    prefix.network_address,
                    prefix.broadcast_address,
                    prefix.network_address + self.config.gateway_host,
                }
                and record.get("vrf") is None
                and self._marker_matches(record, marker)
            )
        except (TypeError, KeyError, ValueError):
            valid = False
        if not valid:
            raise NetBoxError("IPAM_RESPONSE_INVALID", uncertain=True)
        return NetBoxAllocation(ip_id, record["address"], str(prefix), gateway, marker)

    def release(self, allocation: dict, *, marker: str) -> None:
        ip_id = int(allocation.get("netbox_ip_id") or allocation.get("ip_id") or 0)
        if (
            allocation.get("prefix_id")
            and int(allocation["prefix_id"]) != self.config.prefix_id
        ):
            raise NetBoxError("IPAM_IDENTITY_MISMATCH")
        if ip_id <= 0:
            prefix, _ = self.get_prefix()
            record = self.owned_allocation(prefix, marker)
            if record is None:
                return
            ip_id = int(record["id"])
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
            if isinstance(record, dict)
            else None
        )
        prefix = ip_network(self.config.expected_prefix)
        if (
            not isinstance(record, dict)
            or not self._marker_matches(record, marker)
            or value is None
            or value[0] not in prefix
            or value[0]
            in {
                prefix.network_address,
                prefix.broadcast_address,
                prefix.network_address + self.config.gateway_host,
            }
            or value[1] != prefix.prefixlen
            or record.get("vrf") is not None
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
