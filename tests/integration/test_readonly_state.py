"""Recovery con PostgreSQL real y S3 en loopback; CLI sin provider externo."""
from copy import deepcopy
import json
import os
import shutil

import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from iacp.models import OperationEvent, TerraformApply
from iacp.terraform.apply import execute_apply
from iacp.terraform.runner import CommandRunner
from iacp.terraform.state import S3StateReader
from tests.helpers import upgrade
from tests.s3_fixture import versioned_s3
from tests.test_apply import prepared
from tests.test_historical_recovery import historical_run
from tests.test_terraform import terraform_mode

pytestmark = pytest.mark.integration
ENV = {"AWS_ACCESS_KEY_ID": "synthetic-key", "AWS_SECRET_ACCESS_KEY": "synthetic-secret"}


@pytest.fixture
def sessions(pg_engine):
    upgrade(pg_engine)
    return sessionmaker(bind=pg_engine, autoflush=False, expire_on_commit=False)


@pytest.mark.parametrize("external_writer", [False, True])
def test_recovery_preserves_s3_version_or_blocks_an_external_write(
        client, auth, prepared, historical_run, sessions, monkeypatch, external_writer, record_property):
    row, executor, runner, workspace, root, args = historical_run
    monkeypatch.setenv("IACP_TERRAFORM_APPLY_ENABLED", "1")
    before = json.dumps(runner.state()).encode()
    with versioned_s3(before, args[4]["backend"]["bucket"], args[3]) as store:
        def reader(backend, env):
            return S3StateReader(backend | {"endpoints": {"s3": store.endpoint}}, ENV)
        monkeypatch.setattr("iacp.terraform.executor.S3StateReader", reader)
        original = runner.run
        def refreshed(command, *a, **kw):
            result = original(command, *a, **kw)
            if external_writer and command[0] == "plan":
                changed = deepcopy(runner.state())
                changed["serial"] += 1
                store.external_write(json.dumps(changed).encode())
            return result
        monkeypatch.setattr(runner, "run", refreshed)
        path = prepared[2] + "/applies/" + row["approval_id"]
        assert client.post(path + "/recover", headers=auth).status_code == 202
        execute_apply(row["approval_id"], sessions, executor, reconcile=True)
        with sessions() as session:
            current = session.get(TerraformApply, row["approval_id"])
            if external_writer:
                assert current.status == "uncertain" and current.error_code == "STATE_CHANGED_DURING_OBSERVATION"
                assert current.result["diagnostic"]["state_consistency"]["change"] == "serial_advanced"
                assert session.scalar(select(OperationEvent.id).where(OperationEvent.code == "terraform_apply_completed")) is None
                consistency = current.result["diagnostic"]["state_consistency"]
            else:
                assert current.status == "completed" and current.error_code is None
                consistency = current.result["observation"]["state_consistency"]
                assert consistency["before"] == consistency["after"]
                assert consistency["before"]["version_id"] == "fixture-version-1"
                assert store.data == before and store.version == 1
            record_property("state_consistency", json.dumps(consistency, sort_keys=True))
            record_property("recovery_status", current.status)
        assert store.requests == [("GET", store.state_path), ("GET", store.state_path)]
        record_property("s3_requests", json.dumps(store.requests))
        record_property("apply_enabled_during_recovery", os.environ["IACP_TERRAFORM_APPLY_ENABLED"])
        runner.apply_saved.assert_not_called()


def test_real_refresh_only_cli_preserves_remote_object_and_uses_native_lock(tmp_path, monkeypatch, record_property):
    binary = shutil.which("terraform")
    assert binary, "La imagen de pruebas debe incluir Terraform 1.16.6"
    monkeypatch.setenv("IACP_TERRAFORM_APPLY_ENABLED", "1")
    state = {"version": 4, "terraform_version": "1.16.6", "serial": 3,
             "lineage": "d954af88-6ae6-2d04-9ead-55ed1f95d644", "outputs": {},
             "resources": [{"module": "module.kvm", "mode": "managed", "type": "terraform_data", "name": "this",
                            "provider": 'provider["terraform.io/builtin/terraform"]', "instances": [{"schema_version": 0,
                            "attributes": {"id": "11111111-1111-4111-8111-111111111111",
                                           "input": {"value": "synthetic", "type": "string"},
                                           "output": {"value": "synthetic", "type": "string"}, "triggers_replace": None},
                            "sensitive_attributes": []}]}]}
    data = json.dumps(state).encode()
    (tmp_path / "kvm").mkdir()
    (tmp_path / "main.tf").write_text('terraform {\n backend "s3" {}\n}\nmodule "kvm" { source = "./kvm" }\n')
    (tmp_path / "kvm/main.tf").write_text('resource "terraform_data" "this" { input = "synthetic" }\n')
    env = {"PATH": os.environ["PATH"], "HOME": str(tmp_path), "TF_DATA_DIR": str(tmp_path / "data"),
           "TF_WORKSPACE": "default", "TF_INPUT": "0", "TF_IN_AUTOMATION": "1", "CHECKPOINT_DISABLE": "1",
           "AWS_EC2_METADATA_DISABLED": "true", **ENV}
    cli = CommandRunner(binary=(binary,), timeout=60)
    def run(*args):
        return cli.run(list(args), tmp_path, env, lambda: False)
    assert json.loads(run("version", "-json"))["terraform_version"] == "1.16.6"
    with versioned_s3(data) as store:
        backend = {"bucket": store.bucket, "key": store.key, "region": "us-east-1",
                   "endpoints": {"s3": store.endpoint}, "use_lockfile": True, "use_path_style": True,
                   "skip_credentials_validation": True, "skip_requesting_account_id": True,
                   "skip_metadata_api_check": True}
        (tmp_path / "backend.tfbackend.json").write_text(json.dumps(backend))
        reader = S3StateReader(backend, ENV)
        try:
            before = reader.read(lambda: False)
            run("init", "-input=false", "-no-color", "-lockfile=readonly", "-reconfigure",
                "-backend-config=backend.tfbackend.json")
            run("plan", "-refresh-only", "-input=false", "-no-color", "-lock-timeout=5s", "-out=observation.tfplan")
            observed = json.loads(run("show", "-json", "observation.tfplan"))
            assert observed["complete"] is True and observed["errored"] is False
            resource = observed["prior_state"]["values"]["root_module"]["child_modules"][0]["resources"][0]
            assert resource["address"] == "module.kvm.terraform_data.this"
            after = reader.read(lambda: False)
            assert before.verify(after)["change"] == "unchanged"
            assert before.metadata() == after.metadata()
            assert store.data == data and store.version == 1
            writes = [(method, path) for method, path in store.requests if method not in {"GET", "HEAD"}]
            assert writes == [("PUT", store.state_path + ".tflock"), ("DELETE", store.state_path + ".tflock")]
            assert store.lock is None
            record_property("state_consistency", json.dumps(before.verify(after), sort_keys=True))
            record_property("s3_writes", json.dumps(writes))
            record_property("terraform_version", "1.16.6")
        finally:
            reader.close()
