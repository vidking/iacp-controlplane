"""Worker exclusivo de pruebas: el broker y PostgreSQL son reales; Terraform es simulado."""
from iacp.terraform.worker import celery_app
from iacp.terraform import lifecycle
from tests.test_terraform import FakeExecutor

lifecycle.build_executor = FakeExecutor

from iacp.terraform import apply
from tests.test_apply import ApplyingExecutor
apply.build_executor = ApplyingExecutor
