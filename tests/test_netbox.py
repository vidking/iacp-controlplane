import json
from concurrent.futures import ThreadPoolExecutor
from email.message import Message
from io import BytesIO
from ipaddress import IPv4Network, ip_address, ip_network
from threading import Barrier
from time import sleep
from traceback import format_exception
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit

import pytest

from worker.netbox import NetBoxClient, NetBoxError, NetBoxSettings, settings


def ipv4_network(value: str) -> IPv4Network:
    network = ip_network(value)
    assert isinstance(network, IPv4Network)
    return network


def fake_client() -> NetBoxClient:
    client = object.__new__(NetBoxClient)
    client.config = NetBoxSettings(
        base_url="https://netbox.example/api",
        token="secret",
        expected_prefix="198.51.100.0/24",
    )
    return client


def test_netbox_settings_normalize_status_url(monkeypatch):
    monkeypatch.setenv("IACP_NETBOX_URL", "https://netbox.cyberfuel.com/api/status/")
    monkeypatch.setenv("IACP_NETBOX_TOKEN", "token-for-test")
    monkeypatch.delenv("IACP_NETBOX_PREFIX_ID", raising=False)
    assert settings().base_url == "https://netbox.cyberfuel.com/api"
    assert settings().expected_prefix == "190.0.231.0/24"


def test_development_netbox_settings_exclude_unreliable_wan_address(monkeypatch):
    monkeypatch.setenv("IACP_RUNTIME_STAGE", "develop")
    monkeypatch.delenv("IACP_NETBOX_EXCLUDED_ADDRESSES", raising=False)
    monkeypatch.setenv("IACP_NETBOX_URL", "https://netbox.example/api")
    monkeypatch.setenv("IACP_NETBOX_TOKEN", "token-for-test")
    assert settings().excluded_addresses == frozenset(
        {ip_address("190.0.231.2")}
    )

    monkeypatch.setenv("IACP_NETBOX_EXCLUDED_ADDRESSES", "")
    monkeypatch.setenv("IACP_RUNTIME_STAGE", "production")
    assert settings().excluded_addresses == frozenset()


def test_netbox_never_allocates_a_development_excluded_address(monkeypatch):
    client = fake_client()
    client.config = NetBoxSettings(
        base_url="https://netbox.example/api",
        token="test-token",
        expected_prefix="190.0.231.0/24",
        excluded_addresses=frozenset(
            {ip_address("190.0.231.2")}
        ),
    )
    monkeypatch.setattr(client, "_ip_records", lambda _prefix: [])
    candidates = client.available_addresses(ipv4_network("190.0.231.0/24"))
    assert candidates[0] == (ip_address("190.0.231.10"), 24)


def test_netbox_new_allocation_pool_is_limited_to_hosts_10_through_250():
    prefix = ipv4_network("190.0.231.0/24")

    assert not NetBoxClient._in_allocation_pool(ip_address("190.0.231.9"), prefix)
    assert NetBoxClient._in_allocation_pool(ip_address("190.0.231.10"), prefix)
    assert NetBoxClient._in_allocation_pool(ip_address("190.0.231.250"), prefix)
    assert not NetBoxClient._in_allocation_pool(ip_address("190.0.231.251"), prefix)


def test_netbox_keeps_historical_allocations_valid_for_recovery_and_release():
    prefix = ipv4_network("190.0.231.0/24")

    assert NetBoxClient._in_pool(ip_address("190.0.231.2"), prefix)
    assert NetBoxClient._in_pool(ip_address("190.0.231.9"), prefix)
    assert NetBoxClient._in_pool(ip_address("190.0.231.251"), prefix)
    assert NetBoxClient._in_pool(ip_address("190.0.231.253"), prefix)


