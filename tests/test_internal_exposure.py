"""Synthetic deployment fixtures; no corporate approval or network evidence is produced."""
from argparse import Namespace
import importlib.util
import json
from pathlib import Path
import ssl

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'scripts' / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


driver = load('internal_driver', 'go_live.py')
probe = load('internal_probe', 'smoke_public.py')
renderer = load('internal_renderer', 'render-tls.py')


def manifest(identity='gateway'):
    return {'exposure_mode': 'corporate-internal', 'api_bind_ip': '192.168.1.50',
            'approved_client_cidrs': ['192.168.1.0/24', '192.168.1.10/32'], 'identity_mode': identity,
            'compose_files': ['compose.public.yaml', 'compose.tls.yaml']}


def policy(identity='gateway'):
    return {'lan_cidrs': ['192.168.1.0/24'], 'crm_source_cidrs': ['192.168.1.10/32'],
            'crm_origins': ['https://crm.fixture.invalid'], 'identity_mode': identity,
            **({'admin_forward_auth_url': 'https://identity.fixture.invalid/verify'} if identity == 'gateway' else {})}


def compose():
    return {'services': {'api': {}, 'traefik': {'ports': [
        {'target': 80, 'published': '80', 'host_ip': '192.168.1.50'},
        {'target': 443, 'published': '443', 'host_ip': '192.168.1.50'},
        {'target': 8080, 'published': '8080', 'host_ip': '127.0.0.1'}]}}}


def bindings():
    return {'State': {'Running': True}, 'HostConfig': {'PortBindings': {
        '80/tcp': [{'HostIp': '192.168.1.50', 'HostPort': '80'}],
        '443/tcp': [{'HostIp': '192.168.1.50', 'HostPort': '443'}],
        '8080/tcp': [{'HostIp': '127.0.0.1', 'HostPort': '8080'}]}}}


def artifact(tmp_path, name, data):
    path = tmp_path / name
    path.write_text(json.dumps(data))
    return {'path': str(path), 'sha256': driver.digest(path)}


def promotion(tmp_path, monkeypatch, identity='gateway'):
    p = object.__new__(driver.Promotion)
    p.m = manifest(identity)
    p.evidence = lambda *args: None
    p.m['publication_review'] = artifact(tmp_path, 'review.json', {
        **p.m, 'corporate_dns_approved': True, 'lan_vpn_firewall_approved': True,
        'no_public_nat': True, 'no_wan_exposure': True, 'internal_access_policy_approved': True,
        'proxy_version_approved': True, 'gateway_policy_approved': True,
        'network_evidence_reference': 'SYNTHETIC-UNIT-NETWORK', 'identity_policy_reference': 'SYNTHETIC-UNIT-POLICY'})
    report = {**p.m, 'at': driver.stamp(), 'passed': True, 'host': 'iac.cyberfuel.com',
              'resolved_ips': ['192.168.1.50'], 'external_vantage_asserted': False,
              'lan_vpn_vantage_asserted': True, 'runner_ip': '192.168.1.10', 'tls_min_validity_days': 14}
    p.m['activation_review'] = artifact(tmp_path, 'activation.json', {
        'assigned_public_ip_validated': True, 'assigned_public_ip': '190.0.230.162',
        'security_controls_passed': True, 'lan_vpn_https_passed': True, 'pve_permissions_passed': True,
        'vm104_excluded': True, 'lan_vpn_probe': artifact(tmp_path, 'probe.json', report)})
    p.m['tls_dynamic_file'] = artifact(tmp_path, 'iacp.yml', renderer.build_public(policy(identity), internal=True))
    p.compose = lambda *args, **kwargs: json.dumps(compose())
    p.container = lambda role: bindings()
    monkeypatch.setattr(driver, 'resolved_addresses', lambda: ['192.168.1.50'])
    return p


