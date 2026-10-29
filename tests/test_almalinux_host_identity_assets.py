"""Regresiones estáticas del canal de identidad de las plantillas AlmaLinux."""
from pathlib import Path


ROOT = Path(__file__).parents[1]
ALMA = ROOT / 'deploy' / 'mng' / 'almalinux'


def test_selinux_policy_grants_qga_read_only_host_public_key():
    policy = (ALMA / 'iacp_qga.te').read_text()
    assert 'iacp_guest_host_public_t' in policy
    assert 'allow virt_qemu_ga_t iacp_guest_host_public_t:file { getattr ioctl map open read };' in policy
    assert 'allow virt_qemu_ga_t iacp_guest_host_public_t:file { create' not in policy
    assert 'allow virt_qemu_ga_t iacp_guest_host_public_t:dir { add_name' not in policy


def test_file_context_is_dedicated_and_publisher_is_root_only():
    context = (ALMA / 'iacp_qga.fc').read_text()
    publisher = (ALMA / 'iacp-publish-host-key').read_text()
    unit = (ALMA / 'iacp-publish-host-key.service').read_text()
    assert 'iacp_guest_host_public_t' in context
    assert '/etc/ssh/ssh_host_ed25519_key.pub' in publisher
    assert '/var/lib/iacp/host-identity' in publisher
    assert 'install -o root -g root -m 0444' in publisher
    assert 'restorecon -F' in publisher
    assert '${SOURCE%.pub}' in publisher
    assert 'sshd -T' in publisher
    assert 'hostkey' in publisher
    assert 'Before=cloud-init-final.service' in unit
    assert 'ExecStart=/usr/local/sbin/iacp-publish-host-key' in unit


def test_functional_publisher_harness_covers_both_alma_versions_and_handshake():
    harness = (ROOT / 'scripts' / 'test-almalinux-host-key-publisher.sh').read_text()
    assert 'almalinux:9' in harness and 'almalinux:10' in harness
    assert 'ssh-keyscan' in harness
    assert 'cmp -s /etc/ssh/ssh_host_ed25519_key.pub' in harness
    assert '/usr/sbin/sshd -D' in harness