def test_netbox_allocation_creates_and_confirms_exact_ip(monkeypatch):
    client = fake_client()
    requests = []
    monkeypatch.setattr(client, "owned_allocation", lambda *_: None)
    monkeypatch.setattr(client, "ensure_identity_tag", lambda _: 8)
    monkeypatch.setattr(client, "validate_gateway", lambda _: "198.51.100.1")
    monkeypatch.setattr(client, "_tag_id", lambda _: 7)

    record = {"id": 88, "address": "198.51.100.10/24", "comments": "iacp-instance=instance-1"}
    monkeypatch.setattr(client, "_ip_records", lambda parent: [record] if parent.prefixlen == 32 else [])

    def request(method, path, payload=None, **_kwargs):
        requests.append((method, path, payload))
        return record

    monkeypatch.setattr(client, "_request", request)
    result = client.allocate(
        instance_id="instance-1",
        operation_id="operation-1",
        vmid=40001,
        name="cliente-demo",
        customer_tag="cl-demo",
    )
    assert result.address == "198.51.100.10/24"
    assert requests[0][0:2] == ("POST", "/ipam/ip-addresses/")
    assert requests[0][2]["address"] == "198.51.100.10/24"
    assert requests[0][2]["tags"] == [7, 8]
    assert requests[0][2]["vrf"] is None
    assert "iacp-instance=instance-1" in requests[0][2]["comments"]


def test_netbox_rejects_gateway_when_it_is_not_reserved(monkeypatch):
    client = fake_client()
    prefix = ipv4_network("198.51.100.0/24")
    monkeypatch.setattr(
        client, "get_ip", lambda _: {"id": 1, "status": {"value": "active"}, "tags": []}
    )
    with pytest.raises(NetBoxError, match="IPAM_GATEWAY_NOT_RESERVED"):
        client.validate_gateway(prefix)


def test_netbox_release_requires_ownership_marker(monkeypatch):
    client = fake_client()
    calls = []
    monkeypatch.setattr(
        client,
        "_request",
        lambda method, path, payload=None, **_kwargs: (
            calls.append((method, path, payload))
            or {"id": 88, "description": "other-owner"}
        ),
    )
    with pytest.raises(NetBoxError, match="IPAM_IDENTITY_MISMATCH"):
        client.release({"netbox_ip_id": 88}, marker="iacp-instance=mine")
    assert [item[0] for item in calls] == ["GET"]


def test_netbox_available_ips_paginates_and_protects_boundaries(monkeypatch):
    client = fake_client()
    calls = []

    def request(method, path, **_kwargs):
        url = urlsplit(path)
        query = parse_qs(url.query)
        assert method == "GET" and url.path == "/ipam/ip-addresses/"
        assert query["parent"] == ["198.51.100.0/24"]
        offset = int(query["offset"][0])
        calls.append(offset)
        if offset == 0:
            return {"results": [{"address": "198.51.100.3/32"}], "next": "otra-página"}
        return {"results": [
            {"address": "198.51.100.4/24"},
            {"address": "198.51.100.5/24", "vrf": {"id": 1}},
        ]}

    monkeypatch.setattr(client, "_request", request)
    candidates = client.available_addresses(ipv4_network("198.51.100.0/24"))
    assert calls == [0, 1]
    assert candidates[0] == (ip_address("198.51.100.10"), 24)
    assert candidates[-1] == (ip_address("198.51.100.250"), 24)


def test_netbox_lookup_failure_does_not_log_the_token(
    monkeypatch, caplog
):
    client = fake_client()
    requests = []

    def open_request(request, **_kwargs):
        url = urlsplit(request.full_url)
        requests.append((request.get_method(), url.path))
        assert request.get_header("Authorization") == f"Bearer {client.config.token}"
        assert url.path == "/api/ipam/ip-addresses/"
        raise HTTPError(
            request.full_url,
            404,
            f"Echo de prueba: {client.config.token}",
            Message(),
            BytesIO(json.dumps({"debug": client.config.token}).encode()),
        )

    monkeypatch.setattr(client, "opener", SimpleNamespace(open=open_request), raising=False)
    with pytest.raises(NetBoxError, match="IPAM_NOT_FOUND") as failure:
        client.available_addresses(ipv4_network("198.51.100.0/24"))

    error = failure.value
    assert error.status == 404 and not error.uncertain
    assert requests == [("GET", "/api/ipam/ip-addresses/")]
    assert client.config.token not in repr(client.config)
    assert client.config.token not in error.detail
    assert client.config.token not in "".join(format_exception(error))
    assert client.config.token not in caplog.text


