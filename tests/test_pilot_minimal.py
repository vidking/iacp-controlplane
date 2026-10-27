"""Contrato piloto mínimo, compatibilidad histórica y aislamiento sin contactar MNG."""
from copy import deepcopy
import hashlib
import json
from unittest.mock import Mock

import pytest
from pydantic import ValidationError
from sqlalchemy import select, func

from iacp.commercial_contract import CommercialInstanceCreate, MINIMAL_EXAMPLE
from iacp.models import BOOTSTRAP_TENANT_ID, Instance, CommercialIntent, Operation, Tenant
from iacp.security import Principal
from iacp.services.commercial import create
from iacp.services.instances import Conflict
from tests.test_commercial_admission import commercial_config, store, specification
from tests.test_commercial_catalog import catalog_profile
from tests.test_instances import identities


def minimal(**updates):
    body=deepcopy(MINIMAL_EXAMPLE);body.update(updates)
    body['access']['root_password']='SENUELO-PILOTO-NO-PERSISTIR-2026'
    return CommercialInstanceCreate.model_validate(body)


@pytest.mark.parametrize('plan',['kvm-1','kvm-2'])
def test_defaults_preserve_original_intent_and_frozen_allocation(sessions,commercial_config,store,plan,caplog):
    published=[];principal=Principal(BOOTSTRAP_TENANT_ID,'integracion','operator');spec=minimal(plan_id=plan)
    with sessions() as s:
        result=create(s,principal,spec,'piloto',published.append,store,capacity=lambda *_:None)
        instance=s.get(Instance,result.instance_id);row=s.get(CommercialIntent,result.instance_id)
        frozen=deepcopy(row.specification);binding=row.execution_binding
        assert instance.network_profile=='lab-dhcp' and instance.commercial_context is None
        assert frozen['network']=={'profile_id':'lab-dhcp'}
        assert frozen['commercial_context'] is None and frozen['pilot_defaults']['revision']=='lab-dhcp-v1'
        assert instance.tenant_id==principal.tenant_id
        tenants=s.scalar(select(func.count()).select_from(Tenant))
        # Un cambio posterior del perfil no debe volver a resolver ni reservar un retry.
        config=json.loads(commercial_config.read_text());config['profiles']['lab-dhcp']['settings']['network_bridge']='vmbr-invalido'
        commercial_config.write_text(json.dumps(config))
        again=create(s,principal,spec,'piloto',published.append,store,capacity=Mock(side_effect=AssertionError('No reservar retry')))
        assert again==result and len(published)==1
        assert s.get(CommercialIntent,result.instance_id).specification==frozen
        assert s.get(CommercialIntent,result.instance_id).execution_binding==binding
        assert s.scalar(select(func.count()).select_from(Tenant))==tenants
        for table in Instance.metadata.sorted_tables:
            data=json.dumps([dict(x) for x in s.execute(table.select()).mappings()],default=str)
            assert spec.access.root_password.get_secret_value() not in data+caplog.text


