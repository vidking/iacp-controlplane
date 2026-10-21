"""Contrato de ejecución sin acceso a infraestructura en el entregable 2A."""
from dataclasses import dataclass
import os
from typing import Protocol

from .schemas import InstanceCreate


@dataclass(frozen=True)
class ExecutionRequest:
    instance_id: str
    operation_id: str
    tenant_id: str
    spec: InstanceCreate


@dataclass(frozen=True)
class ExecutionResult:
    details: dict


class IaCExecutor(Protocol):
    def create(self, request: ExecutionRequest) -> ExecutionResult: ...


class ExecutorUnavailable(Exception):
    pass


class SimulatedExecutor:
    def create(self, request: ExecutionRequest) -> ExecutionResult:
        return ExecutionResult(details={"simulated": True, "instance_id": request.instance_id})


def get_executor() -> IaCExecutor:
    if os.environ.get("IACP_EXECUTOR", "simulated") != "simulated":
        raise ExecutorUnavailable("Only the simulated executor is available")
    return SimulatedExecutor()
