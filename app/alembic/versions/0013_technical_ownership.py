"""Materializa ownership técnico sin consultar políticas históricas."""

from alembic import op
import sqlalchemy as sa

from iacp.services.ownership_binding import binding_digest, valid_source_revision


revision = "0013_technical_ownership"
down_revision = "0012_commercial_intents"
branch_labels = None
depends_on = None


def _rows(connection):
    instances = sa.table(
        "instances",
        sa.column("id", sa.String()), sa.column("tenant_id", sa.String()),
        sa.column("name", sa.String()), sa.column("image", sa.String()),
        sa.column("vcpus", sa.Integer()), sa.column("memory_mb", sa.Integer()),
        sa.column("disk_gb", sa.Integer()), sa.column("network_profile", sa.String()),
        sa.column("node_preference", sa.String()), sa.column("start_on_create", sa.Boolean()),
        sa.column("status", sa.String()), sa.column("executor", sa.String()),
        sa.column("vmid", sa.Integer()), sa.column("node", sa.String()),
        sa.column("result", sa.JSON()),
    )
    executions = sa.table(
        "terraform_executions",
        sa.column("instance_id", sa.String()), sa.column("attempt", sa.Integer()),
        sa.column("state_key", sa.String()), sa.column("vmid", sa.Integer()),
        sa.column("allocation", sa.JSON()), sa.column("source_revision", sa.String()),
        sa.column("plan_sha256", sa.String()), sa.column("status", sa.String()),
    )
    applies = sa.table(
        "terraform_applies",
        sa.column("id", sa.String()), sa.column("instance_id", sa.String()),
        sa.column("attempt", sa.Integer()), sa.column("plan_sha256", sa.String()),
        sa.column("status", sa.String()), sa.column("result", sa.JSON()),
    )
    return connection.execute(
        sa.select(
            instances.c.id.label("instance_id"), instances.c.tenant_id.label("tenant_id"),
            instances.c.name.label("instance_name"), instances.c.image.label("image"),
            instances.c.vcpus.label("vcpus"), instances.c.memory_mb.label("memory_mb"),
            instances.c.disk_gb.label("disk_gb"), instances.c.network_profile.label("network_profile"),
            instances.c.node_preference.label("node_preference"),
            instances.c.start_on_create.label("start_on_create"),
            instances.c.status.label("instance_status"), instances.c.executor.label("executor"),
            instances.c.vmid.label("instance_vmid"), instances.c.node.label("instance_node"),
            instances.c.result.label("instance_result"),
            executions.c.attempt.label("execution_attempt"), executions.c.state_key.label("state_key"),
            executions.c.vmid.label("execution_vmid"), executions.c.allocation.label("allocation"),
            executions.c.source_revision.label("source_revision"),
            executions.c.plan_sha256.label("execution_plan_sha256"),
            applies.c.id.label("apply_id"), applies.c.attempt.label("apply_attempt"),
            applies.c.plan_sha256.label("apply_plan_sha256"), applies.c.result.label("apply_result"),
        )
        .select_from(instances.join(executions, executions.c.instance_id == instances.c.id)
                     .join(applies, sa.and_(applies.c.instance_id == executions.c.instance_id,
                                            applies.c.attempt == executions.c.attempt,
                                            applies.c.plan_sha256 == executions.c.plan_sha256)))
        .where(instances.c.status != "deleted", instances.c.executor == "terraform",
               executions.c.status == "planned", applies.c.status == "completed")
    ).mappings()


