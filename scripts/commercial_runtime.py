"""Comparación acotada de la configuración G6; sin valores de secretos."""
from pathlib import PurePosixPath


def normalize_commercial(current, candidate, review):
    """Normalizar solo campos revisados; el comparador general verifica el resto."""
    expected = {'contract', 'aes_key_path', 'ssh_key_path', 'ttl_seconds', 'admission_credentials_path'}
    if review is None:
        review = dict(contract='historical', aes_key_path='/dev/null',
            ssh_key_path='/dev/null', ttl_seconds=900, admission_credentials_path='/dev/null')
    elif set(review) != expected:
        raise ValueError('Revisión G6 incompleta o con campos no autorizados')
    contract = review['contract']
    ttl = review['ttl_seconds']
    if contract not in {'historical', 'commercial-g', 'provisioning-api-core'} or type(ttl) is not int or not 60 <= ttl <= 3600:
        raise ValueError('Contrato o TTL G6 inválido')
    paths = {'bootstrap_aes_key': review['aes_key_path'],
        'guest_bootstrap_ssh_key': review['ssh_key_path'],
        'admission_credentials': review['admission_credentials_path']}
    for path in paths.values():
        if not isinstance(path, str) or not PurePosixPath(path).is_absolute() or '..' in PurePosixPath(path).parts:
            raise ValueError('Referencia absoluta de secreto G6 requerida')
        if contract in {'commercial-g', 'provisioning-api-core'} and path == '/dev/null':
            raise ValueError('El contrato comercial requiere secretos G6 reales')
    changed = []
    def field(old, new, key, value, prefix):
        if not isinstance(old, dict) or not isinstance(new, dict):
            return
        if new.get(key) != value:
            return  # El diff general rechazará el valor no revisado.
        if key in old:
            if old[key] != value:
                new[key] = old[key]
                changed.append(prefix+'/'+key)
        else:
            new.pop(key)
            changed.append(prefix+'/'+key)
    for name, path in paths.items():
        old = current.get('secrets', {})
        new = candidate.get('secrets', {})
        # No admite cambiar silenciosamente una clave activa durante el cambio.
        if name in old and old[name].get('file') not in {'/dev/null', path}:
            raise ValueError('Rotación de secreto G6 fuera de alcance')
        field(old, new, name, {'name': 'iacp-controlplane_'+name, 'file': path}, '/secrets')
    if not candidate.get('secrets') and 'secrets' not in current:
        candidate.pop('secrets', None)
    settings = {
        'api': {'IACP_CREATION_CONTRACT': contract,
            'IACP_ADMISSION_CREDENTIALS_FILE': '/run/secrets/admission_credentials',
            'IACP_BOOTSTRAP_AES_KEY_FILE': '/run/secrets/bootstrap_aes_key',
            'IACP_BOOTSTRAP_SECRET_TTL_SECONDS': str(ttl)},
        'terraform-worker': {'IACP_BOOTSTRAP_AES_KEY_FILE': '/run/secrets/bootstrap_aes_key',
            'IACP_BOOTSTRAP_SECRET_TTL_SECONDS': str(ttl),
            'IACP_GUEST_BOOTSTRAP_SSH_KEY_FILE': '/run/secrets/guest_bootstrap_ssh_key'}}
    mounts = {'api': ['bootstrap_aes_key', 'admission_credentials'],
        'terraform-worker': ['bootstrap_aes_key', 'guest_bootstrap_ssh_key']}
    for role, values in settings.items():
        a, b = current['services'][role], candidate['services'][role]
        for key, value in values.items():
            field(a.get('environment', {}), b.get('environment', {}), key, value,
                '/services/'+role+'/environment')
        for name in mounts[role]:
            mount = {'source': name, 'target': '/run/secrets/'+name}
            before, after = a.get('secrets', []), b.get('secrets', [])
            if not isinstance(before, list) or not isinstance(after, list):
                continue
            if not all(isinstance(s, dict) for s in before + after):
                continue
            if mount in after and not any(s.get('source') == name for s in before):
                after.remove(mount)
                changed.append('/services/'+role+'/secrets/'+name)
        for key in ('environment', 'secrets'):
            if not b.get(key) and key not in a:
                b.pop(key, None)
    return changed