def test_full_request_fingerprint_is_exactly_the_preexisting_algorithm(sessions,commercial_config,store):
    spec=specification();values=spec.model_dump(mode='json',exclude={'access'})
    canonical={**values,'access_fingerprint':store.fingerprint(json.dumps({'password':spec.access.root_password.get_secret_value(),
        'keys':spec.access.ssh_public_keys},sort_keys=True,separators=(',',':')))}
    old_digest=hashlib.sha256(json.dumps(canonical,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    with sessions() as s:
        result=create(s,Principal(BOOTSTRAP_TENANT_ID,'crm','operator'),spec,'full',lambda _:None,store,capacity=lambda *_:None)
        assert s.get(Operation,result.operation_id).request_hash==old_digest
        assert 'pilot_defaults' not in s.get(CommercialIntent,result.instance_id).specification
        assert create(s,Principal(BOOTSTRAP_TENANT_ID,'crm','operator'),spec,'full',Mock(side_effect=AssertionError()),store)==result


@pytest.mark.parametrize('updates',[
    {'plan_id':'kvm-2'}, {'name':'otro-nombre'}, {'start_on_create':False},
    {'network':{'profile_id':'lab-dhcp'}}, {'commercial_context':{'order_id':'otro-pedido'}},
    {'access':{'root_password':'OTRA-CONTRASENA-NO-PERSISTIR-2026'}}])
def test_changed_intent_conflicts_without_second_publish(sessions,commercial_config,store,updates):
    body=deepcopy(MINIMAL_EXAMPLE);body['access']['root_password']='SENUELO-PILOTO-NO-PERSISTIR-2026'
    with sessions() as s:
        published=[];p=Principal(BOOTSTRAP_TENANT_ID,'crm','operator')
        create(s,p,CommercialInstanceCreate.model_validate(body),'same',published.append,store,capacity=lambda *_:None)
        changed=CommercialInstanceCreate.model_validate({**body,**updates})
        with pytest.raises(Conflict):create(s,p,changed,'same',published.append,store,capacity=lambda *_:None)
        assert len(published)==1 and len(s.scalars(select(Instance)).all())==1


@pytest.mark.parametrize('field',['network','commercial_context'])
def test_omission_does_not_accept_explicit_null(field):
    with pytest.raises(ValidationError):minimal(**{field:None})


@pytest.mark.parametrize('scope',['big-plan','outside-lab'])
def test_defaults_never_authorize_bigger_plans_or_non_lab(sessions,commercial_config,store,monkeypatch,scope):
    from iacp.services import commercial
    if scope=='big-plan':
        limits=commercial.limits_for(BOOTSTRAP_TENANT_ID).model_copy(update={'allowed_plans':['kvm-3']})
        monkeypatch.setattr(commercial,'limits_for',lambda _:limits)
    else:
        p=commercial.authorized_profile(BOOTSTRAP_TENANT_ID)
        p=p.model_copy(update={'settings':p.settings.model_copy(update={'environment':'production'})})
        monkeypatch.setattr(commercial,'authorized_profile',lambda *args:p)
    with sessions() as s:
        with pytest.raises(Conflict,match='PILOT_DEFAULTS_NOT_AUTHORIZED'):
            create(s,Principal(BOOTSTRAP_TENANT_ID,'crm','operator'),minimal(plan_id='kvm-3' if scope=='big-plan' else 'kvm-1'),'big',Mock(side_effect=AssertionError()),store)
        assert not s.scalars(select(Instance)).all() and not store.redis.values


def test_http_service_token_uses_one_persistent_tenant_and_preserves_security(
        sessions,commercial_config,store,identities,monkeypatch,caplog):
    from fastapi.testclient import TestClient
    from iacp.main import create_app
    from iacp.db import get_session
    from iacp.api.dependencies import get_instance_publisher
    from iacp.services import commercial
    from pathlib import Path
    import os
    tenant=identities['tenant-a-operator']['tenant_id']
    # Derechos y perfil explícitos del fixture para una identidad de servicio registrada.
    raw=json.loads(commercial_config.read_text());raw['profiles']['lab-dhcp']['allowed_tenants']=[tenant];commercial_config.write_text(json.dumps(raw))
    limits=Path(os.environ['IACP_COMMERCIAL_LIMITS_FILE']);raw=json.loads(limits.read_text());raw['tenants'][tenant]=raw['tenants'].pop(BOOTSTRAP_TENANT_ID);limits.write_text(json.dumps(raw))
    monkeypatch.setenv('IACP_CREATION_CONTRACT','commercial-g')
    monkeypatch.setattr(commercial,'live_capacity',lambda *_:None)
    monkeypatch.setattr(commercial.EphemeralSecrets,'configured',classmethod(lambda _:store))
    app=create_app()
    def session_override():
        with sessions() as s:yield s
    app.dependency_overrides[get_session]=session_override
    published=[];app.dependency_overrides[get_instance_publisher]=lambda:published.append
    body=minimal().model_dump(mode='json',exclude={'access','network','commercial_context'})
    body['access']={'root_password':'SENUELO-PILOTO-NO-PERSISTIR-2026','ssh_public_keys':[]}
    headers={**identities['tenant-a-operator']['headers'],'Idempotency-Key':'service-intent'}
    with sessions() as s:tenants=s.scalar(select(func.count()).select_from(Tenant))
    with TestClient(app,base_url='https://fixture') as client:
        assert client.post('/v1/instances',json=body).status_code==401
        assert client.post('/v1/instances',json=body,headers={**identities['tenant-a-reader']['headers'],'Idempotency-Key':'reader'}).status_code==403
        assert client.post('/v1/instances',json={**body,'name':'INVALID NAME'},headers={**identities['tenant-a-operator']['headers'],'Idempotency-Key':'invalid-preflight'}).status_code==422
        for extra in [{'tenant_id':tenant},{'vcpus':8},{'pilot_defaults_revision':'otra'}]:
            assert client.post('/v1/instances',json={**body,**extra},headers=headers).status_code==422
        assert client.post('/v1/instances',json={**body,'network':{'profile_id':'wan-static','ipv4_address':'192.0.2.10','prefix_length':24,'gateway':'192.0.2.1'}},headers=headers).status_code==409
        first=client.post('/v1/instances',json=body,headers=identities['tenant-a-operator']['headers']);assert first.status_code==202,first.text
        generated_key=first.headers.get('Idempotency-Key');assert generated_key and generated_key.startswith('http-create-')
        assert client.post('/v1/instances',json=body,headers={**headers,'Idempotency-Key':generated_key}).json()==first.json()
        denied=client.post('/v1/instances',json=body,headers={**identities['tenant-b-operator']['headers'],'Idempotency-Key':'service-intent'})
        assert denied.status_code==409
        assert client.get('/v1/instances/'+first.json()['instance_id'],headers=identities['tenant-b-operator']['headers']).status_code==404
        own=client.get('/v1/instances/'+first.json()['instance_id'],headers=headers);assert own.status_code==200
        assert own.json()['tenant_id']==tenant and own.json()['commercial_context'] is None
        assert set(first.json())=={'instance_id','status','operation_id','job_id','executor','provisioning_service'}
        assert body['access']['root_password'] not in first.text+own.text+caplog.text
        assert client.post('/v1/instances',json={**body,'name':'otra'},headers={**headers,'Idempotency-Key':'second'}).status_code==409
        with sessions() as s:
            assert s.scalar(select(func.count()).select_from(Tenant))==tenants
            t=s.get(Tenant,tenant);t.active=False;s.commit()
        assert client.post('/v1/instances',json=body,headers=headers).status_code==403
    assert len(published)==1


def test_openapi_change_is_only_compatible_omission_and_documentation(monkeypatch):
    from pathlib import Path
    from iacp.main import create_app
    from scripts.openapi_contract import transport_projection
    from jsonschema import Draft202012Validator, RefResolver
    from openapi_spec_validator import validate
    monkeypatch.setenv('IACP_CREATION_CONTRACT','commercial-g')
    current=create_app().openapi();validate(current)
    create_operation=current['paths']['/v1/instances']['post']
    assert any(item['name']=='Idempotency-Key' and not item['required'] for item in create_operation['parameters'])
    assert create_operation['responses']['202']['headers']['Idempotency-Key']['schema']['type']=='string'
    baseline=json.loads((Path(__file__).resolve().parents[1]/'docs/validation/docs-promocion-https-20261025/openapi-despues.json').read_text())['datos']
    name='CommercialInstanceCreate';schema=current['components']['schemas'][name];old=baseline['components']['schemas'][name]
    assert set(schema['required'])=={'name','plan_id','image_id','access'}
    resolver=RefResolver.from_schema(current)
    validator=Draft202012Validator(schema,resolver=resolver)
    validator.validate(MINIMAL_EXAMPLE)
    for field in ['network','commercial_context']:
        assert list(validator.iter_errors({**MINIMAL_EXAMPLE,field:None}))
    adjusted=deepcopy(current);node=adjusted['components']['schemas'][name]
    node['required']=old['required'];node['examples']=old['examples']
    for field in ['network','commercial_context']:
        prop=node['properties'][field];prop.pop('description')
        assert prop==old['properties'][field]
    for path, method in [('/v1/instances','post'),
                         ('/v1/instances/{instance_id}/power/start','post'),
                         ('/v1/instances/{instance_id}/power/shutdown','post'),
                         ('/v1/instances/{instance_id}/power/reboot','post'),
                         ('/v1/instances/{instance_id}','delete')]:
        adjusted['paths'][path][method]['responses']['202'].pop('headers', None)
        for schema in (adjusted, baseline):
            for parameter in schema['paths'][path][method].get('parameters', []):
                if parameter.get('name') == 'Idempotency-Key':
                    parameter['required'] = False
    assert transport_projection(adjusted)==transport_projection(baseline)


def test_same_key_in_two_authorized_tenants_keeps_separate_intents(sessions,commercial_config,store,identities):
    from pathlib import Path
    import os
    a=identities['tenant-a-operator']['tenant_id'];b=identities['tenant-b-operator']['tenant_id']
    raw=json.loads(commercial_config.read_text());raw['profiles']['lab-dhcp']['allowed_tenants']=[a,b];commercial_config.write_text(json.dumps(raw))
    path=Path(os.environ['IACP_COMMERCIAL_LIMITS_FILE']);raw=json.loads(path.read_text());limits=raw['tenants'].pop(BOOTSTRAP_TENANT_ID);raw['tenants']={a:limits,b:limits};path.write_text(json.dumps(raw))
    with sessions() as s:
        count=s.scalar(select(func.count()).select_from(Tenant));published=[]
        one=create(s,Principal(a,'a','operator'),minimal(),'same',published.append,store,capacity=lambda *_:None)
        two=create(s,Principal(b,'b','operator'),minimal(),'same',published.append,store,capacity=lambda *_:None)
        assert one.instance_id!=two.instance_id and one.operation_id!=two.operation_id
        assert len(published)==2 and s.scalar(select(func.count()).select_from(Tenant))==count
        assert s.get(Instance,one.instance_id).tenant_id==a and s.get(Instance,two.instance_id).tenant_id==b