def upgrade():
    op.create_table(
        "technical_ownership",
        sa.Column("instance_id", sa.String(36), sa.ForeignKey("instances.id"), primary_key=True),
        sa.Column("tenant_id", sa.String(36), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("vmid", sa.Integer(), nullable=False),
        sa.Column("node", sa.String(128), nullable=False),
        sa.Column("name", sa.String(63), nullable=False),
        sa.Column("state_key", sa.String(512), nullable=False),
        sa.Column("source_revision", sa.String(40), nullable=False),
        sa.Column("binding_sha256", sa.String(64), nullable=False),
        sa.Column("allocation", sa.JSON(), nullable=False),
        sa.Column("evidence", sa.JSON(), nullable=False),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("tenant_id", "state_key", name="uq_technical_ownership_tenant_state"),
        sa.UniqueConstraint("vmid", name="uq_technical_ownership_vmid"),
        sa.CheckConstraint("vmid >= 30000 AND vmid NOT IN (104,106,29000)",
                          name="ck_technical_ownership_disposable_vmid"),
    )
    op.create_index("ix_technical_ownership_tenant_id", "technical_ownership", ["tenant_id"])
    connection = op.get_bind()
    from datetime import datetime, timezone
    from types import SimpleNamespace
    import json
    now = datetime.now(timezone.utc)
    for row in _rows(connection):
        values = row._mapping
        instance = SimpleNamespace(
            id=values["instance_id"], tenant_id=values["tenant_id"], name=values["instance_name"],
            image=values["image"], vcpus=values["vcpus"], memory_mb=values["memory_mb"],
            disk_gb=values["disk_gb"], network_profile=values["network_profile"],
            node_preference=values["node_preference"], start_on_create=values["start_on_create"],
            vmid=values["instance_vmid"], node=values["instance_node"],
            result=values["instance_result"], executor=values["executor"],
        )
        execution = SimpleNamespace(
            instance_id=instance.id, attempt=values["execution_attempt"],
            state_key=values["state_key"], vmid=values["execution_vmid"],
            allocation=values["allocation"], source_revision=values["source_revision"],
            plan_sha256=values["execution_plan_sha256"],
        )
        apply = SimpleNamespace(
            id=values["apply_id"], instance_id=instance.id, attempt=values["apply_attempt"],
            plan_sha256=values["apply_plan_sha256"], result=values["apply_result"],
        )
        result = apply.result if isinstance(apply.result, dict) else {}
        observation = result.get("observation") if isinstance(result.get("observation"), dict) else {}
        allocation = execution.allocation if isinstance(execution.allocation, dict) else {}
        if (not execution.state_key or not valid_source_revision(execution.source_revision)
                or execution.vmid is None or execution.vmid < 30000
                or execution.vmid in {104, 106, 29000}
                or instance.vmid != execution.vmid or instance.node != result.get("node")
                or instance.name != result.get("name") or result.get("simulated") is not False
                or observation.get("postconditions_verified") is not True
                or observation.get("state_present") is not True
                or observation.get("proxmox_present") is not True
                or allocation.get("vm_id") != execution.vmid
                or allocation.get("node_name") != instance.node
                or allocation.get("image") != instance.image):
            continue
        digest = binding_digest(instance, execution, apply)
        exists = connection.execute(sa.text(
            """
            SELECT 1 FROM technical_ownership
             WHERE instance_id = :instance_id
                OR vmid = :vmid
                OR (tenant_id = :tenant_id AND state_key = :state_key)
            """), {
                "instance_id": instance.id, "vmid": execution.vmid,
                "tenant_id": instance.tenant_id, "state_key": execution.state_key,
            }).first()
        if exists:
            continue
        connection.execute(sa.text("""
            INSERT INTO technical_ownership
              (instance_id, tenant_id, vmid, node, name, state_key, source_revision,
               binding_sha256, allocation, evidence, verified_at, created_at, updated_at)
            VALUES
              (:instance_id, :tenant_id, :vmid, :node, :name, :state_key, :source_revision,
               :binding_sha256, :allocation, :evidence, :verified_at, :created_at, :updated_at)
        """), {
            "instance_id": instance.id, "tenant_id": instance.tenant_id,
            "vmid": execution.vmid, "node": instance.node, "name": instance.name,
            "state_key": execution.state_key, "source_revision": execution.source_revision,
            "binding_sha256": digest,
            "allocation": json.dumps(allocation),
            "evidence": json.dumps({"apply_id": apply.id,
                "attempt": apply.attempt, "plan_sha256": apply.plan_sha256}),
            "verified_at": now, "created_at": now, "updated_at": now,
        })

    # Los registros de operaciones se conservan íntegros, pero su fence de
    # pertenencia pasa a la huella técnica única. Esto permite reanudar una
    # observación histórica sin volver a consultar ni fabricar LabPolicy.
    for table in ("network_discoveries", "guest_initializations", "deletion_executions"):
        connection.execute(sa.text(f"""
            UPDATE {table}
               SET provisioning_binding = (
                   SELECT binding_sha256 FROM technical_ownership
                    WHERE technical_ownership.instance_id = {table}.instance_id
               )
             WHERE provisioning_binding IS NOT NULL
               AND EXISTS (
                   SELECT 1 FROM technical_ownership
                    WHERE technical_ownership.instance_id = {table}.instance_id
               )
        """))


def downgrade():
    raise RuntimeError("Downgrade deshabilitado: conservar autorizaciones, ownership técnico y auditoría")

