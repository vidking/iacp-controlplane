#!/usr/bin/env python3
"""Validar alta de propietario, login y workflow n8n en Docker local aislado."""

import json
import re
from pathlib import Path
import secrets
import subprocess
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]


def run(argv, *, data=None):
    return subprocess.run(argv, input=data, text=True, capture_output=True, timeout=180)


def validate():
    image = json.loads((ROOT / 'services/releases.json').read_text())['n8n']['images']['app']
    name = 'iacp-n8n-owner-' + uuid.uuid4().hex[:8]
    password = secrets.token_urlsafe(32) + 'aA9!'
    created = run(['docker', 'run', '--rm', '-d', '--network', 'none', '--name', name,
                   '-e', 'N8N_DIAGNOSTICS_ENABLED=false', '-e', 'N8N_VERSION_NOTIFICATIONS_ENABLED=false', image])
    if created.returncode:
        raise RuntimeError('No se pudo crear el contenedor local de validación')
    def node(program):
        result = run(['docker', 'exec', '-i', name, 'node', '--input-type=module'], data=program)
        if result.returncode:
            code = re.search(r'IACP_CHECK_[A-Z_]+[0-9]{3}', result.stderr)
            raise RuntimeError(code.group(0) if code else 'Falló la comprobación HTTP de n8n; sin registrar respuestas ni credenciales')
    login = '''
const base='http://127.0.0.1:5678/rest';
const account=ACCOUNT;
async function post(path,body){return fetch(base+path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});}
async function json(response,stage){if(!response.headers.get('content-type')?.includes('application/json'))throw Error('IACP_CHECK_'+stage+'_CONTENT_001');try{return await response.json();}catch{throw Error('IACP_CHECK_'+stage+'_JSON_001');}}
'''.replace('ACCOUNT', json.dumps({'email': 'prueba-servicio@example.invalid', 'password': password}))
    try:
        for _ in range(180):
            ready = run(['docker', 'exec', name, 'node', '-e', "fetch('http://127.0.0.1:5678/healthz/readiness').then(async r=>{const j=await r.json();process.exit(r.status===200&&j.status==='ok'?0:1)}).catch(()=>process.exit(1))"])
            if ready.returncode == 0:
                break
            time.sleep(1)
        else:
            raise RuntimeError('n8n no alcanzó readiness de base de datos dentro del plazo local')
        node(login + '''
const setup=await post('/owner/setup',{email:account.email,password:account.password,firstName:'Prueba',lastName:'Servicio'});
const owner=await json(setup,'OWNER_SETUP');
if(setup.status!==200||(owner.data||owner).email!==account.email)throw Error('IACP_CHECK_OWNER_SETUP_HTTP_001');
const signed=await post('/login',{emailOrLdapLoginId:account.email,password:account.password});
const loginBody=await json(signed,'OWNER_LOGIN');
if(signed.status!==200||(loginBody.data||loginBody).email!==account.email)throw Error('IACP_CHECK_OWNER_LOGIN_HTTP_001');
if(!signed.headers.getSetCookie().length)throw Error('IACP_CHECK_OWNER_COOKIE_001');
const bad=await post('/login',{emailOrLdapLoginId:account.email,password:account.password+'X'});
if(bad.status!==401)throw Error('IACP_CHECK_INVALID_PASSWORD_HTTP_001');
''')
        workflow = {'id': 'iacpValidation', 'name': 'IACP validacion', 'active': False,
                    'settings': {'executionOrder': 'v1'}, 'nodes': [
                        {'parameters': {}, 'id': 'start', 'name': 'Start', 'type': 'n8n-nodes-base.manualTrigger', 'typeVersion': 1, 'position': [0, 0]},
                        {'parameters': {'jsCode': 'return [{json:{iacpValidation:true}}];'}, 'id': 'code', 'name': 'Code', 'type': 'n8n-nodes-base.code', 'typeVersion': 2, 'position': [200, 0]},
                    ], 'connections': {'Start': {'main': [[{'node': 'Code', 'type': 'main', 'index': 0}]]}}}
        saved = run(['docker', 'exec', '-i', name, 'sh', '-c', 'cat > /home/node/.n8n/validation.json'], data=json.dumps(workflow))
        if saved.returncode:
            raise RuntimeError('No se pudo preparar el workflow local')
        imported = run(['docker', 'exec', name, 'n8n', 'import:workflow', '--input=/home/node/.n8n/validation.json'])
        executed = run(['docker', 'exec', name, 'env', 'N8N_RUNNERS_BROKER_PORT=5681', 'n8n', 'execute', '--id=iacpValidation']) if imported.returncode == 0 else imported
        if executed.returncode or '"iacpValidation": true' not in executed.stdout:
            raise RuntimeError('Falló la ejecución real del workflow n8n')
        if run(['docker', 'restart', name]).returncode:
            raise RuntimeError('No se pudo reiniciar el contenedor de prueba')
        for attempt in range(180):
            try:
                node(login + '''
const signed=await post('/login',{emailOrLdapLoginId:account.email,password:account.password});
const loginBody=await json(signed,'RESTART_LOGIN');
if(signed.status!==200||(loginBody.data||loginBody).email!==account.email)throw Error('IACP_CHECK_RESTART_LOGIN_HTTP_001');
const cookie=signed.headers.getSetCookie().map(value=>value.split(';')[0]).join('; ');
if(!cookie)throw Error('IACP_CHECK_RESTART_COOKIE_001');
const response=await fetch(base+'/workflows?skip=0&take=10',{headers:{Cookie:cookie}});
if(response.status!==200)throw Error('IACP_CHECK_WORKFLOWS_HTTP_001');
const value=await json(response,'WORKFLOWS');
if(!value.data.some(item=>item.id==='iacpValidation'))throw Error('IACP_CHECK_WORKFLOW_PERSISTENCE_001');
''')
                break
            except RuntimeError:
                if attempt == 89:
                    raise
                time.sleep(1)
        print(json.dumps({'result': 'PASS', 'scope': 'Docker local sin red externa; no certificación MNG', 'owner_setup': 'PASS', 'login_and_rejection': 'PASS', 'code_workflow': 'PASS', 'persistence_after_restart': 'PASS'}))
    finally:
        stopped = run(['docker', 'stop', name])
        if stopped.returncode:
            raise RuntimeError('No se pudo limpiar el contenedor local de prueba')


if __name__ == '__main__':
    validate()
