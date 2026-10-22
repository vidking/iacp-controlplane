"""Synthetic regressions classified against bpg/proxmox 0.116.1 source."""
from copy import deepcopy
import json

import pytest

from iacp.terraform.config import PlanError
from iacp.terraform.executor import verify_postconditions
from tests.test_historical_recovery import artifacts
from tests.test_terraform import plan_json


# The eleven persisted-state differences plus the additional refreshed flags.
# No production values or plan/state contents are stored in these fixtures.
DEFAULTS = [
    (("cpu", 0, "affinity"), "", "0-1"),
    (("cpu", 0, "architecture"), "", "aarch64"),
    (("disk", 0, "file_id"), "", "fixture:iso/other.img"),
    (("disk", 0, "import_from"), "", "fixture:iso/other.img"),
    (("disk", 0, "serial"), "", "other-serial"),
    (("initialization", 0, "interface"), "ide2", "sata0"),
    (("initialization", 0, "user_account", 0, "password"), "", "synthetic-private-password"),
    (("memory", 0, "hugepages"), "", "2"),
    (("network_device", 0, "disconnected"), False, True),
    (("network_device", 0, "trunks"), "", "200;201"),
    (("pool_id",), "", "other-pool"),
    (("cpu", 0, "flags"), [], ["+aes"]),
]


def put(value, path, replacement):
    for key in path[:-1]:
        value = value[key]
    value[path[-1]] = replacement


def path_name(path):
    return "".join(f"[{part}]" if isinstance(part, int) else ("." if index else "") + part
                   for index, part in enumerate(path))


def incident_postconditions(variables):
    approved = plan_json(variables)
    approved["resource_changes"][0]["provider_name"] = "registry.terraform.io/bpg/proxmox"
    approved["configuration"] = {"provider_config": {"proxmox": {
        "full_name": "registry.terraform.io/bpg/proxmox", "version_constraint": "0.116.1"}}}
    change = approved["resource_changes"][0]["change"]
    for path, _, _ in DEFAULTS:
        put(change["after"], path, None)
    change["after_unknown"] = {"id": True}
    change["after_sensitive"] = {"initialization": [{"user_account": [{"password": True}]}]}
    persisted = deepcopy(change["after"])
    for path, found, _ in DEFAULTS:
        put(persisted, path, deepcopy(found))
    persisted["cpu"][0]["flags"] = None
    persisted["id"] = str(variables["instance"]["vm_id"])
    refreshed = deepcopy(persisted)
    refreshed["cpu"][0]["flags"] = []
    return approved, persisted, refreshed


def install_incident(runner, monkeypatch):
    approved, persisted, refreshed = incident_postconditions(runner.variables)
    state = runner.state()
    state["resources"][0]["instances"][0]["attributes"] = persisted
    runner.state_override = state
    original = runner.run
    def run(command, *args, **kwargs):
        result = original(command, *args, **kwargs)
        if command == ["show", "-json", "saved.tfplan"]:
            return json.dumps(approved).encode()
        if command == ["show", "-json", "observation.tfplan"]:
            observed = json.loads(result)
            observed["prior_state"]["values"]["root_module"]["child_modules"][0]["resources"][0]["values"] = refreshed
            return json.dumps(observed).encode()
        return result
    monkeypatch.setattr(runner, "run", run)
    return approved, persisted, refreshed


@pytest.fixture
def incident(tmp_path, monkeypatch):
    executor, runner, _, _, args = artifacts(tmp_path, monkeypatch)
    approved, persisted, refreshed = install_incident(runner, monkeypatch)
    return executor, runner, args, approved, persisted, refreshed


def test_all_incident_representations_pass_without_new_apply(incident, monkeypatch):
    executor, runner, args, _, _, _ = incident
    monkeypatch.setenv("IACP_TERRAFORM_APPLY_ENABLED", "1")
    for _ in range(2):
        result = executor.observe(args[0], args[1], args[4], args[5], lambda: False)
        assert result["observation"]["postconditions_verified"] is True
        assert result["observation"]["state_consistency"]["change"] == "unchanged"
    runner.apply_saved.assert_not_called()
    assert set(command[0] for command in runner.commands) == {"version", "init", "show", "plan"}
    assert all(command[1] == "-refresh-only" for command in runner.commands if command[0] == "plan")


@pytest.mark.parametrize("path,default,changed", DEFAULTS, ids=[path_name(row[0]) for row in DEFAULTS])
@pytest.mark.parametrize("source", ["persisted_state", "refreshed_state"])
def test_each_real_change_remains_a_blocker(incident, path, default, changed, source):
    executor, runner, args, _, persisted, refreshed = incident
    put(persisted if source == "persisted_state" else refreshed, path, changed)
    with pytest.raises(PlanError, match="APPROVED_POSTCONDITION_MISMATCH") as failure:
        executor.observe(args[0], args[1], args[4], args[5], lambda: False)
    diagnostic = failure.value.diagnostic()["postcondition"]
    # Either valid automatic drive is allowed alone; disagreement is detected
    # when checking refreshed values against the persisted selected interface.
    expected_source = "refreshed_state" if path == ("initialization", 0, "interface") else source
    assert diagnostic["source"] == expected_source and diagnostic["path"] == path_name(path)
    assert "synthetic-private-password" not in json.dumps(failure.value.diagnostic())
    assert str(failure.value) == "APPROVED_POSTCONDITION_MISMATCH"
    runner.apply_saved.assert_not_called()