@pytest.mark.parametrize('address', ['0.0.0.0', '127.0.0.1', '169.254.1.1', '190.0.230.162', '192.0.2.1'])
def test_internal_api_cannot_use_any_global_or_special_bind(address):
    with pytest.raises(ValueError):
        probe.internal_target({**manifest(), 'api_bind_ip': address})


def test_typo_or_public_sources_cannot_weaken_internal_mode():
    with pytest.raises(ValueError):
        probe.exposure_mode({'exposure_mode': 'corporate-interna'})
    for ranges in ([], ['0.0.0.0/0'], ['190.0.230.0/24']):
        with pytest.raises(ValueError):
            probe.internal_target({**manifest(), 'approved_client_cidrs': ranges})
    with pytest.raises(ValueError):
        renderer.build_public({**policy(), 'crm_source_cidrs': ['190.0.230.162/32']}, internal=True)
    with pytest.raises(ValueError):
        renderer.build_public({k: v for k, v in policy().items() if k != 'identity_mode'}, internal=True)
    with pytest.raises(ValueError):
        renderer.build_public(policy('approved-internal-policy'))  # Public mode still requires its gateway.


def test_missing_tls_never_writes_internal_routing(tmp_path, monkeypatch):
    source = tmp_path / 'policy.json'
    source.write_text(json.dumps(policy('approved-internal-policy')))
    output = tmp_path / 'dynamic'
    monkeypatch.setattr('sys.argv', ['render-tls', '--corporate-internal', '--policy', str(source),
        '--certificates', str(tmp_path / 'no-certificates'), '--output-dir', str(output), '--bind-ip', '192.168.1.50'])
    with pytest.raises(FileNotFoundError):
        renderer.main()
    assert not output.exists()


def test_probe_rejects_wrong_corporate_dns_before_tls_or_secrets(tmp_path, monkeypatch):
    m = tmp_path / 'manifest.json'
    m.write_text(json.dumps(manifest()))
    monkeypatch.setattr(probe, 'resolved_addresses', lambda: ['192.168.1.51'])
    args = Namespace(manifest=m, vantage_id='unit-fixture', external_vantage=False, lan_vpn_vantage=True)
    result = probe.probe(args)
    assert not result['passed']
    assert result['checks'][-1] == {'name': 'corporate_dns', 'passed': False}


def test_probe_rejects_invalid_internal_tls(tmp_path, monkeypatch):
    from contextlib import nullcontext
    m = tmp_path / 'manifest.json'
    m.write_text(json.dumps(manifest()))
    monkeypatch.setattr(probe, 'resolved_addresses', lambda: ['192.168.1.50'])
    monkeypatch.setattr(probe.socket, 'create_connection', lambda *args, **kwargs: nullcontext(object()))
    class InvalidTLS:
        def wrap_socket(self, *args, **kwargs):
            raise ssl.SSLCertVerificationError('synthetic invalid chain')
    monkeypatch.setattr(probe.ssl, 'create_default_context', lambda **kwargs: InvalidTLS())
    args = Namespace(manifest=m, vantage_id='unit-fixture', external_vantage=False, lan_vpn_vantage=True, ca_file=None)
    result = probe.probe(args)
    assert not result['passed'] and result['failure'] == 'SSLCertVerificationError'


@pytest.mark.parametrize('identity', ['gateway', 'approved-internal-policy'])
def test_internal_activation_accepts_private_api_and_preserves_vm106_public_ip(tmp_path, monkeypatch, identity):
    p = promotion(tmp_path, monkeypatch, identity)
    assert p.activation_controls()['assigned_public_ip'] == '190.0.230.162'


@pytest.mark.parametrize('missing', ['no_wan_exposure', 'no_public_nat', 'internal_access_policy_approved', 'gateway_policy_approved'])
def test_missing_internal_network_or_identity_approval_blocks_publication(tmp_path, monkeypatch, missing):
    p = promotion(tmp_path, monkeypatch)
    review = json.loads(Path(p.m['publication_review']['path']).read_text())
    review[missing] = False
    p.m['publication_review'] = artifact(tmp_path, 'incomplete.json', review)
    p.approve = p.preflight = p.verify = lambda *args, **kwargs: None
    p.up = lambda *args: pytest.fail('Must not change proxy with incomplete controls')
    with pytest.raises(RuntimeError, match='Publication controls incomplete'):
        p.publish()


