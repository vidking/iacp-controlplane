"""Disposable integration worker: real broker/DB, explicitly fake infrastructure."""
from types import SimpleNamespace
from iacp.terraform import deletion
from iacp.terraform.worker import celery_app
from tests.test_deletion import FakeDeletion

deletion.build_executor=lambda **kwargs:SimpleNamespace(source=None,artifacts=None,runner=None)
deletion.DeletionExecutor=lambda *args:FakeDeletion()
celery_app.worker_main(['worker','--pool=solo','--concurrency=1','-Q','terraform','--without-gossip','--without-mingle','--without-heartbeat','--loglevel=WARNING'])
