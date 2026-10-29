"""Regresiones del canal dedicado de identidad SSH para AlmaLinux."""
from urllib.parse import parse_qs, urlsplit

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization

from iacp.services.ssh_host_identity import (
    DEDICATED_HOST_KEY_PATH,
    LEGACY_HOST_KEY_PATH,
    HostIdentityError,
    path_for_image,
    read,
)


def public_key():
    return Ed25519PrivateKey.generate().public_key().public_bytes(
        serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH
    ).decode()


class Client:
    def __init__(self, content):
        self.content = content
        self.calls = []

    def _request(self, method, path):
        self.calls.append((method, path))
        return {'content': self.content, 'truncated': False}


@pytest.mark.parametrize('image', ['almalinux-9', 'almalinux-10'])
def test_almalinux_uses_only_dedicated_read_only_channel(image):
    content = public_key() + ' image-host\n'
    client = Client(content)
    evidence = read(client, type('Target', (), {'path': '/v1'})(), image)

    assert evidence.path == DEDICATED_HOST_KEY_PATH
    assert parse_qs(urlsplit(client.calls[0][1]).query)['file'] == [DEDICATED_HOST_KEY_PATH]
    assert LEGACY_HOST_KEY_PATH not in client.calls[0][1]


def test_ubuntu_keeps_legacy_channel_until_template_migration():
    assert path_for_image('ubuntu-2404') == LEGACY_HOST_KEY_PATH


@pytest.mark.parametrize('content', [
    '',
    'ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABAQC7 invalid\n',
    'ssh-ed25519 not-base64\n',
    'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIA==\nextra\n',
])
def test_invalid_public_key_evidence_fails_closed(content):
    with pytest.raises(HostIdentityError, match='BOOTSTRAP_HOST_KEY_EVIDENCE_INVALID'):
        read(Client(content), type('Target', (), {'path': '/v1'})(), 'almalinux-9')


def test_truncated_or_unavailable_channel_is_distinguished():
    class Truncated(Client):
        def _request(self, method, path):
            self.calls.append((method, path))
            return {'content': public_key(), 'truncated': True}

    with pytest.raises(HostIdentityError, match='BOOTSTRAP_HOST_KEY_EVIDENCE_INVALID'):
        read(Truncated(''), type('Target', (), {'path': '/v1'})(), 'almalinux-9')

    class Broken(Client):
        def _request(self, method, path):
            raise RuntimeError('transport')

    with pytest.raises(HostIdentityError, match='BOOTSTRAP_HOST_KEY_CHANNEL_UNAVAILABLE'):
        read(Broken(''), type('Target', (), {'path': '/v1'})(), 'almalinux-9')
