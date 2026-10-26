"""Valida la sonda preparada sin acceder a Proxmox ni ejecutar aprovisionamiento."""
import ast
import importlib.util
from pathlib import Path

import pytest


def runner():
    path=Path(__file__).resolve().parents[1]/'scripts/e2e-commercial-sequential.py'
    spec=importlib.util.spec_from_file_location('e2e_comercial_preparado',path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module


def test_guest_probe_compiles_both_layers_and_supports_ext4_xfs():
    module=runner()
    tree=ast.parse(module.PROBE)
    guest=next(ast.literal_eval(node.value) for node in ast.walk(tree)
        if isinstance(node,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='program' for t in node.targets))
    compile(guest,'guest-probe','exec')
    assert "filesystem=='ext4'" in guest and "filesystem=='xfs'" in guest
    assert "'dumpe2fs'" in guest and "'xfs_info'" in guest


@pytest.mark.parametrize('change',['disk_bytes','root_partition_end_bytes','filesystem_raw_bytes'])
def test_storage_validation_rejects_incomplete_growth(change):
    module=runner();disk=50*1024**3
    guest=dict(disk_bytes=disk,root_partition_end_bytes=disk-1024,
               root_partition_bytes=disk-1024**2,filesystem_raw_bytes=disk-1024**2,
               filesystem_block_bytes=4096)
    module.validate_storage(guest,50)
    guest[change]-=1024**3
    with pytest.raises(AssertionError):module.validate_storage(guest,50)