def test_netbox_rejects_supernet_before_sending_request(monkeypatch):
    client = fake_client()
    monkeypatch.setattr(client, "_request", lambda *_args: pytest.fail("Llamada fuera del alcance"))
    with pytest.raises(NetBoxError, match="IPAM_PREFIX_INVALID"):
        client.available_addresses(ipv4_network("198.51.96.0/21"))


def test_lost_post_reuses_owned_address_and_preserves_external_tags(monkeypatch):
    client = fake_client()
    monkeypatch.setattr(client, "validate_gateway", lambda _: "198.51.100.1")
    monkeypatch.setattr(client, "_tag_id", lambda _: 7)
    monkeypatch.setattr(client, "ensure_identity_tag", lambda _: 8)
    owned = {"id": 44, "address": "198.51.100.3/24", "tags": [{"id": 99}], "comments": "iacp-instance=mine"}
    monkeypatch.setattr(client, "owned_allocation", lambda *_: owned)
    monkeypatch.setattr(client, "_ip_records", lambda _: [owned])
    requests = []
    monkeypatch.setattr(
        client, "_request", lambda *args, **kwargs: requests.append(args)
    )
    result = client.allocate(
        instance_id="mine",
        operation_id="op",
        vmid=40001,
        name="demo",
        tags=["app-nextcloud"],
    )
    assert result.ip_id == 44
    assert requests == [("PATCH", "/ipam/ip-addresses/44/", {"tags": [99, 7, 8]})]


def test_release_unknown_post_recovers_marker_before_delete(monkeypatch):
    client = fake_client()
    calls = []
    record = {"id": 90, "address": "198.51.100.8/24", "comments": "iacp-instance=lost"}
    monkeypatch.setattr(client, "owned_allocation", lambda *_: record)
    monkeypatch.setattr(
        client,
        "_request",
        lambda method, path, **_: calls.append((method, path)) or record,
    )
    client.release({"prefix_id": 42}, marker="iacp-instance=lost")
    assert calls == [
        ("GET", "/ipam/ip-addresses/90/"),
        ("DELETE", "/ipam/ip-addresses/90/"),
    ]


def test_release_rejects_wrong_address_and_marker_substring(monkeypatch):
    client = fake_client()
    calls = []
    record = {
        "id": 90,
        "address": "198.51.100.8/24",
        "comments": "iacp-instance=ours-other",
    }
    monkeypatch.setattr(
        client, "_request", lambda method, path, **_: calls.append(method) or record
    )
    with pytest.raises(NetBoxError, match="IDENTITY_MISMATCH"):
        client.release(
            {"netbox_ip_id": 90, "address": "198.51.100.8/24"},
            marker="iacp-instance=ours",
        )
    record["comments"] = "iacp-instance=ours"
    with pytest.raises(NetBoxError, match="IDENTITY_MISMATCH"):
        client.release(
            {"netbox_ip_id": 90, "address": "198.51.100.9/24"},
            marker="iacp-instance=ours",
        )
    assert "DELETE" not in calls


def test_atomic_post_timeout_does_not_allocate_again(monkeypatch):
    client = fake_client()
    calls = []
    monkeypatch.setattr(client, "validate_gateway", lambda _: "198.51.100.1")
    monkeypatch.setattr(client, "_tag_id", lambda _: 7)
    record = {"id": 90, "address": "198.51.100.2/24", "comments": "iacp-instance=lost"}
    results = iter([None, record])
    monkeypatch.setattr(client, "owned_allocation", lambda *_: next(results))
    monkeypatch.setattr(client, "_ip_records", lambda parent: [record] if parent.prefixlen == 32 else [])

    def request(method, path, payload=None, **kwargs):
        calls.append(method)
        raise NetBoxError("IPAM_REQUEST_UNCERTAIN", uncertain=True)

    monkeypatch.setattr(client, "_request", request)
    allocation = client.allocate(
        instance_id="lost", operation_id="op", vmid=40003, name="lost-test"
    )
    assert allocation.ip_id == 90 and calls == ["POST"]