@pytest.mark.parametrize('port,ip', [(443, '0.0.0.0'), (443, '190.0.230.162'), (8080, '192.168.1.50')])
def test_internal_proxy_rejects_wildcard_wan_and_lan_diagnostic(tmp_path, monkeypatch, port, ip):
    p = promotion(tmp_path, monkeypatch)
    cfg = compose()
    next(v for v in cfg['services']['traefik']['ports'] if v['target'] == port)['host_ip'] = ip
    with pytest.raises(RuntimeError):
        p.proxy_exposure(cfg, renderer.build_public(policy(), internal=True))
    live = bindings()
    live['HostConfig']['PortBindings'][f'{port}/tcp'][0]['HostIp'] = ip
    p.container = lambda role: live
    with pytest.raises(RuntimeError):
        p.internal_live_bindings()


def test_internal_proxy_cannot_omit_gateway_or_security_middleware(tmp_path, monkeypatch):
    p = promotion(tmp_path, monkeypatch)
    for missing in ('corporate-identity', 'body-limit', 'internal-sources'):
        config = renderer.build_public(policy(), internal=True)
        config['http']['routers']['iacp-admin']['middlewares'].remove(missing)
        with pytest.raises(RuntimeError):
            p.proxy_exposure(compose(), config)


def test_legacy_public_activation_report_is_still_accepted(tmp_path):
    p = object.__new__(driver.Promotion)
    proof = artifact(tmp_path, 'public-probe.json', {'passed': True, 'at': driver.stamp(),
        'external_vantage_asserted': True, 'host': 'iac.cyberfuel.com', 'resolved_ips': ['1.1.1.1']})
    p.m = {'activation_review': artifact(tmp_path, 'public-review.json', {
        'assigned_public_ip_validated': True, 'security_controls_passed': True, 'external_https_passed': True,
        'pve_permissions_passed': True, 'vm104_excluded': True, 'external_probe': proof})}
    assert p.activation_controls()['external_https_passed'] is True


def test_internal_policy_still_requires_api_token_but_not_gateway_cookie(tmp_path):
    token = tmp_path / 'operator.token'
    token.write_text('synthetic-unit-token')
    token.chmod(0o600)
    p = object.__new__(driver.Promotion)
    p.m = {**manifest('approved-internal-policy'), 'operator_token_file': str(token)}
    assert p.api_credentials() == ('synthetic-unit-token', None)
    p.m['identity_mode'] = 'gateway'
    p.m['gateway_cookie_file'] = str(tmp_path / 'missing-session')
    with pytest.raises(FileNotFoundError):
        p.api_credentials()


@pytest.mark.parametrize('mode', ['public', 'corporate-internal'])
def test_activate_never_enables_lifecycle_without_recorded_certificate(tmp_path, mode):
    p = object.__new__(driver.Promotion)
    p.m = {'exposure_mode': mode, 'recorded_certificate': artifact(tmp_path, 'not-recorded.json', {'data': {'recorded': False}})}
    p.approve = p.verify = lambda *args, **kwargs: None
    p.activation_controls = lambda: {'assigned_public_ip': '190.0.230.162'}
    p.state = lambda: {'assigned_ipv4_cidr': '190.0.230.162/24'}
    p.container = lambda role: {'Config': {'Env': ['IACP_KVM_LIFECYCLE_ENABLED=0']}}
    p.flags = p.up = lambda *args, **kwargs: pytest.fail('Certificate is required before enabling power')
    with pytest.raises(RuntimeError, match='No recorded certificate'):
        p.activate()
