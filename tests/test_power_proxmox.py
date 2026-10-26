import io
import json
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs
from unittest.mock import Mock

import pytest

from iacp.lifecycle.proxmox import PowerError, ProxmoxPower, Target

TARGET = Target(21001, "mng01", "fixture-kvm")


@pytest.fixture
def native():
    responses = {
        "/access/permissions?path=%2Fvms%2F21001": {"/vms/21001": {"VM.Audit": 1, "VM.PowerMgmt": 1}},
        TARGET.path + "/config?current=1": {"name": TARGET.name},
        TARGET.path + "/status/current": {"vmid": TARGET.vmid, "status": "running", "ha": {"managed": 0}},
        TARGET.path + "/pending": [],
    }
    calls = []
    def opened(request, timeout):
        calls.append(request)
        assert request.get_header("Authorization") == "PVEAPIToken=synthetic-private-token"
        path = request.full_url.split("/api2/json", 1)[1]
        if request.method == "POST":
            action = path.rsplit('/', 1)[1]
            typ = {"start": "qmstart", "shutdown": "qmshutdown", "reboot": "qmreboot"}[action]
            value = f"UPID:mng01:1:2:3:{typ}:21001:synthetic@pve:"
        else:
            value = responses[path]
        if isinstance(value, Exception):
            raise value
        stream = io.BytesIO(json.dumps({"data": value}).encode())
        stream.status = 200
        return stream
    opener = Mock(open=Mock(side_effect=opened))
    return ProxmoxPower("https://fixture.invalid:8006", "synthetic-private-token", opener), responses, calls


@pytest.mark.parametrize("action", ["start", "shutdown", "reboot"])
def test_only_allowlisted_power_endpoints_and_no_forced_fallback(native, action):
    client, _, calls = native
    client.permissions(TARGET, mutate=True)
    observed = client.observe(TARGET, preflight=True)
    assert observed["guest_ready"] is None and observed["guest_agent"] == "unknown"
    client.submit(TARGET, action)
    assert [request.method for request in calls].count("POST") == 1
    last = calls[-1]
    assert last.full_url.endswith("/status/" + action)
    assert parse_qs(last.data.decode()) == ({} if action == "start" else
           {"timeout": ["60"], **({"forceStop": ["0"]} if action == "shutdown" else {})})
    assert not any("/agent/" in request.full_url or "/stop" in request.full_url or "/reset" in request.full_url for request in calls)


@pytest.mark.parametrize("change,expected", [
    ("name", "POWER_IDENTITY_MISMATCH"), ("template", "POWER_IDENTITY_MISMATCH"),
    ("vmid", "POWER_IDENTITY_MISMATCH"), ("locked", "POWER_RESOURCE_BUSY"),
    ("paused", "POWER_VM_PAUSED"), ("ha", "POWER_HA_UNSUPPORTED"),
    ("pending", "POWER_PENDING_CONFIGURATION"), ("delete", "POWER_PENDING_CONFIGURATION"),
    ("malformed", "POWER_PROXMOX_UNAVAILABLE"),
])
def test_preflight_blocks_unsafe_or_ambiguous_native_state(native, change, expected):
    client, responses, calls = native
    config, status = responses[TARGET.path + "/config?current=1"], responses[TARGET.path + "/status/current"]
    if change == "name": config["name"] = "other-vm"
    if change == "template": config["template"] = 1
    if change == "vmid": status["vmid"] = 104
    if change == "locked": config["lock"] = "clone"
    if change == "paused": status["qmpstatus"] = "paused"
    if change == "ha": status["ha"]["managed"] = 1
    if change == "pending": responses[TARGET.path + "/pending"] = [{"key": "memory", "pending": 4096}]
    if change == "delete": responses[TARGET.path + "/pending"] = [{"key": "scsi0", "delete": 1}]
    if change == "malformed": responses[TARGET.path + "/pending"] = {}
    with pytest.raises(PowerError, match=expected):
        client.observe(TARGET, preflight=True)
    assert all(request.method == "GET" for request in calls)


@pytest.mark.parametrize("privilege", ["VM.Audit", "VM.PowerMgmt"])
def test_minimum_native_rbac_is_verified(native, privilege):
    client, responses, _ = native
    del responses["/access/permissions?path=%2Fvms%2F21001"]["/vms/21001"][privilege]
    with pytest.raises(PowerError, match="POWER_PERMISSION_DENIED"):
        client.permissions(TARGET, mutate=True)


@pytest.mark.parametrize("flag", [0, 1])
def test_effective_privilege_does_not_require_propagation(native, flag):
    client, responses, _ = native
    responses["/access/permissions?path=%2Fvms%2F21001"]["/vms/21001"] = {"VM.Audit": flag, "VM.PowerMgmt": flag}
    client.permissions(TARGET, mutate=True)


@pytest.mark.parametrize("flag", [None, False, True, "0", "1", -1, 2, {}, []])
def test_malformed_effective_permissions_fail_closed(native, flag):
    client, responses, _ = native
    responses["/access/permissions?path=%2Fvms%2F21001"]["/vms/21001"] = {"VM.Audit": 0, "VM.PowerMgmt": flag}
    with pytest.raises(PowerError, match="POWER_PERMISSION_DENIED"):
        client.permissions(TARGET, mutate=True)


@pytest.mark.parametrize("failure", [URLError("private-url"), HTTPError("private-url", 403, "private-message", {}, None)])
def test_transport_errors_are_safe_and_posts_are_never_retried(native, failure):
    client, _, _ = native
    client.opener.open.side_effect = failure
    with pytest.raises(PowerError) as error:
        client.submit(TARGET, "start")
    assert "private" not in str(error.value)
    client.opener.open.assert_called_once()


@pytest.mark.parametrize("task", ["UPID:mng01:1:2:3:qmreboot:104:user:", "UPID:mng02:1:2:3:qmreboot:21001:user:",
    "UPID:mng01:1:2:3:qmstart:21001:user:", "invalid", None])
def test_reboot_task_identity_is_required(task):
    with pytest.raises(PowerError, match="POWER_TASK_IDENTITY_MISMATCH"):
        ProxmoxPower.validate_task(TARGET, "reboot", task)


def test_guest_agent_timeout_is_reported_as_a_distinct_task_outcome(native):
    client, _, _ = native
    task = "UPID:mng01:1:2:3:qmreboot:21001:synthetic@pve:"
    def opened(request, timeout):
        assert request.method == "GET"
        stream = io.BytesIO(json.dumps({"data": {
            "status": "stopped",
            "exitstatus": "QEMU Guest Agent is not running - VM 21001 qga command 'guest-ping' failed - got timeout"
        }}).encode())
        stream.status = 200
        return stream
    client.opener.open.side_effect = opened
    assert client.task_status(TARGET, "reboot", task) == "guest_agent_timeout"


@pytest.mark.parametrize("vmid,node", [(104, "mng01"), (True, "mng01"), (21001, "../mng01"), (None, "mng01")])
def test_provider_identifiers_are_validated_before_network_access(vmid, node):
    with pytest.raises(PowerError, match="POWER_TARGET_INVALID"):
        Target(vmid, node, "fixture")