def netbox_for_allocation(monkeypatch):
    client = fake_client()
    monkeypatch.setattr(client, "validate_gateway", lambda _: "198.51.100.1")
    monkeypatch.setattr(client, "_tag_id", lambda _: 7)
    return client


@pytest.mark.parametrize("status", [400, 409])
def test_netbox_reloads_pool_after_a_confirmed_collision(monkeypatch, status):
    client = netbox_for_allocation(monkeypatch)
    records = []
    posted = []
    range_reads = []

    def request(
        method: str,
        path: str,
        payload: dict[str, object] | None = None,
        **_kwargs: object,
    ) -> object:
        url = urlsplit(path)
        assert url.path == "/ipam/ip-addresses/"
        if method == "GET":
            parent = ipv4_network(parse_qs(url.query)["parent"][0])
            if parent.prefixlen == 24:
                range_reads.append(parent)
            return {"results": [r for r in records if ip_address(r["address"].split("/")[0]) in parent]}
        assert method == "POST"
        assert payload is not None
        address = payload["address"]
        posted.append(address)
        if address == "198.51.100.10/24":
            records.append({"id": 23, "address": address, "comments": "otro propietario"})
            raise NetBoxError("IPAM_ADDRESS_CONFLICT", status)
        record = {"id": 24, **payload}
        records.append(record)
        return record

    monkeypatch.setattr(client, "_request", request)
    allocation = client.allocate(instance_id="ours", operation_id="op", vmid=40001, name="mine")
    assert posted == ["198.51.100.10/24", "198.51.100.11/24"]
    assert len(range_reads) == 3
    assert allocation.ip_id == 24
    assert allocation.marker == "iacp-instance=ours"


def test_netbox_does_not_retry_a_rejection_without_evidence_of_occupation(monkeypatch):
    client = netbox_for_allocation(monkeypatch)
    posted = []

    def request(
        method: str,
        _path: str,
        payload: dict[str, object] | None = None,
        **_kwargs: object,
    ) -> object:
        if method == "GET":
            return {"results": []}
        assert payload is not None
        posted.append(payload["address"])
        raise NetBoxError("IPAM_ADDRESS_CONFLICT", 400)

    monkeypatch.setattr(client, "_request", request)
    with pytest.raises(NetBoxError, match="IPAM_ADDRESS_CONFLICT"):
        client.allocate(instance_id="ours", operation_id="op", vmid=40001, name="mine")
    assert posted == ["198.51.100.10/24"]


def test_netbox_duplicate_after_success_blocks_provisioning(monkeypatch):
    client = netbox_for_allocation(monkeypatch)
    record = {"id": 90, "address": "198.51.100.2/24", "comments": "iacp-instance=ours"}
    other = {"id": 91, "address": "198.51.100.2/32", "comments": "otro propietario"}
    calls = []

    def request(method, path, **_kwargs):
        calls.append(method)
        parent = ipv4_network(parse_qs(urlsplit(path).query)["parent"][0])
        return {"results": [record, other] if parent.prefixlen == 32 else []}

    def send(method, path, payload=None, **kwargs):
        if method == "POST":
            calls.append(method)
            return record
        return request(method, path, **kwargs)

    monkeypatch.setattr(client, "_request", send)
    with pytest.raises(NetBoxError, match="IPAM_ALLOCATION_CONFLICT") as failure:
        client.allocate(instance_id="ours", operation_id="op", vmid=40001, name="mine")
    assert failure.value.uncertain
    assert calls.count("POST") == 1
    assert "DELETE" not in calls


