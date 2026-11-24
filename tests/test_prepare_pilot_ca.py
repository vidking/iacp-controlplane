import os

import pytest
from cryptography import x509
from cryptography.hazmat.primitives.serialization import load_pem_private_key

from scripts.prepare_pilot_ca import prepare


def test_prepare_pilot_ca_creates_private_matching_pair_and_never_overwrites(tmp_path):
    directory = tmp_path / "services"
    directory.mkdir(mode=0o700)

    prepare(directory, uid=os.getuid(), gid=os.getgid())

    key_path = directory / "pilot-ca.key"
    cert_path = directory / "pilot-ca.crt"
    key = load_pem_private_key(key_path.read_bytes(), password=None)
    cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
    assert key.public_key().public_numbers() == cert.public_key().public_numbers()
    assert key_path.stat().st_mode & 0o777 == 0o600
    assert cert_path.stat().st_mode & 0o077 == 0
    with pytest.raises(ValueError, match="CA existente"):
        prepare(directory, uid=os.getuid(), gid=os.getgid())


def test_prepare_pilot_ca_rejects_symlink_directory_and_non_private_directory(tmp_path):
    target = tmp_path / "target"
    target.mkdir(mode=0o700)
    alias = tmp_path / "alias"
    alias.symlink_to(target, target_is_directory=True)

    with pytest.raises(ValueError, match="no ser un enlace"):
        prepare(alias, uid=os.getuid(), gid=os.getgid())

    unsafe = tmp_path / "unsafe"
    unsafe.mkdir(mode=0o755)
    unsafe.chmod(0o755)
    with pytest.raises(ValueError, match="permisos privados"):
        prepare(unsafe, uid=os.getuid(), gid=os.getgid())
