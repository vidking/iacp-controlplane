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
    assert after["schema"] == ["0006_power_ownership"]
    for table, fingerprint in before["tables"].items():
        assert after["tables"][table] == fingerprint
    assert after["tables"]["power_executions"]["count"] == 0
    assert after["tables"]["power_ownership_certificates"]["count"] == 0
