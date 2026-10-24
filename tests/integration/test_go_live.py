import importlib.util
from pathlib import Path

import pytest

from tests.helpers import upgrade

pytestmark = pytest.mark.integration


def test_readonly_promotion_snapshot_survives_both_real_migrations(pg_engine, runtime):
    root = Path(__file__).resolve().parents[2]
    spec = importlib.util.spec_from_file_location("runtime_snapshot", root / "scripts/runtime_snapshot.py")
    inspector = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(inspector)
    inspector.get_engine = lambda: pg_engine
    upgrade(pg_engine, "0004_authorized_apply")
    before = inspector.snapshot()
    assert before["schema"] == ["0004_authorized_apply"]
    upgrade(pg_engine)
    after = inspector.snapshot()
    assert after["schema"] == ["0010_guest_identity"]
    for table, fingerprint in before["tables"].items():
        actual = dict(after["compatibility"]["instances_before_0008"]) if table == "instances" else after["tables"][table]
        actual.pop("new_fields_default", None)
        assert actual == fingerprint
    assert after["tables"]["power_executions"]["count"] == 0
    assert after["tables"]["power_ownership_certificates"]["count"] == 0


def test_0010_preserves_all_0009_rows_and_does_not_initialize_existing_guests(pg_engine,runtime):
    from sqlalchemy.orm import sessionmaker
    from tests.power_helpers import managed_instance
    from iacp.models import BOOTSTRAP_TENANT_ID
    import importlib.util
    root=Path(__file__).resolve().parents[2]
    spec=importlib.util.spec_from_file_location('snapshot_identity',root/'scripts/runtime_snapshot.py')
    inspector=importlib.util.module_from_spec(spec);spec.loader.exec_module(inspector)
    inspector.get_engine=lambda:pg_engine
    spec=importlib.util.spec_from_file_location('driver_identity',root/'scripts/go_live.py')
    driver=importlib.util.module_from_spec(spec);spec.loader.exec_module(driver)
    upgrade(pg_engine,'0009_lab_self_service')
    managed_instance(sessionmaker(bind=pg_engine,expire_on_commit=False),BOOTSTRAP_TENANT_ID,vmid=30000)
    before=inspector.snapshot();upgrade(pg_engine);upgrade(pg_engine);after=inspector.snapshot()
    driver.compare_history(before,after)
    assert after['schema']==['0010_guest_identity']
    assert after['tables']['guest_initializations']['count']==0
