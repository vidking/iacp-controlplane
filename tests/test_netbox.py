from ipaddress import ip_network

import pytest

from worker.netbox import NetBoxAllocation, NetBoxClient, NetBoxError, NetBoxSettings, settings


def fake_client() -> NetBoxClient:
    client = object.__new__(NetBoxClient)
    client.config = NetBoxSettings(
        base_url="https://netbox.example/api",
        token="secret",
        prefix_id=42,
    )
    return client


def test_netbox_settings_normalize_status_url(monkeypatch):
    monkeypatch.setenv("IACP_NETBOX_URL", "https://netbox.cyberfuel.com/api/status/")
    monkeypatch.setenv("IACP_NETBOX_TOKEN", "token-for-test")
    monkeypatch.setenv("IACP_NETBOX_PREFIX_ID", "42")
    assert settings().base_url == "https://netbox.cyberfuel.com/api"


def test_netbox_allocation_writes_exact_address_metadata_and_retries_conflicts(monkeypatch):
    client = fake_client()
    prefix = ip_network("198.51.100.0/24")
    candidates = [[("198.51.100.2", 24)], [("198.51.100.3", 24)]]
    requests = []

    monkeypatch.setattr(client, "get_prefix", lambda: (prefix, {"is_pool": False}))
    monkeypatch.setattr(client, "validate_gateway", lambda _: "198.51.100.1/24")
    monkeypatch.setattr(client, "_tag_id", lambda _: 7)
    monkeypatch.setattr(client, "available_addresses", lambda _: candidates.pop(0))
    monkeypatch.setattr(client, "get_ip", lambda _: None)

    def request(method, path, payload=None, **_kwargs):
        requests.append((method, path, payload))
        if method == "POST" and len([item for item in requests if item[0] == "POST"]) == 1:
            raise NetBoxError("IPAM_ADDRESS_CONFLICT", 409)
        return {"id": 88, "address": payload["address"]}

    monkeypatch.setattr(client, "_request", request)
    result = client.allocate(
        instance_id="instance-1",
        operation_id="operation-1",
        vmid=40001,
        name="cliente-demo",
        customer_tag="cl-demo",
    )

    assert result == NetBoxAllocation(
        ip_id=88,
        address="198.51.100.3/24",
        prefix="198.51.100.0/24",
        gateway="198.51.100.1/24",
        marker="iacp-instance=instance-1",
    )
    post = [item for item in requests if item[0] == "POST"][-1]
    assert post[1] == "/ipam/ip-addresses/"
    assert post[2]["address"] == "198.51.100.3/24"
    assert post[2]["tags"] == [7]
    assert "iacp-instance=instance-1" in post[2]["description"]
    assert "operation=operation-1" in post[2]["comments"]


def test_netbox_rejects_gateway_when_it_is_not_reserved(monkeypatch):
    client = fake_client()
    prefix = ip_network("198.51.100.0/24")
    monkeypatch.setattr(client, "get_ip", lambda _: {"id": 1, "status": {"value": "active"}, "tags": []})
    with pytest.raises(NetBoxError, match="IPAM_GATEWAY_NOT_RESERVED"):
        client.validate_gateway(prefix)


def test_netbox_release_requires_ownership_marker(monkeypatch):
    client = fake_client()
    calls = []
    monkeypatch.setattr(
        client,
        "_request",
        lambda method, path, payload=None, **_kwargs: calls.append((method, path, payload))
        or {"id": 88, "description": "other-owner"},
    )
    with pytest.raises(NetBoxError, match="IPAM_IDENTITY_MISMATCH"):
        client.release({"netbox_ip_id": 88}, marker="iacp-instance=mine")
    assert [item[0] for item in calls] == ["GET"]
