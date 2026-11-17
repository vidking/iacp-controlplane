from scripts.prepare_golden_copies import BASE_PACKAGES, COPIES, PROTECTED_BASE_VMIDS


def test_almalinux_network_bootstrap_uses_new_protected_copy_targets():
    assert COPIES == {9: (29010, 29016), 10: (29011, 29017)}
    assert all(source != target for source, target in COPIES.values())
    assert {target for _, target in COPIES.values()}.isdisjoint(PROTECTED_BASE_VMIDS)
    assert {"policycoreutils-python-utils", "iproute"}.issubset(BASE_PACKAGES)
