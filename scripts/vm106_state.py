"""Read only state metadata and allocation, never emit state/credentials."""
import json
import os
from pathlib import Path
import tempfile

from iacp.db import get_session_factory
from iacp.models import TerraformExecution
from iacp.terraform.config import Allocation
from iacp.terraform.executor import TerraformExecutor
from iacp.terraform.state import S3StateReader

with get_session_factory()() as session:
    execution = session.get(TerraformExecution, "a1b2d063-6240-4969-bc04-e7e0e6be9e73")
    allocation = Allocation.model_validate(execution.allocation)
    executor = TerraformExecutor(Path(os.environ["IACP_TERRAFORM_SOURCE_DIR"]), Path(os.environ["IACP_TERRAFORM_ARTIFACTS_DIR"]))
    with tempfile.TemporaryDirectory() as directory:
        reader = S3StateReader(allocation.backend.arguments(execution.state_key), executor.environment(Path(directory)))
        try:
            first = reader.read(lambda: False)
            first.verify(reader.read(lambda: False))
            print(json.dumps({"state": first.metadata(), "assigned_ipv4_cidr": allocation.ipv4_cidr}))
        finally:
            reader.close()
