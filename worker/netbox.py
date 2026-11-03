"""Small, fail-closed NetBox IPAM client used by the provisioning worker.

The worker deliberately uses NetBox's REST API directly instead of adding a
runtime dependency.  IP allocation is a two-step operation: ask NetBox for
available candidates, then create the exact address with all orchestration
metadata in one request.  The unique address constraint in NetBox makes a
concurrent loser retry safely, while the marker lets a retry recognize an
already-created object after a lost HTTP response.
"""

from __future__ import annotations

import json
import os
import socket
import ssl
import time
from dataclasses import dataclass
from ipaddress import IPv4Address, IPv4Network, ip_address, ip_network
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
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
        allocation_tag=(os.getenv("IACP_NETBOX_ALLOCATION_TAG", "ocupada").strip() or "ocupada"),
        gateway_tag=(os.getenv("IACP_NETBOX_GATEWAY_TAG", "reservada").strip() or "reservada"),
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
        body = None if payload is None else json.dumps(payload, separators=(",", ":")).encode()
        headers = {
            "Authorization": f"Bearer {self.config.token}",
            "Accept": "application/json",
        }
        if body is not None:
            headers["Content-Type"] = "application/json"
        request = Request(f"{self.config.base_url}{path}", data=body, headers=headers, method=method)
        for attempt in range(self.config.attempts):
            try:
                with self.opener.open(request, timeout=self.config.timeout) as response:
                    raw = response.read()
                if not raw:
                    return None
                return json.loads(raw)
            except HTTPError as error:
                status = error.code
                if retry_get and 500 <= status < 600 and attempt + 1 < self.config.attempts:
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
        if prefix.version != 4 or prefix.prefixlen != self.config.prefix_length:
            raise NetBoxError("IPAM_PREFIX_INVALID")
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
            value = self._address_value(row.get("address") if isinstance(row, dict) else row)
            if value is None:
                continue
            address, length = value
            if address in prefix and length == self.config.prefix_length and address != prefix.network_address + self.config.gateway_host:
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
        text = " ".join(str(record.get(key) or "") for key in ("description", "comments"))
        return marker in text

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
        return address

    def allocate(
        self,
        *,
        instance_id: str,
        operation_id: str,
        vmid: int,
        name: str,
        customer_tag: str | None = None,
    ) -> NetBoxAllocation:
        prefix, _ = self.get_prefix()
        gateway = self.validate_gateway(prefix)
        tag_id = self._tag_id(self.config.allocation_tag)
        marker = f"iacp-instance={instance_id}"
        description = f"IACP {customer_tag or 'customer'} | {name} | vmid:{vmid} | {marker}"[:200]
        comments = f"{marker}; operation={operation_id}; customer_tag={customer_tag or ''}"
        for _attempt in range(max(1, self.config.attempts * 2)):
            candidates = self.available_addresses(prefix)
            if not candidates:
                raise NetBoxError("IPAM_POOL_EXHAUSTED")
            address, length = candidates[0]
            address_cidr = f"{address}/{length}"
            payload = {
                "address": address_cidr,
                "status": "active",
                "description": description,
                "comments": comments,
                "tags": [tag_id],
            }
            try:
                response = self._request("POST", "/ipam/ip-addresses/", payload)
                record = response if isinstance(response, dict) else {}
                return NetBoxAllocation(
                    ip_id=int(record["id"]),
                    address=address_cidr,
                    prefix=str(prefix),
                    gateway=gateway,
                    marker=marker,
                )
            except NetBoxError as error:
                record = self.get_ip(address_cidr) if error.code in {"IPAM_ADDRESS_CONFLICT", "IPAM_REQUEST_UNCERTAIN"} else None
                if self._marker_matches(record, marker):
                    return NetBoxAllocation(
                        ip_id=int(record["id"]),
                        address=str(record.get("address") or address_cidr),
                        prefix=str(prefix),
                        gateway=gateway,
                        marker=marker,
                    )
                if error.code == "IPAM_ADDRESS_CONFLICT":
                    continue
                raise
        raise NetBoxError("IPAM_ADDRESS_CONFLICT")

    def release(self, allocation: dict, *, marker: str) -> None:
        ip_id = int(allocation.get("netbox_ip_id") or allocation.get("ip_id") or 0)
        if ip_id <= 0:
            raise NetBoxError("IPAM_ALLOCATION_INVALID")
        path = f"/ipam/ip-addresses/{ip_id}/"
        try:
            record = self._request("GET", path, retry_get=True)
        except NetBoxError as error:
            if error.status == 404:
                return
            raise
        if not isinstance(record, dict) or not self._marker_matches(record, marker):
            raise NetBoxError("IPAM_IDENTITY_MISMATCH")
        try:
            self._request("DELETE", path)
        except NetBoxError as error:
            if error.status == 404:
                return
            raise
