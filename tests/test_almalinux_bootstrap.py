import ast
import hashlib
import json
import subprocess
import pytest
from scripts.prepare_almalinux_bootstrap import prepare


def test_bundle_is_fixed_validated_and_does_not_disable_selinux(tmp_path):
    output = prepare(tmp_path / "bundle")
    helper = (output / "iacp-guest-bootstrap").read_bytes()
    ast.parse(helper.decode())
    manifest = json.loads((output / "manifest.json").read_text())
    assert manifest["helper_sha256"] == hashlib.sha256(helper).hexdigest()
    assert manifest["bootstrap_protocol"] == 1
    installer = (output / "install.sh").read_text()
    assert "setenforce" not in installer and "SELINUX=disabled" not in installer
    assert "virt_qemu_ga_unconfined_exec_t" in installer
    assert "setsebool -P virt_qemu_ga_run_unconfined on" in installer
    subprocess.run(["bash", "-n", str(output / "install.sh")], check=True)
    with pytest.raises(FileExistsError):
        prepare(output)
