from ipaddress import ip_network

import pytest

from worker.netbox import NetBoxClient, NetBoxError, NetBoxSettings, settings


def fake_client() -> NetBoxClient:
    client = object.__new__(NetBoxClient)
    client.config = NetBoxSettings(
        base_url="https://netbox.example/api",
        token="secret",
        prefix_id=42,
        expected_prefix="198.51.100.0/24",
    )
    return client


def test_netbox_settings_normalize_status_url(monkeypatch):
    monkeypatch.setenv("IACP_NETBOX_URL", "https://netbox.cyberfuel.com/api/status/")
    monkeypatch.setenv("IACP_NETBOX_TOKEN", "token-for-test")
    monkeypatch.setenv("IACP_NETBOX_PREFIX_ID", "42")
    assert settings().base_url == "https://netbox.cyberfuel.com/api"


def test_netbox_allocation_uses_server_atomic_allocator(monkeypatch):
    client = fake_client()
    prefix = ip_network("198.51.100.0/24")
    requests = []
    monkeypatch.setattr(client, "get_prefix", lambda: (prefix, {"is_pool": False}))
    monkeypatch.setattr(client, "owned_allocation", lambda *_: None)
    monkeypatch.setattr(client, "ensure_identity_tag", lambda _: 8)
    monkeypatch.setattr(client, "validate_gateway", lambda _: "198.51.100.1")
    monkeypatch.setattr(client, "_tag_id", lambda _: 7)

    def request(method, path, payload=None, **_kwargs):
        requests.append((method, path, payload))
        return {"id": 88, "address": "198.51.100.3/24", "comments": payload["comments"]}

    monkeypatch.setattr(client, "_request", request)
    result = client.allocate(
        instance_id="instance-1",
        operation_id="operation-1",
        vmid=40001,
        name="cliente-demo",
        customer_tag="cl-demo",
    )
    assert result.address == "198.51.100.3/24"
    assert requests[0][0:2] == ("POST", "/ipam/prefixes/42/available-ips/")
    assert "address" not in requests[0][2]
    assert requests[0][2]["tags"] == [7, 8]
    assert requests[0][2]["prefix_length"] == 24


def test_netbox_rejects_gateway_when_it_is_not_reserved(monkeypatch):
    client = fake_client()
    prefix = ip_network("198.51.100.0/24")
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


def test_exact_prefix_and_reserved_candidates(monkeypatch):
    client = fake_client()
    monkeypatch.setattr(
        client,
        "_request",
        lambda *_args, **_kwargs: {"prefix": "203.0.113.0/24", "is_pool": False},
    )
    with pytest.raises(NetBoxError, match="IPAM_PREFIX_INVALID"):
        client.get_prefix()
    monkeypatch.setattr(
        client,
        "_request",
        lambda *_args, **_kwargs: [
            {"address": f"198.51.100.{host}/24"} for host in [0, 1, 2, 255]
        ],
    )
    assert [
        str(ip) for ip, _ in client.available_addresses(ip_network("198.51.100.0/24"))
    ] == ["198.51.100.2"]


def test_lost_post_reuses_owned_address_and_preserves_external_tags(monkeypatch):
    client = fake_client()
    prefix = ip_network("198.51.100.0/24")
    monkeypatch.setattr(client, "get_prefix", lambda: (prefix, {}))
    monkeypatch.setattr(client, "validate_gateway", lambda _: "198.51.100.1")
    monkeypatch.setattr(client, "_tag_id", lambda _: 7)
    monkeypatch.setattr(client, "ensure_identity_tag", lambda _: 8)
    monkeypatch.setattr(
        client,
        "owned_allocation",
        lambda *_: {"id": 44, "address": "198.51.100.2/24", "tags": [{"id": 99}]},
    )
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
    monkeypatch.setattr(
        client, "get_prefix", lambda: (ip_network("198.51.100.0/24"), {})
    )
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
    prefix = ip_network("198.51.100.0/24")
    monkeypatch.setattr(client, "get_prefix", lambda: (prefix, {}))
    monkeypatch.setattr(client, "validate_gateway", lambda _: "198.51.100.1")
    monkeypatch.setattr(client, "_tag_id", lambda _: 7)
    record = {"id": 90, "address": "198.51.100.8/24", "comments": "iacp-instance=lost"}
    results = iter([None, record])
    monkeypatch.setattr(client, "owned_allocation", lambda *_: next(results))

    def request(method, path, payload=None, **kwargs):
        calls.append(method)
        raise NetBoxError("IPAM_REQUEST_UNCERTAIN", uncertain=True)

    monkeypatch.setattr(client, "_request", request)
    allocation = client.allocate(
        instance_id="lost", operation_id="op", vmid=40003, name="lost-test"
    )
    assert allocation.ip_id == 90 and calls == ["POST"]


def test_gateway_contract_returns_ip_without_prefix(monkeypatch):
    client = fake_client()
    observed = []
    def get_ip(address):
        observed.append(address)
        return {"status": {"value": "reserved"}, "tags": []}
    monkeypatch.setattr(client, "get_ip", get_ip)
    assert client.validate_gateway(ip_network("198.51.100.0/24")) == "198.51.100.1"
    assert observed == ["198.51.100.1/24"]
