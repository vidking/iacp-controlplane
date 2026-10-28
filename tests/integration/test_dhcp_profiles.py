"""Pruebas históricas del flujo de perfiles LAB, retiradas del producto activo."""
import pytest

pytest.skip("El flujo de perfiles LAB fue retirado; usar las regresiones Core de red y ownership", allow_module_level=True)

from concurrent.futures import ThreadPoolExecutor
import json
from threading import Barrier
from unittest.mock import Mock
import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker
from iacp.models import BOOTSTRAP_TENANT_ID,TerraformExecution,Instance,Operation
from iacp.schemas import InstanceCreate
from iacp.security import Principal
from iacp.services.instances import create_instance,Conflict
from iacp.services.network import request_discovery,execute_discovery
from iacp.terraform import profiles
from tests.helpers import upgrade
from tests.test_dhcp_profiles import profile_data
from tests.power_helpers import managed_instance

pytestmark=pytest.mark.integration


def test_overlapping_profiles_serialize_global_vmid_reservations(pg_engine,tmp_path,monkeypatch):
    upgrade(pg_engine);sessions=sessionmaker(bind=pg_engine,autoflush=False,expire_on_commit=False)
    data=profile_data();data['profiles']['second']=data['profiles']['lab-dhcp']
    path=tmp_path/'profiles.json';path.write_text(json.dumps(data))
    monkeypatch.setenv('IACP_TERRAFORM_PROFILES_FILE',str(path));monkeypatch.setenv('IACP_INSTANCE_EXECUTOR','terraform')
    monkeypatch.setattr(profiles,'cluster_vmids',lambda:set())
    barrier=Barrier(2);principal=Principal(BOOTSTRAP_TENANT_ID,'fixture','operator')
    def submit(n,wait=True):
        if wait:barrier.wait(timeout=10)
        with sessions() as s:
            try:return create_instance(s,principal,InstanceCreate(name='parallel-'+str(n),image=data['profiles']['lab-dhcp']['settings']['image'],network_profile=['lab-dhcp','second'][n]),'profile-'+str(n),lambda _:None)
            except Conflict:return None
    with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(submit,(0,1)))
    for n,result in enumerate(results):
        if result is None:assert submit(n,False) is not None
    with sessions() as s:assert set(s.scalars(select(TerraformExecution.vmid)))=={30000,30001}


def test_discovery_dispatches_real_redis_message_and_persists_pending_without_failing_guest(pg_engine,runtime,monkeypatch):
    import os
    from kombu import Connection
    from iacp.api.dependencies import get_network_publisher
    from iacp.worker import celery_app
    upgrade(pg_engine);sessions=sessionmaker(bind=pg_engine,expire_on_commit=False,autoflush=False)
    identifier=managed_instance(sessions,BOOTSTRAP_TENANT_ID,vmid=30000)
    with sessions() as s:s.get(Instance,identifier).network_mode='dhcp';s.commit()
    celery_app.conf.broker_url=os.environ['CELERY_BROKER_URL']
    with Connection(os.environ['CELERY_BROKER_URL']) as connection:
        queue=connection.SimpleQueue('terraform');queue.clear()
        try:
            with sessions() as s:
                result=request_discovery(s,s.get(Instance,identifier),Principal(BOOTSTRAP_TENANT_ID,'fixture','operator'),'discover-pg',get_network_publisher())
            message=queue.get(timeout=10)
            assert message.headers['task']=='iacp.network.discover'
            assert message.payload[0]==[result['operation_id']]
            message.ack()
        finally:queue.close()
    observer=Mock(return_value={'status':'pending','source':'qemu-guest-agent','ip_addresses':[]})
    execute_discovery(result['operation_id'],sessions,observer,timeout=0,pause=lambda _:None)
    with sessions() as s:
        assert s.get(Operation,result['operation_id']).status=='failed'
        assert s.get(Instance,identifier).status=='ready'
        assert s.get(Instance,identifier).network_observation['status']=='pending'