@pytest.mark.parametrize("path,default,changed", DEFAULTS, ids=[path_name(row[0]) for row in DEFAULTS])
def test_explicit_known_attributes_and_sensitive_values_are_still_enforced(incident, path, default, changed):
    _, _, _, approved, persisted, _ = incident
    put(approved["resource_changes"][0]["change"]["after"], path, changed)
    put(persisted, path, changed)
    verify_postconditions(approved, persisted)
    put(persisted, path, default)
    with pytest.raises(PlanError, match="APPROVED_POSTCONDITION_MISMATCH"):
        verify_postconditions(approved, persisted)


@pytest.mark.parametrize("found", ["**********", "synthetic-private-password"])
def test_absent_password_is_not_equivalent_to_mask_or_configured_secret(incident, found):
    _, _, _, approved, persisted, _ = incident
    persisted["initialization"][0]["user_account"][0]["password"] = found
    with pytest.raises(PlanError) as failure:
        verify_postconditions(approved, persisted)
    assert failure.value.diagnostic()["postcondition"]["category"] == "sensitive_value_mismatch"
    assert found not in json.dumps(failure.value.diagnostic())


def test_auto_interface_must_be_valid_and_stable_across_observations(incident):
    executor, _, args, _, _, refreshed = incident
    refreshed["initialization"][0]["interface"] = "sata0"
    with pytest.raises(PlanError) as failure:
        executor.observe(args[0], args[1], args[4], args[5], lambda: False)
    assert failure.value.diagnostic()["postcondition"] == {
        "source": "refreshed_state", "path": "initialization[0].interface", "category": "auto_interface_changed"}


@pytest.mark.parametrize("provider_change", ["name", "version"])
def test_normalization_requires_the_pinned_bpg_contract(incident, provider_change):
    _, _, _, approved, persisted, _ = incident
    if provider_change == "name":
        approved["resource_changes"][0]["provider_name"] = "other/provider"
    else:
        approved["configuration"]["provider_config"]["proxmox"]["version_constraint"] = ">= 0.116.1"
    with pytest.raises(PlanError) as failure:
        verify_postconditions(approved, persisted)
    assert failure.value.diagnostic()["postcondition"]["category"] == "provider_contract_unverified"


@pytest.mark.parametrize("found", [None, "", "ide4", "scsi31", "ide2-private-suffix", False])
def test_auto_interface_requires_an_observable_valid_drive(incident, found):
    _, _, _, approved, persisted, _ = incident
    persisted["initialization"][0]["interface"] = found
    with pytest.raises(PlanError) as failure:
        verify_postconditions(approved, persisted)
    assert failure.value.diagnostic()["postcondition"]["category"] == "auto_interface_invalid"


def test_nested_unknown_and_sensitive_masks_do_not_exempt_known_siblings(incident):
    _, _, _, approved, persisted, _ = incident
    change = approved["resource_changes"][0]["change"]
    change["after_unknown"] = {"initialization": [{"user_account": [{"password": True}]}]}
    persisted["initialization"][0]["user_account"][0]["password"] = "synthetic-private-password"
    verify_postconditions(approved, persisted)
    persisted["initialization"][0]["user_account"][0]["username"] = "other-user"
    with pytest.raises(PlanError) as failure:
        verify_postconditions(approved, persisted)
    assert failure.value.diagnostic()["postcondition"]["path"] == "initialization[0].user_account[0].username"


@pytest.mark.parametrize("kind", ["missing", "other-null", "bool-as-int", "ordered-list", "private-map-key"])
def test_equivalences_do_not_generalize_or_expose_private_values(incident, kind):
    _, _, _, approved, persisted, _ = incident
    after = approved["resource_changes"][0]["change"]["after"]
    if kind == "missing":
        del persisted["cpu"][0]["affinity"]
    elif kind == "other-null":
        after["description"] = None
        persisted["description"] = ""
    elif kind == "bool-as-int":
        persisted["network_device"][0]["disconnected"] = 0
    elif kind == "ordered-list":
        after["cpu"][0]["flags"] = ["+aes", "+avx"]
        persisted["cpu"][0]["flags"] = ["+avx", "+aes"]
    else:
        after["private_field_synthetic_secret"] = "synthetic-private-value"
        persisted["private_field_synthetic_secret"] = "different-private-value"
    with pytest.raises(PlanError) as failure:
        verify_postconditions(approved, persisted)
    diagnostic = json.dumps(failure.value.diagnostic())
    assert "synthetic_secret" not in diagnostic and "private-value" not in diagnostic


@pytest.mark.parametrize("source", ["proxmox", "checks"])
def test_power_and_checks_have_distinct_safe_diagnostics(incident, monkeypatch, source):
    executor, runner, args, _, _, _ = incident
    if source == "proxmox":
        runner.proxmox_override = {"present": True, "power_state": "running"}
    else:
        original = runner.run
        def checked(command, *a, **kw):
            result = original(command, *a, **kw)
            if command == ["show", "-json", "observation.tfplan"]:
                observed = json.loads(result)
                observed["checks"] = [{"status": "pass", "instances": [{"status": "unknown", "address": "private-check-address"}]}]
                return json.dumps(observed).encode()
            return result
        monkeypatch.setattr(runner, "run", checked)
    with pytest.raises(PlanError) as failure:
        executor.observe(args[0], args[1], args[4], args[5], lambda: False)
    assert failure.value.diagnostic()["postcondition"]["source"] == source
    assert "private-check-address" not in json.dumps(failure.value.diagnostic())
