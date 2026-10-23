"""Shared, non-secret Compose comparison. No mutations or application dependencies."""
import copy

SERVICES = {'api', 'worker', 'terraform-worker', 'lifecycle-worker', 'traefik', 'db', 'redis', 'migrate', 'ownership-certifier'}
EXEC_TMPFS = '/run/iacp-certification:rw,exec,nosuid,nodev,size=536870912,mode=0700,uid=10001,gid=10001'
IMAGE_CHANGES = {f'/services/{role}/image' for role in SERVICES - {'db', 'redis', 'traefik'}}


def normalized(config):
    data = copy.deepcopy(config)
    for service in data.get('services', {}).values():
        service.pop('build', None)  # Production never builds or pulls implicitly.
        for key, order in [('volumes', 'target'), ('secrets', 'target'), ('ports', 'target')]:
            if isinstance(service.get(key), list) and all(isinstance(row, dict) for row in service[key]):
                service[key] = sorted(service[key], key=lambda row: str(row.get(order, row.get('source', ''))))
    return data


def differences(a, b, path=''):
    if type(a) is not type(b):
        return [path]
    if isinstance(a, dict):
        result = []
        for key in sorted(a.keys() | b.keys()):
            child = path + '/' + key
            result += differences(a[key], b[key], child) if key in a and key in b else [child]
        return result
    return [] if a == b else [path]


def compare(current, candidate, image_changes=()):
    if not set(image_changes) <= IMAGE_CHANGES:
        raise ValueError('Only separately reviewed application image changes may be allowed')
    if set(candidate.get('services', {})) != SERVICES:
        raise ValueError('Canonical service inventory differs')
    a, b = normalized(current), normalized(candidate)
    allowed = set(image_changes)
    old, new = a['services']['ownership-certifier'], b['services']['ownership-certifier']
    if new.get('environment', {}).get('TMPDIR') == '/run/iacp-certification':
        allowed.add('/services/ownership-certifier/environment/TMPDIR')
    if new.get('tmpfs') == old.get('tmpfs', []) + [EXEC_TMPFS]:
        allowed.add('/services/ownership-certifier/tmpfs')
    changed = differences(a, b)
    unexpected = [p for p in changed if p not in allowed]
    return {'passed': not unexpected, 'changed_paths': changed, 'unexpected_paths': unexpected,
            'ignored_build_metadata': sorted(role for role, s in current['services'].items() if 'build' in s),
            'note': 'No secret values included; certifier-only executable tmpfs is an explicit regression fix.'}


def runtime_command(containers, docker):
    proxy = next(c for c in containers if c['Config']['Labels'].get('com.docker.compose.service') == 'traefik')
    labels = proxy['Config']['Labels']
    args = docker + ['compose', '--env-file', labels['com.docker.compose.project.environment_file'], '-p', 'iacp-controlplane']
    for file in labels['com.docker.compose.project.config_files'].split(','):
        args += ['-f', file]
    return args + ['--profile', 'certification', 'config', '--format', 'json']


def live_compatibility(config, containers):
    """Compare runtime objects too; changing server.env cannot rewrite history."""
    unexpected = []
    for c in containers:
        labels = c['Config']['Labels']
        role = labels.get('com.docker.compose.service')
        if labels.get('com.docker.compose.oneoff', '').lower() == 'true' or role not in config['services']:
            continue
        service = config['services'][role]
        expected_ports = {}
        for item in service.get('ports', []):
            expected_ports.setdefault(str(item['target']) + '/' + item.get('protocol', 'tcp'), []).append(
                {'HostIp': item.get('host_ip', ''), 'HostPort': str(item['published'])})
        if (c['HostConfig']['PortBindings'] or {}) != expected_ports:
            unexpected.append('/live/' + role + '/ports')
        expected_networks = {config['networks'][key]['name'] for key in service.get('networks', {})}
        if set(c['NetworkSettings']['Networks']) != expected_networks:
            unexpected.append('/live/' + role + '/networks')
        env = dict(item.split('=', 1) for item in c['Config']['Env'] if '=' in item)
        for key, value in service.get('environment', {}).items():
            if env.get(key) != value:
                unexpected.append('/live/' + role + '/environment/' + key)
    return {'passed': not unexpected, 'unexpected_paths': unexpected}
