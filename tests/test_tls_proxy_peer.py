"""La actualización del peer conserva las demás fronteras de configuración."""
import copy
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / 'scripts'))
from operations import compare, review_tls_proxy_peer


def fixture():
    current = {'services': {r: {} for r in ('api', 'worker', 'terraform-worker', 'lifecycle-worker',
                                           'traefik', 'db', 'redis', 'migrate', 'ownership-certifier')}}
    current['services']['api'] = {'environment': {'IACP_TLS_PROXY_IPS': '172.20.0.3', 'IACP_ADMISSION_ENABLED': '0'}}
    candidate = copy.deepcopy(current)
    candidate['services']['api']['environment']['IACP_TLS_PROXY_IPS'] = '172.20.0.2'
    containers = [{'Id': r, 'State': {'Running': True}, 'Config': {
        'Labels': {'com.docker.compose.service': r},
        'Env': ['IACP_TLS_PROXY_IPS=172.20.0.3'] if r == 'api' else []},
        'NetworkSettings': {'Networks': {'frontend': {'IPAddress': '172.20.0.2' if r == 'traefik' else '172.20.0.3'}}}}
        for r in ('api', 'traefik')]
    return current, candidate, containers, {'previous_proxy_ip': '172.20.0.3', 'proxy_ip': '172.20.0.2', 'proxy_container_id': 'traefik'}


def test_peer_observado_permite_solo_la_transicion_exacta_sin_ampliar_otros_permisos():
    old, new, containers, review = fixture()
    assert not compare(old, new)['passed']
    checked, evidence = review_tls_proxy_peer(old, new, containers, review)
    assert compare(checked, new)['passed']
    assert old['services']['api']['environment']['IACP_TLS_PROXY_IPS'] == '172.20.0.3'
    assert evidence['changed_path'] == '/services/api/environment/IACP_TLS_PROXY_IPS'
    new['services']['api']['environment']['IACP_ADMISSION_ENABLED'] = '1'
    assert not compare(checked, new)['passed']


@pytest.mark.parametrize('scenario', ['peer_ajeno', 'rango', 'proxy_sustituido', 'anterior_ajeno',
    'runtime_distinto', 'candidato_distinto', 'proxy_detenido', 'red_ajena', 'red_ambigua', 'campo_extra'])
def test_rechazar_revision_ajena_y_drift_del_runtime(scenario):
    old, new, containers, review = fixture()
    if scenario == 'peer_ajeno': review['proxy_ip'] = '172.20.0.99'
    elif scenario == 'rango': review['proxy_ip'] = '172.20.0.0/24'
    elif scenario == 'proxy_sustituido': review['proxy_container_id'] = 'otro-proxy'
    elif scenario == 'anterior_ajeno': review['previous_proxy_ip'] = '172.20.0.8'
    elif scenario == 'runtime_distinto': containers[0]['Config']['Env'] = ['IACP_TLS_PROXY_IPS=172.20.0.7']
    elif scenario == 'candidato_distinto': new['services']['api']['environment']['IACP_TLS_PROXY_IPS'] = '172.20.0.8'
    elif scenario == 'proxy_detenido': containers[1]['State']['Running'] = False
    elif scenario == 'red_ajena': containers[1]['NetworkSettings']['Networks'] = {'otra': {'IPAddress': '172.20.0.2'}}
    elif scenario == 'red_ambigua':
        for c in containers: c['NetworkSettings']['Networks']['otra'] = {'IPAddress': '172.21.0.2'}
    elif scenario == 'campo_extra': review['permitir_cualquier_peer'] = True
    with pytest.raises(ValueError): review_tls_proxy_peer(old, new, containers, review)