def test_netbox_concurrent_workers_reserve_different_ips(monkeypatch):
    client = netbox_for_allocation(monkeypatch)
    ready = Barrier(2, timeout=5)
    records = []

    def request(
        method: str,
        path: str,
        payload: dict[str, object] | None = None,
        **_kwargs: object,
    ) -> object:
        if method == "GET":
            parent = ipv4_network(parse_qs(urlsplit(path).query)["parent"][0])
            return {"results": [r for r in records if ip_address(r["address"].split("/")[0]) in parent]}
        sleep(0.04)
        assert payload is not None
        record = {"id": len(records) + 100, **payload}
        records.append(record)
        return record

    monkeypatch.setattr(client, "_request", request)

    def allocate(index):
        ready.wait()
        return client.allocate(
            instance_id=f"vm-{index}", operation_id=f"op-{index}", vmid=40000 + index,
            name=f"vm-{index}",
        )

    with ThreadPoolExecutor(max_workers=2) as workers:
        allocations = list(workers.map(allocate, (1, 2)))
    assert {entry.address for entry in allocations} == {"198.51.100.10/24", "198.51.100.11/24"}
    assert len({entry.ip_id for entry in allocations}) == 2


@pytest.mark.parametrize(
    ("host", "status"), [(1, "active"), (254, "active"), (12, "reserved")]
)
def test_netbox_release_preserves_exclusions_and_reservations(monkeypatch, host, status):
    client = fake_client()
    record = {
        "id": 90, "address": f"198.51.100.{host}/24", "status": status,
        "comments": "iacp-instance=ours",
    }
    calls = []
    monkeypatch.setattr(
        client, "_request", lambda method, path, **_: calls.append(method) or record
    )
    with pytest.raises(NetBoxError, match="IPAM_IDENTITY_MISMATCH"):
        client.release({"netbox_ip_id": 90, "address": record["address"]}, marker="iacp-instance=ours")
    assert calls == ["GET"]


def test_netbox_release_allows_historical_first_host(monkeypatch):
    client = fake_client()
    record = {
        "id": 90, "address": "198.51.100.2/24", "status": "active",
        "comments": "iacp-instance=ours",
    }
    calls = []
    monkeypatch.setattr(
        client, "_request", lambda method, path, **_: calls.append(method) or record
    )
    client.release({"netbox_ip_id": 90, "address": record["address"]}, marker="iacp-instance=ours")
    assert calls == ["GET", "DELETE"]


def test_netbox_release_keeps_an_unresolved_post_pending(monkeypatch):
    client = fake_client()
    monkeypatch.setattr(client, "owned_allocation", lambda *_: None)
    with pytest.raises(NetBoxError, match="IPAM_REQUEST_UNCERTAIN") as failure:
        client.release({"status": "release_pending"}, marker="iacp-instance=lost")
    assert failure.value.uncertain


def test_gateway_contract_returns_ip_without_prefix(monkeypatch):
    client = fake_client()
    observed = []
    def open_request(request, **_kwargs):
        url = urlsplit(request.full_url)
        observed.append((request.get_method(), url.path, parse_qs(url.query)))
        assert request.get_header("Authorization") == f"Bearer {client.config.token}"
        return BytesIO(
            b'{"results": [{"id": 137, "address": "198.51.100.1/24",'
            b' "status": {"value": "reserved"}}]}'
        )

    monkeypatch.setattr(client, "opener", SimpleNamespace(open=open_request), raising=False)
    assert client.validate_gateway(ipv4_network("198.51.100.0/24")) == "198.51.100.1"
    assert observed == [
        (
            "GET",
            "/api/ipam/ip-addresses/",
            {"address": ["198.51.100.1/24"], "limit": ["2"]},
        )
    ]


@pytest.mark.parametrize("addresses", [[], ["198.51.100.1/21"]])
def test_missing_exact_gateway_reports_absent_ip_address(monkeypatch, addresses):
    client = fake_client()
    monkeypatch.setattr(
        client,
        "_request",
        lambda *_args, **_kwargs: {
            "results": [{"address": address, "status": "reserved"} for address in addresses]
        },
    )
    with pytest.raises(NetBoxError, match="IPAM_GATEWAY_NOT_FOUND") as failure:
        client.validate_gateway(ipv4_network("198.51.100.0/24"))
    assert failure.value.detail == (
        "No se encontró la dirección del gateway 198.51.100.1/24 en NetBox."
    )
