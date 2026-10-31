import importlib.util
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "scripts" / "e2e-commercial-sequential.py"
SPEC = importlib.util.spec_from_file_location("e2e_runner", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


def test_cloud_init_accepts_empty_and_known_deprecation_warnings():
    assert MODULE.cloud_init_recoverable_errors_are_safe({})
    assert MODULE.cloud_init_recoverable_errors_are_safe(
        {"DEPRECATED": ["known cloud-init deprecation"]}
    )


def test_cloud_init_accepts_only_the_known_busy_interface_rename_pair():
    warnings = {
        "WARNING": [
            "Unable to rename interfaces: [['bc:24:11:a5:12:3f', 'eth0', None, None]] "
            "due to errors: ['[busy] Error renaming mac=bc:24:11:a5:12:3f from ens18 to eth0']",
            "Failed to rename devices: Failed to apply network config names: [busy] "
            "Error renaming mac=bc:24:11:a5:12:3f from ens18 to eth0",
        ]
    }
    assert MODULE.cloud_init_recoverable_errors_are_safe(warnings)


def test_cloud_init_rejects_unknown_warnings_and_malformed_errors():
    assert not MODULE.cloud_init_recoverable_errors_are_safe(
        {"WARNING": ["network failed"]}
    )
    assert not MODULE.cloud_init_recoverable_errors_are_safe(
        {"ERROR": ["unexpected failure"]}
    )
    assert not MODULE.cloud_init_recoverable_errors_are_safe(
        {"WARNING": "not-a-list"}
    )
