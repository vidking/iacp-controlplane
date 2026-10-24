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


def compare(current, candidate, image_changes=(), *, profile_file=None, source_directory=None, root_bootstrap=None):
    if not set(image_changes) <= IMAGE_CHANGES:
        raise ValueError('Only separately reviewed application image changes may be allowed')
    if set(candidate.get('services', {})) != SERVICES:
        raise ValueError('Canonical service inventory differs')
    a, b = normalized(current), normalized(candidate)
    allowed = set(image_changes)
    promoted_paths=[]
    # Narrow opt-in for the lab's reviewed input key and exact TLS proxy IPs.
    # This never changes DB/Redis, tenant tokens, RBAC, worker isolation or ports.
    import ipaddress
    review = root_bootstrap or {}
    key_path = review.get('key_path', '/dev/null')
    proxy_ips = review.get('proxy_ips', '')
    if proxy_ips:
        if any(str(ipaddress.ip_address(v.strip())) != v.strip() for v in proxy_ips.split(',')):
            raise ValueError('Exact reviewed TLS proxy IPs required')
    expected_enabled = '1' if review.get('enabled') is True else '0'
    key = b.get('secrets', {}).get('root_input_key')
    if key and key.get('file') == key_path and key.get('name') == 'iacp-controlplane_root_input_key':
        if 'root_input_key' not in a.get('secrets', {}):
            b['secrets'].pop('root_input_key')
            promoted_paths.append('/secrets/root_input_key')
        for role in ('api', 'terraform-worker'):
            old_service, new_service = a['services'][role], b['services'][role]
            old_env, new_env = old_service.get('environment', {}), new_service.get('environment', {})
            settings = {'IACP_ROOT_PASSWORD_BOOTSTRAP_ENABLED': expected_enabled, 'IACP_ROOT_INPUT_KEY_FILE': '/run/secrets/root_input_key'}
            if role == 'api':
                settings['IACP_TLS_PROXY_IPS'] = proxy_ips
            for name, value in settings.items():
                if name not in old_env and new_env.get(name) == value:
                    new_env.pop(name)
                    promoted_paths.append('/services/'+role+'/environment/'+name)
            additions = [s for s in new_service.get('secrets', []) if s.get('source') == 'root_input_key']
            if (len(additions) == 1 and additions[0].get('target') == '/run/secrets/root_input_key'
                    and not any(s.get('source') == 'root_input_key' for s in old_service.get('secrets', []))):
                new_service['secrets'].remove(additions[0])
                promoted_paths.append('/services/'+role+'/secrets/root_input_key')
    # Typed, reviewed promotion of exactly one readonly policy file and pinned
    # IaC source. Never tolerate arbitrary volume/env/security changes.
    for role in ('api','terraform-worker'):
        old_service,new_service=a['services'][role],b['services'][role]
        if profile_file:
            old_mounts=old_service.get('volumes',[])
            additions=[m for m in new_service.get('volumes',[]) if m.get('target')=='/run/config/profiles.json']
            old_profile=next((m for m in old_mounts if m.get('target')=='/run/config/profiles.json'),None)
            if old_profile and len(additions)==1:
                mount=additions[0]
                if (mount.get('source')==profile_file and mount.get('source')!=old_profile.get('source')
                        and mount.get('type')=='bind' and mount.get('read_only') is True
                        and mount.get('bind',{}).get('create_host_path') is False
                        and {k:v for k,v in mount.items() if k!='source'}=={k:v for k,v in old_profile.items() if k!='source'}):
                    mount['source']=old_profile['source']
                    promoted_paths.append('/services/'+role+'/volumes/profiles.json')
            if not any(m.get('target')=='/run/config/profiles.json' for m in old_mounts) and len(additions)==1:
                mount=additions[0]
                if (mount.get('type')=='bind' and mount.get('source')==profile_file and mount.get('read_only') is True
                        and mount.get('bind',{}).get('create_host_path') is False):
                    new_service['volumes'].remove(mount)
                    if not new_service['volumes'] and 'volumes' not in old_service:new_service.pop('volumes')
                    promoted_paths.append('/services/'+role+'/volumes/profiles.json')
                    env=new_service.get('environment',{})
                    if ('IACP_TERRAFORM_PROFILES_FILE' not in old_service.get('environment',{})
                            and env.get('IACP_TERRAFORM_PROFILES_FILE')=='/run/config/profiles.json'):
                        env.pop('IACP_TERRAFORM_PROFILES_FILE')
                        if not env and 'environment' not in old_service:new_service.pop('environment')
                        promoted_paths.append('/services/'+role+'/environment/IACP_TERRAFORM_PROFILES_FILE')
    if source_directory:
        for role in ('terraform-worker','ownership-certifier'):
            old_mount=next((m for m in a['services'][role].get('volumes',[]) if m.get('target')=='/iac-source'),None)
            new_mount=next((m for m in b['services'][role].get('volumes',[]) if m.get('target')=='/iac-source'),None)
            if old_mount and new_mount and new_mount.get('source')==source_directory and old_mount.get('source')!=source_directory:
                if {k:v for k,v in old_mount.items() if k!='source'}=={k:v for k,v in new_mount.items() if k!='source'} and new_mount.get('read_only') is True:
                    new_mount['source']=old_mount['source']
                    promoted_paths.append('/services/'+role+'/volumes/iac-source')
    for role in ('api','lifecycle-worker'):
        old_env=a['services'][role].get('environment',{});new_env=b['services'][role].get('environment',{})
        if 'IACP_LIFECYCLE_INSTANCE_IDS' not in old_env and new_env.get('IACP_LIFECYCLE_INSTANCE_IDS')=='':
            allowed.add('/services/'+role+'/environment/IACP_LIFECYCLE_INSTANCE_IDS')
    # An upgrade may introduce the destructive capability ONLY disabled. Enabling
    # it or adding UUIDs is a separately reviewed operational change, never drift tolerance.
    for role in ('api', 'terraform-worker'):
        old_env = a['services'][role].get('environment', {})
        new_env = b['services'][role].get('environment', {})
        for key, safe in [('IACP_TERRAFORM_DELETE_ENABLED', '0'), ('IACP_DISPOSABLE_INSTANCE_IDS', '')]:
            if key not in old_env and new_env.get(key) == safe:
                allowed.add('/services/' + role + '/environment/' + key)
    for role in ('api','terraform-worker','lifecycle-worker'):
        old_env=a['services'][role].get('environment',{});new_env=b['services'][role].get('environment',{})
        if 'IACP_LAB_SELF_SERVICE_ENABLED' not in old_env and new_env.get('IACP_LAB_SELF_SERVICE_ENABLED')=='0':
            allowed.add('/services/'+role+'/environment/IACP_LAB_SELF_SERVICE_ENABLED')
    old_tf=current['services'].get('terraform-worker',{}).get('environment',{})
    new_tf=candidate['services'].get('terraform-worker',{}).get('environment',{})
    if 'IACP_KVM_LIFECYCLE_ENABLED' not in old_tf and new_tf.get('IACP_KVM_LIFECYCLE_ENABLED')=='0':
        allowed.add('/services/terraform-worker/environment/IACP_KVM_LIFECYCLE_ENABLED')
    old, new = a['services']['ownership-certifier'], b['services']['ownership-certifier']
    if new.get('environment', {}).get('TMPDIR') == '/run/iacp-certification':
        allowed.add('/services/ownership-certifier/environment/TMPDIR')
    if new.get('tmpfs') == old.get('tmpfs', []) + [EXEC_TMPFS]:
        allowed.add('/services/ownership-certifier/tmpfs')
    changed = differences(a, b)
    unexpected = [p for p in changed if p not in allowed]
    return {'passed': not unexpected, 'changed_paths': changed + promoted_paths, 'unexpected_paths': unexpected,
            'ignored_build_metadata': sorted(role for role, s in current['services'].items() if 'build' in s),
            'note': 'No secret values included; certifier-only executable tmpfs is an explicit regression fix.'}


def runtime_command(containers, docker):
    proxy = next(c for c in containers if c['Config']['Labels'].get('com.docker.compose.service') == 'traefik')
    # Proxy labels intentionally remain historical when only app roles upgrade.
    api = next((c for c in containers if c['Config']['Labels'].get('com.docker.compose.service') == 'api'), None)
    if api:
        files = api['Config']['Labels'].get('com.docker.compose.project.config_files','').split(',')
        if len(files) == 1 and '/deploy/' in files[0] and files[0].endswith('/compose.yaml'):
            proxy = api
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
