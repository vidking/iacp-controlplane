"""Real Celery/Redis/PostgreSQL, synthetic provider; never connects to Proxmox."""
from types import SimpleNamespace

from iacp.terraform import lifecycle, apply, deletion
from iacp.terraform.worker import celery_app
from tests.test_terraform import FakeExecutor
from tests.test_lab_self_service import LabExecutor
from tests.test_deletion import FakeDeletion

lifecycle.build_executor=lambda **kwargs:FakeExecutor()
apply.build_executor=lambda **kwargs:LabExecutor()
deletion.build_executor=lambda **kwargs:SimpleNamespace(source=None,artifacts=None,runner=None)
deletion.DeletionExecutor=lambda *args:FakeDeletion()
celery_app.worker_main(['worker','--pool=solo','--concurrency=1','-Q','terraform',
    '--without-gossip','--without-mingle','--without-heartbeat','--loglevel=WARNING'])
