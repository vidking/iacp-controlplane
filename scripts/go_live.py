#!/usr/bin/env python3
"""Driver de promoción y mantenimiento del IACP Control Plane.

En producción, las acciones siguen requiriendo registros de cambio revisados.
En el LAB MNG autorizado, el perfil privado del host habilita únicamente las
acciones operativas explícitas y genera su propia evidencia auditable. La fase
first-start usa IACP; una promoción fallida nunca reabre admisión por sí sola.
"""
import argparse
from datetime import datetime, timezone
import fcntl
import hashlib
import ipaddress
import json
import os
import pwd
from pathlib import Path
import re
import socket
import ssl
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from smoke_public import exposure_mode, internal_target, resolved_addresses

HEAD = "0014_allow_historical_kvm_106"
SOURCE = "b517c9bb67a9e3c800f0c3657c56224c2b8f80aa"
ARCHIVE = "68ddafc66df5a96cd3e22165658a273446d9d0f9fb46115f2e065034c2ddf2a3"
ROLES = ["api", "worker", "terraform-worker", "lifecycle-worker"]
HOST_EXECUTION_PROFILE = Path("/etc/iacp/execution-profile.json")
LAB_AUTOMATED_ACTIONS = frozenset({
    "deploy", "promote-env", "recover-queued-guest-init",
    "migrate-technical-ownership",
})


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def require_supported_schema(schema):
    supported = {'0004_authorized_apply', '0005_kvm_lifecycle', '0006_power_ownership',
        '0007_safe_deletion', '0008_dhcp_profiles', '0009_lab_self_service', '0010_guest_identity',
        '0011_crm_contract', '0012_commercial_intents', '0013_technical_ownership', HEAD}
    require(len(schema) == 1 and schema[0] in supported, 'Unsupported schema')


def check_restore_evidence(backup, restored):
    """Aceptar respaldos comprobados de un esquema admitido sin confundirlos con HEAD."""
    schema = backup.get('schema', '0004_authorized_apply')
    minimum_tables = {'0004_authorized_apply': 9, '0005_kvm_lifecycle': 10,
        '0006_power_ownership': 11, '0007_safe_deletion': 12, '0008_dhcp_profiles': 13,
        '0009_lab_self_service': 14, '0010_guest_identity': 15,
        '0011_crm_contract': 15, '0012_commercial_intents': 16,
        '0013_technical_ownership': 17, HEAD: 17}
    require(restored.get('passed') is True and restored.get('isolated') is True
        and restored.get('backup_sha256') == backup['sha256']
        and restored.get('schema') == schema and schema in minimum_tables
        and restored.get('table_count', 0) >= minimum_tables[schema],
        'Restore attestation is incomplete')


def digest(path):
    value = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def stamp():
    return datetime.now(timezone.utc).isoformat()


def local_os_actor():
    """Identidad efectiva del proceso; nunca toma el actor de un argumento."""
    uid = os.geteuid()
    try:
        name = pwd.getpwuid(uid).pw_name
    except KeyError:
        name = "uid-" + str(uid)
    return f"os:{uid}:{name}"[:128]


def validate_host_execution_profile(profile, *, owner_uid, mode, effective_uid,
                                    hostname, actual_hostname):
    """Valida el perfil sellado sin aceptar selección desde el cliente."""
    require(owner_uid == 0 and not mode & 0o077,
            "Host execution profile must be root-owned and private")
    require(effective_uid == 0,
            "LAB execution requires the authorized OS identity")
    require(
        isinstance(profile, dict)
        and set(profile) == {
            "version", "environment", "cluster", "control_plane", "hostname", "enabled"
        }
        and profile["version"] == 1
        and profile["environment"] == "lab"
        and profile["cluster"] == "MNG"
        and profile["control_plane"] == "VM104"
        and profile["hostname"] == hostname == actual_hostname
        and profile["enabled"] is True,
        "Host execution profile is not the authorized MNG LAB profile",
    )
    return {"environment": "lab", "source": "host-execution-profile",
            "cluster": "MNG", "control_plane": "VM104", "hostname": hostname}


def host_execution_profile():
    """Lee el perfil LAB solo desde la configuración privada fija del host.

    La ausencia del archivo conserva el camino corporativo. Un archivo
    presente pero inválido falla cerrado; ningún argumento o variable del
    cliente puede seleccionar LAB.
    """
    try:
        path = HOST_EXECUTION_PROFILE
        stat = path.lstat()
    except FileNotFoundError:
        return {"environment": "production", "source": "default"}
    require(path.is_file() and not path.is_symlink(),
            "Invalid host execution profile")
    try:
        profile = json.loads(path.read_text(encoding="utf8"))
    except (OSError, UnicodeError, ValueError) as error:
        raise RuntimeError("Invalid host execution profile") from error
    result = validate_host_execution_profile(
        profile,
        owner_uid=stat.st_uid,
        mode=stat.st_mode,
        effective_uid=os.geteuid(),
        hostname=profile.get("hostname") if isinstance(profile, dict) else None,
        actual_hostname=socket.gethostname(),
    )
    result["source"] = str(path)
    return result


def private_json(path, value):
    # Evidence is immutable. A rerun gets a new numbered artifact.
    with Path(path).open("x", encoding="utf8") as stream:
        os.chmod(path, 0o600)
        json.dump(value, stream, sort_keys=True, indent=2)
        stream.write("\n")


def check_idle(report, allowed_uncertain=()):
    nonterminal={k:list(v) for k,v in report["nonterminal"].items()}
    if allowed_uncertain:
        expected=set(allowed_uncertain)
        rows=nonterminal.get('terraform_applies',[])
        found={r['id'] for r in rows if r['status']=='uncertain' and r['id'] in expected}
        require(found==expected,'Reviewed uncertainty fence changed')
        nonterminal['terraform_applies']=[r for r in rows if not (r['status']=='uncertain' and r['id'] in expected)]
    require(not any(nonterminal.values()), "Nonterminal DB work; use its approved recovery workflow")
    require(not any(report["queues"].values()) and report["unacked"] == 0, "Broker has pending work")


def check_queued_guest_recovery_scope(report, request):
    """Permite observar únicamente la inicialización declarada antes de cancelarla."""
    expected = {
        "guest_initializations": request["operation_id"],
        "operations": request["operation_id"],
        "jobs": request["job_id"],
    }
    for name, rows in report.get("nonterminal", {}).items():
        if not rows:
            continue
        require(name in expected and len(rows) == 1,
                "Unexpected nonterminal work during queued guest recovery: " + name)
        row = rows[0]
        require(str(row["id"]) == expected[name] and row["status"] == "queued",
                "Queued guest recovery identity or status changed: " + name)
    # En una repetición idempotente las tres filas ya pueden ser terminales y,
    # por tanto, no aparecen en runtime_snapshot(). La acción inyectada vuelve
    # a comprobar sus estados terminales y su evento de auditoría exactos.
    require(not any(report.get("queues", {}).values()) and report.get("unacked") == 0,
            "Broker activity prevents queued guest recovery")


def compare_history(before, after, ownership_migration=None):
    from_0013 = before.get('schema') == ['0013_technical_ownership']
    if from_0013:
        require(isinstance(ownership_migration, dict),
                'Technical ownership migration evidence is required for 0013→0014')
        adopted = ownership_migration.get('adopted_instances')
        require(isinstance(adopted, list) and all(
            isinstance(row, dict) and row.get('instance_id') and isinstance(row.get('vmid'), int)
            for row in adopted), 'Technical ownership adoption report is incomplete')
        before_entries = before.get('technical_ownership_entries')
        after_entries = after.get('technical_ownership_entries')
        require(isinstance(before_entries, dict) and isinstance(after_entries, dict),
                'Technical ownership row evidence is missing')
        before_ids, after_ids = set(before_entries), set(after_entries)
        adopted_ids = {row['instance_id'] for row in adopted}
        require(before_ids <= after_ids, 'Technical ownership rows were removed during 0013→0014')
        require(after_ids - before_ids == adopted_ids,
                'Unexpected technical ownership rows were materialized during 0013→0014')
        require(all(after_entries[identifier] == before_entries[identifier] for identifier in before_ids),
                'Existing technical ownership changed during 0013→0014')
        before_tables = set(before.get('tables', {}))
        after_tables = set(after.get('tables', {}))
        require(before_tables <= after_tables,
                'Historical table disappeared during 0013→0014')
        require(after_tables - before_tables <= {'technical_ownership'},
                'Unexpected historical table materialized during 0013→0014')
    for name, evidence in before["tables"].items():
        if name == 'technical_ownership' and from_0013:
            continue
        if from_0013:
            require(after["tables"].get(name) == evidence,
                    "Historical rows changed: " + name)
            continue
        if name=='instances' and before.get('schema')==['0011_crm_contract']:
            projected=after.get('compatibility',{}).get('instances_before_0012',{})
            require(projected.get('new_fields_default') is True and
                {k:projected.get(k) for k in ('count','sha256')}==evidence,
                'Historical data changed during commercial migration: instances')
            continue
        if before.get('schema') and before['schema'] not in [['0011_crm_contract'], ['0012_commercial_intents'], [HEAD]] and (name == 'operations' or
                name == 'instances' and before['schema'] in [['0009_lab_self_service'], ['0010_guest_identity']]):
            projected = after.get('compatibility', {}).get(name+'_before_0011', {})
            require(projected.get('new_fields_default') is True and
                {k:projected.get(k) for k in ('count','sha256')} == evidence,
                'Historical data changed during CRM migration: '+name)
            continue
        if name in {'instances','deletion_executions'} and before['schema'] not in [['0009_lab_self_service'],['0010_guest_identity'],['0011_crm_contract'],['0012_commercial_intents'],[HEAD]]:
            projected=after.get('compatibility',{}).get(name+'_before_0009',{})
            if name=='deletion_executions' or before['schema']==['0008_dhcp_profiles']:
                require(projected.get('new_fields_default') is True
                        and {k:projected.get(k) for k in ('count','sha256')}==evidence,
                        'Historical data changed during technical ownership migration: '+name)
                continue
        if name=='instances' and before['schema']!=[HEAD] and before['schema'] in [
                ['0004_authorized_apply'],['0005_kvm_lifecycle'],['0006_power_ownership'],['0007_safe_deletion']]:
            projected=after.get('compatibility',{}).get('instances_before_0008',{})
            require(projected.get('new_fields_default') is True
                    and {k:projected.get(k) for k in ('count','sha256')}==evidence,
                    'Historical instance data changed during profile migration')
            continue
        require(after["tables"].get(name) == evidence, "Historical rows changed: " + name)


class Promotion:
    def __init__(self, manifest, output):
        self.m = manifest
        self.output = output
        self.execution_profile = host_execution_profile()
        self.env = {k: os.environ[k] for k in ("PATH", "HOME") if k in os.environ}
        self.env.update(COMPOSE_ANSI="never")
        require(manifest["project"] == "iacp-controlplane", "Unexpected Compose project")
        require(socket.gethostname() == manifest["hostname"], "Unexpected host")
        require(Path(manifest["runtime_dir"]).resolve() == ROOT, "Execute from the reviewed runtime checkout")
        self.docker = ["docker", "--host", "unix:///var/run/docker.sock"]
        self.dc = self.docker + ["compose", "--env-file", manifest["environment_file"], "-p", manifest["project"]]
        for file in manifest["compose_files"]:
            self.dc += ["-f", str(ROOT / file)]
        self.sequence = 0
        exposure_mode(manifest)  # A typo must never silently select a weaker mode.
        if exposure_mode(manifest) == "corporate-internal":
            internal_target(manifest)

    def evidence(self, name, data):
        self.sequence += 1
        private_json(self.output / f"{self.sequence:03}-{name}.json", {"at": stamp(), "data": data})

    def run(self, args, *, input=None, timeout=60):
        result = subprocess.run(args, input=input, text=True, capture_output=True, cwd=ROOT,
                                env=self.env, timeout=timeout)
        # Never print Compose output or provider errors: they can contain secrets.
        self.evidence("command", {"program": args[0], "arguments_sha256": hashlib.sha256(json.dumps(args).encode()).hexdigest(),
                                  "returncode": result.returncode})
        require(result.returncode == 0, "Command failed; inspect privately on host: " + args[0])
        return result.stdout.strip()

    def compose(self, *args, **kwargs):
        # Compose excludes inactive profiles from `config`. Inspect the optional
        # certifier too, without enabling it for `up` or any ordinary service.
        options = ["--profile", "certification"] if args and args[0] == "config" else []
        return self.run(self.dc + options + list(args), **kwargs)

    def creation_contract(self):
        """Lee el contrato efectivo de Compose, nunca una variable del shell."""
        return "provisioning-api-core"

    def containers(self):
        ids = self.run(self.docker + ["ps", "-aq", "--filter", "label=com.docker.compose.project=" + self.m["project"]]).split()
        require(ids, "No existing stack; this driver cannot bootstrap or adopt another project")
        return json.loads(self.run(self.docker + ["inspect"] + ids))

    def container(self, role):
        matches = [c for c in self.containers() if c["Config"]["Labels"].get("com.docker.compose.service") == role
                   and c["Config"]["Labels"].get("com.docker.compose.oneoff", "false").lower() != "true"]
        require(len(matches) == 1, "Expected one container for " + role)
        return matches[0]

    @staticmethod
    def planned_mounts(config, role):
        mounts = {}
        for item in config["services"][role].get("volumes", []):
            source = config["volumes"][item["source"]]["name"] if item["type"] == "volume" else item["source"]
            mounts[item["target"]] = (item["type"], source, not item.get("read_only", False))
        for item in config["services"][role].get("secrets", []):
            target = item.get("target", item["source"])
            target = target if target.startswith("/") else "/run/secrets/" + target
            mounts[target] = ("bind", config["secrets"][item["source"]]["file"], False)
        return mounts

    def exec_python(self, role, code):
        return self.run(self.docker + ["exec", "-i", self.container(role)["Id"], "python", "-"], input=code)

    def snapshot(self):
        # API may be stopped, but the conventional worker has the same DB/broker config.
        if not self.container("worker")["State"]["Running"]:
            return self.snapshot_stopped()
        report = json.loads(self.exec_python("worker", (ROOT / "scripts/runtime_snapshot.py").read_text()))
        self.evidence("snapshot", report)
        return report

    def workers_idle(self, *, lifecycle=False):
        code = '''import json
from iacp.worker import celery_app
control = celery_app.control.inspect(timeout=5)
result = {key: getattr(control, key)() for key in ('active','reserved','scheduled','active_queues')}
print(json.dumps({key: {node: ([q['name'] for q in tasks] if key == 'active_queues' else len(tasks))
                       for node,tasks in (value or {}).items()} for key,value in result.items()}))
'''
        if self.container("worker")["State"]["Running"]:
            report = json.loads(self.exec_python("worker", code))
        else:
            report = json.loads(self.compose("run", "--rm", "--no-deps", "--pull", "never", "-T", "--entrypoint", "python", "migrate", "-", input=code))
        roles = ROLES[1:] if lifecycle else ["worker", "terraform-worker"]
        roles = [role for role in roles if self.container(role)["State"]["Running"]]
        expected = {"celery@" + self.container(role)["Config"]["Hostname"]:
                    [dict(worker="celery", **{"terraform-worker": "terraform", "lifecycle-worker": "lifecycle"})[role]] for role in roles}
        require(report["active_queues"] == expected, "Missing, unexpected or incorrectly routed worker")
        require(all(set(report[key]) == set(expected) for key in ("active", "reserved", "scheduled")), "Partial worker inventory")
        require(all(not any(report[key].values()) for key in ("active", "reserved", "scheduled")), "Workers are still busy")
        self.evidence("workers", report)

    def recover_queued_guest_init(self, request):
        """Cancela una sola inicialización queued no despachada en el API activo.

        La fuente de la acción se inyecta por stdin al contenedor que está
        ejecutando la aplicación. Por eso esta operación no depende de haber
        desplegado primero el candidato ni de que la imagen activa conozca el
        nuevo comando.
        """
        change = self.authorize("recover-queued-guest-init")
        if self.execution_profile.get("environment") == "lab":
            request = {
                **request,
                "actor": request.get("actor") or change["actor"],
                "change_reference": request.get("change_reference") or change["reference"],
            }
        else:
            require(request.get("actor") == change["actor"],
                    "Operator identity differs from change record")
            require(request.get("change_reference") == change["reference"],
                    "Change reference differs from change record")
        before = self.snapshot()
        require(before["schema"] == [HEAD], "Recovery requires the current schema head")
        check_queued_guest_recovery_scope(before, request)
        self.workers_idle(lifecycle=True)
        source = (ROOT / "app/iacp/services/guest_recovery.py").read_text(encoding="utf8")
        payload = json.dumps(request, sort_keys=True)
        code = source + "\nrun_from_json(" + repr(payload) + ")\n"
        result = json.loads(self.exec_python("api", code))
        self.evidence("recover-queued-guest-init", result)
        require(result.get("passed") is True,
                "Queued guest recovery rejected: " + str(result.get("reason", "unknown")))
        after = self.snapshot()
        check_idle(after)
        self.workers_idle(lifecycle=True)
        self.evidence("recover-queued-guest-init-completed", {
            "instance_id": request["instance_id"],
            "operation_id": request["operation_id"],
            "job_id": request["job_id"],
            "changed": result.get("changed"),
            "already_completed": result.get("already_completed"),
            "check_idle": True,
        })
        return result

    def migrate_technical_ownership(self):
        """Adopta evidencia persistida sin mutar Terraform ni Proxmox."""
        report = json.loads(self.exec_python("api", """
import json
from iacp.db import get_session_factory
from iacp.services.technical_ownership_migration import migrate
with get_session_factory()() as session:
    print(json.dumps(migrate(session), sort_keys=True))
"""))
        self.evidence("technical-ownership-migration", report)
        require(report.get("blocked_count", 0) == 0,
                "Technical ownership migration has blocked instances")
        return report

    def approve(self, action):
        record = self.m["change_record"]
        require(digest(record["path"]) == record["sha256"], "Change record checksum mismatch")
        change = json.loads(Path(record["path"]).read_text())
        now = datetime.now(timezone.utc)
        start, end = (datetime.fromisoformat(change[key].replace("Z", "+00:00")) for key in ("window_start", "window_end"))
        require(start.tzinfo and end.tzinfo and start <= now <= end, "Outside authorized change window")
        require(change["approved"] is True and change["hostname"] == self.m["hostname"]
                and change["project"] == self.m["project"] and change["release"] == self.m["release"]
                and action in change["actions"] and change["actor"] and change["reference"], "Action not approved")
        require(change["external_writers_excluded"] is True, "External writers have not been excluded")
        self.evidence("approval", {"action": action, "record_sha256": record["sha256"], "actor": change["actor"], "reference": change["reference"]})
        return change

    def authorize(self, action):
        """Autoriza según el entorno sellado del host, sin debilitar producción."""
        profile = getattr(self, "execution_profile", None)
        if profile is None:
            profile = host_execution_profile()
        if profile.get("environment") == "lab" and action in LAB_AUTOMATED_ACTIONS:
            actor = local_os_actor()
            reference = f"lab:{socket.gethostname()}:{self.m['release']}:{action}"
            result = {
                "approved": False,
                "authorized": True,
                "authorization_source": "host-execution-profile",
                "actor": actor,
                "reference": reference,
                "hostname": socket.gethostname(),
                "project": self.m["project"],
                "release": self.m["release"],
                "actions": [action],
                "external_writers_excluded": True,
            }
            self.evidence("lab-authorization", {
                "actor": actor,
                "action": action,
                "release": self.m["release"],
                "authorization_source": "host-execution-profile",
                "result": "authorized",
            })
            return result
        return self.approve(action)

    def attest_root_transport(self):
        """Install only the deterministic TLS assertion; preserve routing/RBAC."""
        self.authorize('attest-root-transport')
        reference=self.m['tls_dynamic_file'];candidate=self.m['root_transport_configuration']
        require(digest(reference['path'])==reference['sha256'], 'Active TLS routing changed')
        require(digest(candidate['path'])==candidate['sha256'], 'Transport candidate checksum mismatch')
        import importlib.util
        spec=importlib.util.spec_from_file_location('root_transport_renderer', ROOT/'scripts/render-tls.py')
        renderer=importlib.util.module_from_spec(spec);spec.loader.exec_module(renderer)
        current=json.loads(Path(reference['path']).read_text())
        expected=renderer.root_tls_transport(current)
        require(json.loads(Path(candidate['path']).read_text())==expected, 'Transport change alters controls outside the assertion')
        self.proxy_exposure(json.loads(self.compose('config','--format','json')),current)
        path=Path(reference['path']);st=path.stat();temporary=path.with_name(path.name+'.installing')
        with temporary.open('x') as stream:
            os.chmod(temporary, st.st_mode & 0o777);os.chown(temporary,st.st_uid,st.st_gid)
            stream.write(Path(candidate['path']).read_text());stream.flush();os.fsync(stream.fileno())
        temporary.replace(path)
        self.evidence('root-transport-installed',{'path':str(path),'sha256':digest(path),'existing_controls_preserved':True})

    def flags(self, *, admission, lifecycle, deletion=None):
        path = Path(self.m["environment_file"])
        require(not path.is_symlink(), "Environment file must not be a symlink")
        original = path.read_text()
        values = {"IACP_ADMISSION_ENABLED": str(admission), "IACP_KVM_LIFECYCLE_ENABLED": str(lifecycle),
                  "IACP_TERRAFORM_APPLY_ENABLED": "1"}
        if deletion is not None:
            require(deletion in (0, 1), "Invalid deletion flag")
            values['IACP_TERRAFORM_DELETE_ENABLED']=str(deletion)
        lines = [line for line in original.splitlines() if not re.match(
            r"^\s*(?:export\s+)?(?:" + "|".join(values) + r")\s*=", line)]
        new = "\n".join(lines + [key + "=" + value for key, value in values.items()]) + "\n"
        if new == original:
            return
        temporary = path.with_name(path.name + ".promotion-tmp")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as stream:
            stream.write(new)
            stream.flush()
            os.fsync(stream.fileno())
        st = path.stat()
        os.chmod(temporary, st.st_mode & 0o777)
        os.chown(temporary, st.st_uid, st.st_gid)
        temporary.replace(path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        self.evidence("flags", values)

    def preflight(self):
        require(re.fullmatch(r"[a-f0-9]{40}", self.m["release"]), "Full release commit required")
        require(self.run(["git", "rev-parse", "HEAD"]) == self.m["release"], "Checkout is not the reviewed release")
        require(not self.run(["git", "status", "--porcelain", "--untracked-files=normal"]), "Dirty runtime checkout")
        cfg = json.loads(self.compose("config", "--format", "json"))
        services = cfg["services"]
        require(set(ROLES + ["migrate", "ownership-certifier", "db", "redis", "traefik"]).issubset(services), "Required override missing")
        for role in ROLES + ["migrate", "ownership-certifier"]:
            expected = self.m["images"]["terraform" if role in {"terraform-worker", "ownership-certifier"} else "api"]
            image = services[role]["image"]
            actual = json.loads(self.run(self.docker + ["image", "inspect", image]))[0]["Id"]
            require(actual == expected and "bootstrap" not in image, "Image mismatch: " + role)
        require(services["api"]["environment"].get("IACP_TERRAFORM_APPLY_ENABLED") == "1"
                and services["terraform-worker"]["environment"].get("IACP_TERRAFORM_APPLY_ENABLED") == "1", "Apply must remain 1")
        require("IACP_ADMISSION_ENABLED" in services["api"]["environment"], "Promotion override missing")
        active = self.containers()
        if self.canonical():
            from operations import compare, runtime_command, live_compatibility
            current = json.loads(self.run(runtime_command(active, self.docker)))
            compatibility = live_compatibility(current, active)
            self.evidence("live-compatibility", compatibility)
            require(compatibility["passed"], "Active environment differs from the running containers")
            comparison = compare(current, cfg, self.m.get("configuration_changes", []), profile_file=self.m.get('profile_configuration',{}).get('path'), source_directory=self.m.get('source_promotion',{}).get('path'), root_bootstrap=self.m.get('root_bootstrap_configuration'), commercial_bootstrap=self.m.get('commercial_bootstrap_configuration'), creation_contract=self.creation_contract())
            self.evidence("canonical-comparison", comparison)
            require(comparison["passed"], "Canonical configuration differs outside the reviewed changes")
        planned_files = {str((ROOT / file).resolve()) for file in self.m["compose_files"]}
        # Old bootstrap labels reference a different compose for DB/Redis. Their
        # immutable volumes/secrets are checked directly; all application overrides persist.
        for c in active:
            role = c["Config"]["Labels"].get("com.docker.compose.service")
            if role not in ROLES + ["traefik"]:
                continue
            old_files = set(c["Config"]["Labels"]["com.docker.compose.project.config_files"].split(","))
            require(self.canonical() or old_files <= planned_files, "An active override was omitted")
        for role, service in services.items():
            if role != "traefik":
                require(not service.get("ports"), "Unexpected published service: " + role)
            require(not service.get("privileged") and not service.get("network_mode") == "host", "Unsafe container: " + role)
            for mount in service.get("volumes", []):
                require("docker.sock" not in mount.get("source", ""), "Docker socket must not be mounted")
            for item in service.get("secrets", []):
                path = Path(cfg["secrets"][item["source"]]["file"])
                if item['source'] == 'root_input_key' and str(path) == '/dev/null' and service.get('environment', {}).get('IACP_ROOT_PASSWORD_BOOTSTRAP_ENABLED') == '0':
                    continue
                if (item['source'] in {'bootstrap_aes_key', 'guest_bootstrap_ssh_key', 'admission_credentials'}
                        and str(path) == '/dev/null'):
                    continue
                st = path.stat()
                require(st.st_size > 0 and not st.st_mode & 0o007, "Empty or world-readable secret")
                # Current application images run as UID/GID 10001; reject root-only mounts.
                if role in ROLES + ["migrate", "ownership-certifier"]:
                    require((st.st_uid == 10001 and st.st_mode & 0o400) or (st.st_gid == 10001 and st.st_mode & 0o040),
                            "Secret unreadable by UID/GID 10001: " + item["source"])
        for role, destination in (("db", "/var/lib/postgresql/data"), ("redis", "/data"), ("terraform-worker", "/var/lib/iacp/terraform")):
            current = next(m for m in self.container(role)["Mounts"] if m["Destination"] == destination)
            planned = next(m for m in services[role]["volumes"] if m["target"] == destination)
            volume = cfg["volumes"][planned["source"]]
            require(volume.get("external") is True and volume["name"] == current["Name"], "Storage adoption mismatch")
        root_review = self.m.get('root_bootstrap_configuration', {})
        if root_review.get('enabled') is True:
            dynamic=self.m['tls_dynamic_file']
            require(digest(dynamic['path'])==dynamic['sha256'], 'Reviewed TLS routing changed')
            import importlib.util
            spec=importlib.util.spec_from_file_location('root_transport_preflight', ROOT/'scripts/render-tls.py')
            renderer=importlib.util.module_from_spec(spec);spec.loader.exec_module(renderer)
            routing=json.loads(Path(dynamic['path']).read_text())
            require(renderer.root_tls_transport(routing)==routing, 'TLS-only transport assertion is not installed')
            proxy_ips = root_review.get('proxy_ips', '').split(',')
            actual_proxy_ips = {n['IPAddress'] for n in self.container('traefik')['NetworkSettings']['Networks'].values()}
            require(bool(proxy_ips) and all(ip and ip in actual_proxy_ips for ip in proxy_ips), 'TLS peer differs from the existing proxy')
            key_path = root_review.get('key_path')
            require(key_path and key_path != '/dev/null' and cfg['secrets']['root_input_key']['file'] == key_path,
                    'Reviewed root input key required')
            for role in ('api', 'terraform-worker'):
                require(services[role]['environment']['IACP_ROOT_PASSWORD_BOOTSTRAP_ENABLED'] == '1', 'Root bootstrap gate differs')
            require(services['api']['environment']['IACP_TLS_PROXY_IPS'] == root_review['proxy_ips'], 'TLS proxy trust differs')
            self.run(self.docker+['run','--rm','--pull','never','--network','none','--read-only','--user','10001:10001',
                '--cap-drop','ALL','--security-opt','no-new-privileges','--memory','256m','--cpus','1','--pids-limit','32',
                '--mount','type=bind,source='+key_path+',target=/key,readonly',services['api']['image'],'python','-c',
                "from pathlib import Path; from cryptography.fernet import Fernet; Fernet(Path('/key').read_bytes().strip()); print('ROOT_INPUT_KEY_VALID')"])
        self.lifecycle_mount_targets(services['lifecycle-worker'])
        for role in ("api", "worker", "terraform-worker"):
            old_mounts = {m["Destination"]: m["Source"] for m in self.container(role)["Mounts"] if m["Destination"].startswith("/run/")}
            planned = self.planned_mounts(cfg, role)
            for target, source in old_mounts.items():
                profile=self.m.get('profile_configuration',{})
                if (target=='/run/config/profiles.json' and profile
                        and planned.get(target)==('bind',profile['path'],False)):
                    require(digest(profile['path'])==profile['sha256'], 'Replacement profile checksum mismatch')
                    continue
                require(planned.get(target) == ("bind", source, False), "Existing secret/config mount changed")
        source_mount = next(m for m in services["terraform-worker"]["volumes"] if m["target"] == "/iac-source")
        require(source_mount["read_only"] is True, "IaC source must be readonly")
        archive = subprocess.run(["git", "-c", "safe.directory=" + source_mount["source"], "-C", source_mount["source"], "archive", SOURCE],
                                 capture_output=True, timeout=30, env=self.env, check=True).stdout
        require(hashlib.sha256(archive).hexdigest() == ARCHIVE, "IaC checksum mismatch")
        if 'profile_configuration' in self.m:
            ref=self.m['profile_configuration']
            require(digest(ref['path'])==ref['sha256'] and not Path(ref['path']).is_symlink(), 'Profile policy checksum/type mismatch')
            for role in ('api','terraform-worker'):
                profile_mount=next(m for m in services[role]['volumes'] if m['target']=='/run/config/profiles.json')
                require(profile_mount['source']==ref['path'] and profile_mount.get('read_only') is True, 'Profile mount differs from reviewed file')
            image=services['api']['image']
            self.run(self.docker+['run','--rm','--pull','never','--network','none','--read-only','--user','10001:10001',
                '--cap-drop','ALL','--security-opt','no-new-privileges','--memory','512m','--cpus','1','--pids-limit','64',
                '--mount','type=bind,source='+ref['path']+',target=/policy.json,readonly',image,'python','-c',
                "import json; from pathlib import Path; from iacp.terraform.profiles import Profile; [Profile.model_validate(v) for v in json.loads(Path('/policy.json').read_text())['profiles'].values()]; print('PROFILE_POLICY_VALID')"])
        backup = self.m["backup"]
        require(digest(backup["path"]) == backup["sha256"], "Backup checksum mismatch")
        require(digest(backup["restore_evidence"]) == backup["restore_evidence_sha256"], "Restore evidence checksum mismatch")
        restored = json.loads(Path(backup["restore_evidence"]).read_text())
        check_restore_evidence(backup, restored)
        report = self.snapshot()
        require_supported_schema(report['schema'])
        self.evidence("preflight", {"passed": True, "configuration_sha256": hashlib.sha256(json.dumps(cfg, sort_keys=True).encode()).hexdigest()})
        return report

    def canonical(self):
        if self.m.get("configuration_mode") != "canonical":
            return False
        files = self.m["compose_files"]
        require(len(files) == 1 and Path(files[0]).parts[0] == "deploy"
                and Path(files[0]).name == "compose.yaml" and (ROOT / files[0]).resolve().is_relative_to(ROOT / "deploy"), "One versioned canonical Compose required")
        return True

    def lifecycle_mount_targets(self, service):
        """Permitir el perfil público revisado; nunca secretos adicionales o escritura."""
        require({s['source'] for s in service['secrets']} == {'db_password', 'kvm_credentials'}
                and service.get('read_only') is True, 'Lifecycle isolation changed')
        allowed = {'/run/secrets/db_password', '/run/secrets/kvm_credentials'}
        ca = service['environment'].get('IACP_KVM_CA_FILE')
        profile_target = service['environment'].get('IACP_TERRAFORM_PROFILES_FILE')
        seen = set()
        for mount in service.get('volumes', []):
            target = mount['target']
            require(mount['type'] == 'bind' and mount.get('read_only') is True
                    and target not in seen, 'Unexpected or writable Lifecycle mount')
            seen.add(target)
            if profile_target and target == profile_target:
                ref = self.m.get('profile_configuration', {})
                require(target == '/run/config/profiles.json' and mount['source'] == ref.get('path')
                        and not Path(mount['source']).is_symlink()
                        and digest(mount['source']) == ref.get('sha256'),
                        'Lifecycle profile differs from review')
            else:
                require(bool(ca) and target == ca, 'Unexpected Lifecycle mount')
            allowed.add(target)
        require(not profile_target or profile_target in seen, 'Lifecycle profile mount missing')
        require(not ca or ca in seen, 'Lifecycle CA mount missing')
        return allowed

    def publish(self):
        self.authorize("publish-api")
        self.preflight()
        self.verify()
        require(self.canonical() or ("compose.public.yaml" in self.m["compose_files"] and "compose.tls.yaml" in self.m["compose_files"]), "TLS/proxy configuration missing")
        self.publication_controls()
        dynamic = self.m["tls_dynamic_file"]
        require(digest(dynamic["path"]) == dynamic["sha256"], "Reviewed TLS routing changed")
        # The file provider loads every file in the directory. An extra router
        # can bypass the reviewed policy, so this directory must be dedicated.
        directory = Path(dynamic["path"]).parent
        require([p.name for p in directory.iterdir() if p.is_file()] == [Path(dynamic["path"]).name], "Unexpected dynamic proxy configuration")
        config = json.loads(Path(dynamic["path"]).read_text())
        require(set(config["http"]["routers"]) == {"diagnostic", "iacp-admin", "iacp-crm", "iacp-http"}, "Unexpected proxy routes")
        compose = json.loads(self.compose("config", "--format", "json"))
        self.proxy_exposure(compose, config)
        proxy = compose["services"]["traefik"]
        require(proxy["command"] == ["--configFile=/etc/traefik/public.yml"], "Unexpected static proxy command")
        require(not any(key.startswith("TRAEFIK_") for key in proxy.get("environment", {})), "Unexpected static proxy environment override")
        require("@sha256:" in proxy["image"], "Public Traefik image must be pinned by digest")
        image = json.loads(self.run(self.docker + ["image", "inspect", proxy["image"]]))[0]["Id"]
        require(image == self.m["images"]["proxy"], "Unreviewed proxy image")
        mounts = {item["target"]: item["source"] for item in proxy["volumes"]}
        require(digest(mounts["/etc/traefik/public.yml"]) == digest(ROOT / "traefik/public.yml"), "Unreviewed static proxy file/provider")
        require(Path(mounts["/etc/traefik/tls-dynamic"]).resolve() == directory.resolve(), "Reviewed routing is not mounted")
        # Validate the current chain immediately before exposing it, even if the
        # renderer already checked it when generating the reviewed artifact.
        import importlib.util
        spec = importlib.util.spec_from_file_location("tls_renderer", ROOT / "scripts/render-tls.py")
        renderer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(renderer)
        renderer.check_certificate(Path(mounts["/run/tls"]), self.m.get("api_ca_file"))
        self.up("traefik")
        if exposure_mode(self.m) == "corporate-internal":
            self.internal_live_bindings()
        deadline = time.monotonic() + 30
        while True:
            require(self.container("traefik")["State"]["Running"], "Traefik exited")
            try:
                binding = next(port for port in proxy["ports"] if port["target"] == 443)
                context = ssl.create_default_context(cafile=self.m.get("api_ca_file"))
                with socket.create_connection((binding["host_ip"], 443), timeout=3) as transport:
                    with context.wrap_socket(transport, server_hostname="iac.cyberfuel.com"):
                        break
            except OSError:
                require(time.monotonic() < deadline, "Local published TLS handshake failed; do not claim external readiness")
                time.sleep(2)
        self.evidence("publication", {"configured": True, "exposure_mode": exposure_mode(self.m),
                                     "client_validation": "pending", "host": "iac.cyberfuel.com"})

    def publication_controls(self):
        reviewed = self.m["publication_review"]
        require(digest(reviewed["path"]) == reviewed["sha256"], "Publication review checksum mismatch")
        controls = json.loads(Path(reviewed["path"]).read_text())
        if exposure_mode(self.m) == "corporate-internal":
            bind, networks = internal_target(self.m)
            require(controls.get("exposure_mode") == "corporate-internal" and controls.get("api_bind_ip") == bind
                    and controls.get("approved_client_cidrs") == [str(net) for net in networks]
                    and controls.get("identity_mode") == self.m["identity_mode"], "Internal review does not match manifest")
            keys = ["corporate_dns_approved", "lan_vpn_firewall_approved", "no_public_nat", "no_wan_exposure",
                    "internal_access_policy_approved", "proxy_version_approved"]
            require(all(isinstance(controls.get(key), str) and controls[key].strip() for key in
                        ("network_evidence_reference", "identity_policy_reference")), "Internal network/identity approval references missing")
            if self.m["identity_mode"] == "gateway":
                keys.append("gateway_policy_approved")
            require(resolved_addresses() == [bind], "Corporate DNS does not resolve to the authorized API bind")
        else:
            keys = ["public_dns_approved", "nat_firewall_approved", "gateway_policy_approved", "proxy_version_approved"]
        require(all(controls.get(key) is True for key in keys), "Publication controls incomplete")
        self.evidence("publication-controls", {"exposure_mode": exposure_mode(self.m), "review_sha256": reviewed["sha256"]})

    def proxy_exposure(self, compose, config):
        routers = config["http"]["routers"]
        internal = exposure_mode(self.m) == "corporate-internal"
        if not internal or self.m["identity_mode"] == "gateway":
            require("corporate-identity" in routers["iacp-admin"]["middlewares"], "Administrative gateway missing")
        if internal:
            bind, networks = internal_target(self.m)
            # Compose binds alone cannot establish absence of perimeter NAT. That
            # requires the separately reviewed network evidence above.
            for role, service in compose["services"].items():
                if role != "traefik":
                    require(not service.get("ports"), "Only Traefik may publish host ports")
            ports = compose["services"]["traefik"]["ports"]
            expected = {(80, "80", bind), (443, "443", bind)}
            if not self.canonical():
                expected.add((8080, "8080", "127.0.0.1"))
            require({(p["target"], str(p["published"]), p.get("host_ip")) for p in ports} == expected
                    and len(ports) == len(expected) and all(p.get("protocol", "tcp") == "tcp" for p in ports), "Internal proxy must bind only the approved private IP and loopback diagnostic")
            for name in ("iacp-admin", "iacp-crm", "iacp-http"):
                require("internal-sources" in routers[name]["middlewares"], "Internal source restriction missing")
            for name in ("iacp-admin", "iacp-crm"):
                require(set(("strip-identity", "request-rate", "inflight", "body-limit", "crm-headers")) <= set(routers[name]["middlewares"])
                        and routers[name].get("tls") == {"options": "corporate"}, "Internal proxy security/TLS controls missing")
            require(config["http"]["middlewares"]["internal-sources"]["ipAllowList"] == {"sourceRange": [str(net) for net in networks]}, "Internal allowlist differs from approved LAN/VPN networks")

    def internal_live_bindings(self):
        bind, _ = internal_target(self.m)
        proxy = self.container("traefik")
        expected = {
            "80/tcp": [{"HostIp": bind, "HostPort": "80"}],
            "443/tcp": [{"HostIp": bind, "HostPort": "443"}],
        }
        if not self.canonical():
            expected["8080/tcp"] = [{"HostIp": "127.0.0.1", "HostPort": "8080"}]
        require(proxy["State"]["Running"] and proxy["HostConfig"]["PortBindings"] == expected, "Running Traefik exposes unexpected addresses or ports")

    def up(self, *roles):
        self.compose("up", "-d", "--no-deps", "--no-build", "--pull", "never", *roles, timeout=180)

    def verify(self, *, admission=0, lifecycle=0, idle=True, allowed_uncertain=()):
        config = json.loads(self.compose("config", "--format", "json"))
        if self.canonical():
            self.internal_live_bindings()
        for role in ROLES:
            c = self.container(role)
            require(c["State"]["Running"], "Container stopped: " + role)
            require(c["Image"] == self.m["images"]["terraform" if role == "terraform-worker" else "api"], "Running image mismatch")
            values = dict(v.split("=", 1) for v in c["Config"]["Env"])
            require(not c["HostConfig"]["PortBindings"], "Unexpected application host port")
            actual_mounts = {m["Destination"]: (m["Type"], m.get("Name") if m["Type"] == "volume" else m["Source"], m["RW"])
                             for m in c["Mounts"] if m["Type"] != "tmpfs"}
            require(actual_mounts == self.planned_mounts(config, role), "Running mounts differ from reviewed configuration: " + role)
            # Validate effective UID and actual reads, not only host mode bits.
            secrets = [target for target in actual_mounts if target.startswith("/run/secrets/")]
            self.exec_python(role, "import os\nassert os.geteuid() == 10001\nfor path in " + repr(secrets) + ":\n with open(path, 'rb') as stream: assert stream.read(1)\nprint('UID10001_SECRET_READS_OK')")
            if role in {"api", "terraform-worker"}:
                require(values.get("IACP_TERRAFORM_APPLY_ENABLED") == "1", "Apply changed")
                planned_env = config['services'][role]['environment']
                require(values.get('IACP_TERRAFORM_DELETE_ENABLED', '0') == planned_env.get('IACP_TERRAFORM_DELETE_ENABLED', '0'),
                        "Deletion flag/allowlist differs from reviewed configuration")
            if role in {"api", "lifecycle-worker", "terraform-worker"}:
                require(values.get("IACP_KVM_LIFECYCLE_ENABLED", "0") == str(lifecycle), "Lifecycle flag mismatch")
            if role == "api":
                require(values.get("IACP_ADMISSION_ENABLED") == str(admission), "Admission flag mismatch")
            if role == "lifecycle-worker":
                require(not c["HostConfig"]["PortBindings"] and c["HostConfig"]["ReadonlyRootfs"], "Lifecycle container is not isolated")
                allowed = self.lifecycle_mount_targets(config['services'][role])
                require(set(actual_mounts) == allowed, "Unexpected Lifecycle mount")
            self.evidence("running", {"role": role, "image": c["Image"], "mounts": [{"target": m["Destination"], "rw": m["RW"]} for m in c["Mounts"]]})
        pins = json.loads(self.exec_python("terraform-worker", "import json,subprocess\nfrom iacp.terraform.config import SOURCE_REVISION\nprint(json.dumps({'source':SOURCE_REVISION,'terraform':json.loads(subprocess.check_output(['terraform','version','-json']))['terraform_version']}))"))
        require(pins == {"source": SOURCE, "terraform": "1.16.6"}, "Terraform pin/version mismatch")
        for role in ("api", "lifecycle-worker"):
            require(json.loads(self.exec_python(role, "import json\nfrom iacp.lifecycle.ownership import ownership_revision\nprint(json.dumps(ownership_revision()))")) == SOURCE, "Power ownership pin mismatch")
        report = self.snapshot()
        require(report["schema"] == [HEAD], "Schema is not current head")
        if idle:
            check_idle(report, allowed_uncertain)
            self.workers_idle(lifecycle=True)
        deadline = time.monotonic() + 60
        while True:
            try:
                self.exec_python("api", "import urllib.request; assert urllib.request.urlopen('http://127.0.0.1:8000/health/ready',timeout=5).status == 200")
                break
            except RuntimeError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(2)
        return report

    def activation_controls(self):
        record = self.m["activation_review"]
        require(digest(record["path"]) == record["sha256"], "Activation controls checksum mismatch")
        controls = json.loads(Path(record["path"]).read_text())
        internal = exposure_mode(self.m) == "corporate-internal"
        https_control = "lan_vpn_https_passed" if internal else "external_https_passed"
        require(all(controls.get(k) is True for k in ("assigned_public_ip_validated", "security_controls_passed", https_control, "pve_permissions_passed", "vm104_excluded")), "Activation controls incomplete")
        probe = controls["lan_vpn_probe" if internal else "external_probe"]
        require(digest(probe["path"]) == probe["sha256"], "API probe checksum mismatch")
        proof = json.loads(Path(probe["path"]).read_text())
        require(proof["passed"] is True and proof["host"] == "iac.cyberfuel.com"
                and exposure_mode(proof) == exposure_mode(self.m), "API validation did not pass for this exposure mode")
        if internal:
            self.publication_controls()
            bind, networks = internal_target(self.m)
            require(proof.get("lan_vpn_vantage_asserted") is True and proof.get("external_vantage_asserted") is False
                    and proof["resolved_ips"] == [bind] and proof.get("api_bind_ip") == bind
                    and proof.get("approved_client_cidrs") == [str(net) for net in networks]
                    and proof.get("identity_mode") == self.m["identity_mode"] and proof.get("tls_min_validity_days", 0) >= 14
                    and any(ipaddress.ip_address(proof["runner_ip"]) in net for net in networks), "Authorized LAN/VPN validation missing")
            cfg = json.loads(self.compose("config", "--format", "json"))
            dynamic = self.m["tls_dynamic_file"]
            require(digest(dynamic["path"]) == dynamic["sha256"], "Reviewed TLS routing changed")
            self.proxy_exposure(cfg, json.loads(Path(dynamic["path"]).read_text()))
            self.internal_live_bindings()
        else:
            require(proof["external_vantage_asserted"] is True and proof["resolved_ips"]
                    and all(ipaddress.ip_address(ip).is_global for ip in proof["resolved_ips"]), "External validation did not pass")
        age = datetime.now(timezone.utc) - datetime.fromisoformat(proof["at"])
        require(0 <= age.total_seconds() < 86400, "API validation is stale")
        return controls

    def promote(self):
        change=self.authorize("deploy")
        before = self.preflight()
        recovery_ids=self.review_recovery_deployment(change,before)
        current_api_flags = dict(v.split("=", 1) for v in self.container("api")["Config"]["Env"] if "=" in v)
        restore_flags = {
            "admission": int(current_api_flags.get("IACP_ADMISSION_ENABLED", "0")),
            "lifecycle": int(current_api_flags.get("IACP_KVM_LIFECYCLE_ENABLED", "0")),
            "deletion": int(current_api_flags.get("IACP_TERRAFORM_DELETE_ENABLED", "0")),
        }
        migration = None
        if self.canonical() and before["schema"] == [HEAD] and all(
            self.container(role)["Image"] == self.m["images"]["terraform" if role == "terraform-worker" else "api"] for role in ROLES) and (
                current_api_flags.get("IACP_ADMISSION_ENABLED") == "0" and current_api_flags.get("IACP_KVM_LIFECYCLE_ENABLED") == "0"):
            migration = self.migrate_technical_ownership()
            self.verify(allowed_uncertain=recovery_ids)
            # Incluso en la ruta de adopción sin recreación, restaurar el
            # estado de admisión que estaba habilitado antes del drenaje.
            self.flags(**restore_flags)
            self.up(*ROLES)
            self.verify(admission=restore_flags["admission"],
                        lifecycle=restore_flags["lifecycle"],
                        deletion=restore_flags["deletion"],
                        allowed_uncertain=recovery_ids)
            self.evidence("canonical-adoption", {"passed": True, "containers_recreated": [],
                                                  "schema_changed": False, "ownership_migration": migration,
                                                  "admission_restored": restore_flags["admission"],
                                                  "lifecycle_restored": restore_flags["lifecycle"],
                                                  "deletion_restored": restore_flags["deletion"]})
            return
        # Stop admission at the source, including internal clients. Never stop shared Traefik.
        self.compose("stop", "--timeout", "60", "api", timeout=75)
        self.flags(admission=0, lifecycle=0, deletion=0)
        deadline = time.monotonic() + 300
        while True:
            try:
                before = self.snapshot()
                check_idle(before,recovery_ids)
                self.workers_idle(lifecycle=any(c["Config"]["Labels"].get("com.docker.compose.service") == "lifecycle-worker" for c in self.containers()))
                break
            except RuntimeError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(5)
        self.compose("stop", "--timeout", "60", "worker", "terraform-worker", "lifecycle-worker", timeout=75)
        check_idle(self.snapshot_stopped(),recovery_ids)
        # Alembic owns its version table. Its ordered graph keeps 0005 intact.
        self.compose("run", "--rm", "--no-deps", "--pull", "never", "-T", "migrate", timeout=180)
        self.up(*ROLES)
        migration = self.migrate_technical_ownership()
        self.verify(allowed_uncertain=recovery_ids)
        self.flags(**restore_flags)
        self.up(*ROLES)
        after = self.verify(admission=restore_flags["admission"], lifecycle=restore_flags["lifecycle"],
                            allowed_uncertain=recovery_ids)
        compare_history(before, after, migration)
        self.evidence("promotion-completed", {"schema": HEAD,
                                               "lifecycle": restore_flags["lifecycle"],
                                               "admission": restore_flags["admission"],
                                               "deletion": restore_flags["deletion"],
                                               "history_unchanged": True,
                                               "ownership_migration": migration})

    def review_recovery_deployment(self, change, before):
        if 'recovery_deployment_review' not in self.m:
            return ()
        review=self.operator_review('recovery_deployment_review',change)
        require(self.canonical() and before['schema']==[HEAD] and review.get('mode')=='readonly-apply-recovery'
                and review.get('source_revision')==SOURCE,'Recovery deployment must preserve schema/source')
        cfg=json.loads(self.compose('config','--format','json'))
        api=cfg['services']['api']['environment']
        require(api.get('IACP_ADMISSION_ENABLED')=='0' and api.get('IACP_KVM_LIFECYCLE_ENABLED')=='0'
                and api.get('IACP_TERRAFORM_DELETE_ENABLED','0')=='0','Recovery deployment must stay drained')
        current=next(v['Source'] for v in self.container('terraform-worker')['Mounts'] if v['Destination']=='/iac-source')
        require(current==next(v['source'] for v in cfg['services']['terraform-worker']['volumes'] if v['target']=='/iac-source'),
                'Recovery deployment cannot change IaC source mount')
        from uuid import UUID
        identifier=str(UUID(review['approval_id']))
        code="""import json
from iacp.db import get_session_factory
from iacp.models import TerraformApply,TerraformExecution,Instance,Job
from iacp.terraform.config import SOURCE_REVISION,Allocation,PlanError
from iacp.terraform.lifecycle import build_executor
review=json.loads(REVIEW)
with get_session_factory()() as s:
 row=s.get(TerraformApply,review['approval_id']);assert row and row.status=='uncertain' and row.started_at and row.job_id
 e=s.get(TerraformExecution,row.instance_id);i=s.get(Instance,row.instance_id)
 assert row.instance_id==review['instance_id'] and row.attempt==review['attempt'] and row.plan_sha256==review['plan_sha256']
 assert e.source_revision==SOURCE_REVISION==review['source_revision'] and e.plan_sha256==row.plan_sha256
 assert e.vmid>=30000 and e.vmid not in (104,106) and i.network_mode=='dhcp' and i.status=='failed'
 assert s.get(Job,row.job_id).status=='failed'
 a=Allocation.model_validate(e.allocation);ex=build_executor(observation=True)
 try:ex.observe(i.id,row.attempt,e.allocation,a.variables(i),lambda:False)
 except PlanError as failure:
  assert failure.code=='RESOURCE_ABSENT' and failure.state_present is False and failure.proxmox_present is False
  assert failure.state_consistency['change']=='unchanged'
  print(json.dumps({'approval_id':row.id,'vmid':e.vmid,'absent':True,'state_consistency':failure.state_consistency}))
 else:raise AssertionError('Recovery maintenance is limited to proved empty-state/absent VM')
""".replace('REVIEW',repr(json.dumps(review)))
        result=json.loads(self.exec_python('terraform-worker',code))
        require(result['approval_id']==identifier,'Reviewed recovery identity mismatch')
        self.evidence('fenced-recovery-readonly',result)
        return (identifier,)

    def operator_review(self, key, change):
        reference = self.m[key]
        require(digest(reference['path']) == reference['sha256'], "Operator review checksum mismatch")
        review = json.loads(Path(reference['path']).read_text())
        require(review.get('approved') is True and review.get('release') == self.m['release']
                and review.get('change_reference') == change['reference'], "Operator review missing or for another change")
        return review

    def activate_core(self):
        """Habilita el contrato técnico con auditoría del operador."""
        self.authorize('activate-core')
        self.preflight()
        self.verify(admission=0, lifecycle=0)
        self.flags(admission=1, lifecycle=1, deletion=1)
        self.up('api', 'terraform-worker', 'lifecycle-worker')
        self.verify(admission=1, lifecycle=1)
        self.evidence('core-contract-enabled', {
            'contract': 'provisioning-api-core',
            'admission': 1,
            'lifecycle': 1,
            'deletion': 1,
        })

    def admit(self):
        change = self.authorize('enable-admission')
        self.preflight()
        self.verify()  # Idle, admission=0, Lifecycle=0; does not need VM106 activation.
        review = self.operator_review('admission_review', change)
        cfg = json.loads(self.compose('config', '--format', 'json'))
        purpose=review.get('purpose','static-provisioning')
        if purpose=='dhcp-provisioning':
            path=next(v['source'] for v in cfg['services']['terraform-worker']['volumes'] if v['target']=='/run/config/profiles.json')
            require(review['profile_file']=={'path':path,'sha256':digest(path)},'Profile review mismatch')
            require(bool(review['profiles']),'Reviewed profiles required')
            code="""import json
from iacp.terraform.profiles import load_profile,cluster_vmids
pairs=json.loads(REVIEW)
used=cluster_vmids()
for pair in pairs:
 p=load_profile(pair['profile'],pair['tenant_id'])
 assert p.settings.network_mode=='dhcp' and p.settings.vlan_id is None
 assert any(v not in used for v in range(p.vmid_min,p.vmid_max+1))
print('DHCP_PROFILE_AUTHORIZED')
""".replace('REVIEW',repr(json.dumps(review['profiles'])))
            self.exec_python('api',code)
        elif purpose=='existing-disposable':
            require(bool(review['identities']),'Existing disposable identities required')
            self.review_instances(review['identities'])
            path=self.m['profile_configuration']['path']
        else:
            require(purpose=='static-provisioning','Unknown admission purpose')
            path = next(v['source'] for v in cfg['services']['terraform-worker']['volumes']
                        if v['target'] == '/run/config/allocations.json')
            require(review['allocation_file'] == {'path': path, 'sha256': digest(path)}, "IPAM allocation review mismatch")
            require(bool(review['targets']), "At least one new lab target must be reviewed")
            # Validate the real mounted allocation and cluster-wide absence using the
            # existing read-only client; unsupported DHCP cannot pass this boundary.
            code = '''import json, tempfile
from pathlib import Path
from iacp.terraform.config import Allocation
from iacp.terraform.lifecycle import build_executor
from iacp.terraform.observation import proxmox_resource
from iacp.db import get_session_factory
from iacp.models import Tenant, Instance
from sqlalchemy import select
from ipaddress import IPv4Interface
targets = json.loads(REVIEW)
import os
entries = json.loads(Path(os.environ['IACP_TERRAFORM_ALLOCATIONS_FILE']).read_text())
with tempfile.TemporaryDirectory() as directory:
 env = build_executor(observation=True).environment(Path(directory))
 for key in targets:
  tenant,name=key.split('/',1)
  with get_session_factory()() as s:
   assert s.get(Tenant,tenant) and s.get(Tenant,tenant).active
   assert s.scalar(select(Instance.id).where(Instance.tenant_id==tenant,Instance.name==name)) is None
  a = Allocation.model_validate(entries[key])
  assert a.vm_id >= 30000 and a.vm_id not in (104,106)
  assert sum(v['vm_id'] == a.vm_id for v in entries.values()) == 1
  assert sum(IPv4Interface(v['ipv4_cidr']).ip == IPv4Interface(a.ipv4_cidr).ip for v in entries.values()) == 1
  assert proxmox_resource(env, {'vm_id': a.vm_id, 'node_name': a.node_name, 'vm_name': key.split('/',1)[1]}, lambda:False)['present'] is False
print('AUTHORIZED_NEW_LAB_TARGETS')

'''.replace('REVIEW', repr(json.dumps(review['targets'])))
            self.exec_python('terraform-worker', code)
        try:
            self.flags(admission=1, lifecycle=0)
            self.up('api')
            self.verify(admission=1, lifecycle=0)
        except Exception:
            self.flags(admission=0, lifecycle=0)
            self.up('api')
            raise
        self.evidence('admission-enabled', {'lifecycle': 0, 'apply': 1, 'allocation_sha256': digest(path)})

    def review_instances(self, identities):
        code="""import json
from iacp.db import get_session_factory
from iacp.models import Instance,Tenant,TerraformExecution
from iacp.lifecycle.service import managed_target
from iacp.terraform.config import SOURCE_REVISION
with get_session_factory()() as s:
 for identifier,expected in json.loads(__REVIEW_IDENTITIES__).items():
  i=s.get(Instance,identifier);assert i and s.get(Tenant,i.tenant_id).active
  t,a=managed_target(s,i)
  assert t.vmid>=30000 and t.vmid not in (104,106) and i.network_mode=='dhcp'
  assert expected=={'vmid':t.vmid,'node':t.node,'name':t.name}
  assert s.get(TerraformExecution,identifier).source_revision==SOURCE_REVISION
print('REVIEWED_DISPOSABLE_IDENTITIES')
""".replace('__REVIEW_IDENTITIES__',repr(json.dumps(identities)))
        self.exec_python('api',code)

    def activate_pilot(self):
        change=self.authorize('activate-pilot')
        self.verify(admission=1,lifecycle=0)
        review=self.operator_review('pilot_review',change)
        require(review.get('lab_network_authorized') is True,'Lab network authorization required')
        require(bool(review['identities']),'Explicit disposable identities required')
        self.review_instances(review['identities'])
        proof=review['dhcp_evidence'];require(digest(proof['path'])==proof['sha256'],'DHCP evidence mismatch')
        require(json.loads(Path(proof['path']).read_text()).get('dhcp_offer_received') is True,'Functional DHCP not demonstrated')
        https=review['https_evidence'];require(digest(https['path'])==https['sha256'],'HTTPS evidence mismatch')
        data=json.loads(Path(https['path']).read_text())
        require(data.get('passed') is True and data.get('tls_verified') is True and data.get('host')=='iac.cyberfuel.com','Verified LAN/VPN HTTPS required')
        age=datetime.now(timezone.utc)-datetime.fromisoformat(data['at'])
        require(0<=age.total_seconds()<86400,'Pilot HTTPS evidence is stale')
        checks={(c['path'],c['identity'],c['http']) for c in data['checks'] if c.get('passed') is True}
        require({('/health/ready','anonymous',200),('/docs','anonymous',401),('/docs','docs',200),
                 ('/openapi.json','anonymous',401),('/openapi.json','docs',200)}<=checks,'Pilot documentation authentication not validated')
        # Confirm exact native networking and a stable, owned state before power.
        code="""import json,tempfile
from pathlib import Path
from iacp.db import get_session_factory
from iacp.models import Instance,TerraformExecution
from iacp.terraform.config import Allocation
from iacp.terraform.lifecycle import build_executor
from iacp.terraform.state import S3StateReader
from iacp.terraform.executor import state_resource
from iacp.terraform.observation import proxmox_resource
with get_session_factory()() as s, tempfile.TemporaryDirectory() as scratch:
 env=build_executor(observation=True).environment(Path(scratch))
 for identifier in json.loads(REVIEW):
  i=s.get(Instance,identifier);e=s.get(TerraformExecution,identifier)
  a=Allocation.model_validate(e.allocation);assert a.network_mode=='dhcp' and a.vlan_id is None
  variables=a.variables(i)
  p=proxmox_resource(env,variables['instance'],lambda:False);assert p['present'] and p['power_state']=='stopped'
  r=S3StateReader(a.backend.arguments(e.state_key),env)
  try:
   state=r.read(lambda:False);assert state_resource(state.state,variables) is not None
   state.verify(r.read(lambda:False))
  finally:r.close()
print('PILOT_OWNERSHIP_NETWORK_STATE_VALID')
""".replace('REVIEW',repr(json.dumps(list(review['identities']))))
        self.exec_python('terraform-worker',code)
        identifiers=list(review['identities'])
        try:
            self.flags(admission=1,lifecycle=1)
            self.up('api','lifecycle-worker')
            self.verify(admission=1,lifecycle=1)
        except Exception:
            self.flags(admission=0,lifecycle=0)
            self.up('api','lifecycle-worker')
            raise
        self.evidence('pilot-activated',{'instance_ids':identifiers,'vm106_authorized':False,'apply':1})

    def enable_delete(self):
        change = self.authorize('enable-delete')
        self.preflight()
        self.verify()
        review = self.operator_review('deletion_review', change)
        from uuid import UUID
        identifiers = [str(UUID(v)) for v in review['instance_ids']]
        require(bool(identifiers) and len(set(identifiers)) == len(identifiers), "Unique disposable UUIDs required")
        require(set(review['identities']) == set(identifiers), "Explicit reviewed VMID/node/name required per UUID")
        code = '''import json
from iacp.db import get_session_factory
from iacp.models import Instance, Tenant, TerraformExecution
from iacp.lifecycle.service import managed_target
from iacp.terraform.config import SOURCE_REVISION, Allocation
with get_session_factory()() as s:
 for identifier,expected in json.loads(REVIEW).items():
  i=s.get(Instance,identifier);assert i and s.get(Tenant,i.tenant_id).active
  t,a=managed_target(s,i);assert t.vmid >= 30000 and t.vmid not in (104,106)
  assert expected == {'vmid':t.vmid,'node':t.node,'name':t.name}
  e=s.get(TerraformExecution,identifier);assert e.source_revision == SOURCE_REVISION
  Allocation.model_validate(e.allocation)
print('DISPOSABLE_PROVISIONING_VERIFIED')
'''.replace('REVIEW', repr(json.dumps(review['identities'])))
        self.exec_python('api', code)
        try:
            self.flags(admission=0, lifecycle=0, deletion=1)
            self.up('terraform-worker', 'api')
            self.verify()
        except Exception:
            self.flags(admission=0, lifecycle=0, deletion=0)
            self.up('terraform-worker', 'api')
            raise
        self.evidence('delete-enabled', {'instance_ids': identifiers, 'admission': 0, 'lifecycle': 0, 'apply': 1})

    def snapshot_stopped(self):
        # All application containers are now stopped. Read through a one-shot
        # migrate service with exactly the same DB secret; no migration is executed.
        report = json.loads(self.compose("run", "--rm", "--no-deps", "--pull", "never", "-T", "--entrypoint", "python", "migrate", "-",
                                         input=(ROOT / "scripts/runtime_snapshot.py").read_text()))
        self.evidence("snapshot-stopped", report)
        return report

    def certify(self, *, record=False):
        change = self.authorize("record-certificate" if record else "validate-certificate")
        self.verify()
        request = json.loads(Path(self.m["expectations_file"]).read_text())
        require(request["actor"] == change["actor"] and request["change_reference"] == change["reference"], "Certification actor/change mismatch")
        require(request["release_reference"] == self.m["release_reference"], "Certification release mismatch")
        if record:
            reviewed = self.m["certification_review"]
            require(digest(reviewed["path"]) == reviewed["sha256"], "Unreviewed certification evidence")
            validated = json.loads(Path(reviewed["path"]).read_text())["data"]
            require(validated.get("validated") is True and validated.get("recorded") is False, "Read-only certification must pass first")
        args = ["run", "--rm", "--no-deps", "--pull", "never", "-T", "ownership-certifier", "python", "-m", "iacp.lifecycle.certify", "--expectations", "/dev/stdin"]
        if record:
            args.append("--record")
        result = json.loads(self.compose(*args, input=json.dumps(request), timeout=300))
        require(result["validated"] is True and result["recorded"] is record, "Certification did not pass")
        self.evidence("certificate", result)

    def expectations(self):
        change = self.authorize("validate-certificate")
        self.verify()
        result = json.loads(self.compose("run", "--rm", "--no-deps", "--pull", "never", "-T",
            "-e", "CERT_ACTOR=" + change["actor"], "-e", "CERT_CHANGE=" + change["reference"],
            "-e", "CERT_RELEASE=" + self.m["release_reference"], "--entrypoint", "python", "ownership-certifier", "-",
            input=(ROOT / "scripts/vm106_expectations.py").read_text(), timeout=90))
        private_json(self.m["expectations_file"], result)
        self.evidence("expectations", {"sha256": digest(self.m["expectations_file"])})

    def activate(self):
        self.authorize("activate-lifecycle")
        controls = self.activation_controls()
        allocated = str(ipaddress.ip_interface(self.state()["assigned_ipv4_cidr"]).ip)
        require(ipaddress.ip_address(allocated).is_global and allocated == controls["assigned_public_ip"], "Assigned public IP is not validated")
        current = dict(v.split("=", 1) for v in self.container("api")["Config"]["Env"])
        if current.get("IACP_KVM_LIFECYCLE_ENABLED") == "1":
            self.verify(admission=1, lifecycle=1)
            self.evidence("activation", {"already_active": True})
            return
        self.verify()
        # Re-run certification and binding verification against DB/state/PVE. The
        # idempotent record command returns the already recorded ID, never backfills.
        certificate = self.m["recorded_certificate"]
        require(digest(certificate["path"]) == certificate["sha256"], "Recorded certificate checksum mismatch")
        data = json.loads(Path(certificate["path"]).read_text())["data"]
        require(data["recorded"] is True and data["certificate_id"], "No recorded certificate")
        self.certify(record=False)
        found = json.loads(self.exec_python("api", "from iacp.db import get_session_factory\nfrom iacp.models import PowerOwnershipCertificate\nimport json\nwith get_session_factory()() as s:\n c=s.get(PowerOwnershipCertificate," + repr(data["certificate_id"]) + ")\n print(json.dumps(bool(c and c.instance_id == 'a1b2d063-6240-4969-bc04-e7e0e6be9e73')))"))
        require(found, "Certificate not present in operational database")
        self.flags(admission=1, lifecycle=1)
        self.up("api", "lifecycle-worker")
        self.verify(admission=1, lifecycle=1)

    def state(self):
        return json.loads(self.compose("run", "--rm", "--no-deps", "--pull", "never", "-T", "--entrypoint", "python", "ownership-certifier", "-",
                                      input=(ROOT / "scripts/vm106_state.py").read_text(), timeout=60))

    def api_credentials(self):
        from smoke_public import private_secret
        token = private_secret(Path(self.m["operator_token_file"]))
        gateway = exposure_mode(self.m) == "public" or self.m["identity_mode"] == "gateway"
        cookie = private_secret(Path(self.m["gateway_cookie_file"])) if gateway else None
        return token, cookie

    def first_start(self):
        self.authorize("first-start")
        controls = self.activation_controls()
        intent_path = Path(self.m["start_intent_file"])
        before = self.verify(admission=1, lifecycle=1, idle=not intent_path.exists())
        from smoke_public import request
        token, cookie = self.api_credentials()
        context = ssl.create_default_context(cafile=self.m.get("api_ca_file"))
        key = self.m["start_idempotency_key"]
        require(re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", key), "Invalid fixed start key")
        path = "/v1/instances/a1b2d063-6240-4969-bc04-e7e0e6be9e73/power"

        def api(suffix, **kwargs):
            status, _, body = request(suffix, token=token, cookie=cookie, context=context, **kwargs)
            require(status == (202 if kwargs.get("method") == "POST" else 200), "API request rejected or ambiguous; preserve intent and SAME key")
            return json.loads(body)

        observed = api(path)
        require(observed["guest_ready"] is None, "Guest readiness must remain unknown")
        state = self.state()
        ip = str(ipaddress.ip_interface(state["assigned_ipv4_cidr"]).ip)
        require(ipaddress.ip_address(ip).is_global and ip == controls["assigned_public_ip"], "Assigned public IP was not independently validated")
        watched = {name: before["tables"][name] for name in ("instances", "terraform_executions", "terraform_attempts", "terraform_applies")}
        if intent_path.exists():
            intent = json.loads(intent_path.read_text())
            require(intent["key"] == key and intent["release"] == self.m["release"] and intent["state"] == state["state"], "Start intent changed; investigate")
        else:
            require(observed["power_state"] == "stopped" and observed["mutations_available"] is True
                    and observed["blocked_reason"] is None, "VM106 is not ready for the controlled start")
            intent = {"key": key, "release": self.m["release"], "state": state["state"], "history": watched, "at": stamp()}
            private_json(intent_path, intent)
        # A resumed invocation repeats this exact key. API dispatch fencing owns
        # recovery; the driver never sends a direct Proxmox POST or another action.
        accepted = api(path + "/start", method="POST", headers={"Idempotency-Key": key})
        operation_id = accepted["operation_id"]
        self.evidence("power-accepted", accepted)
        deadline = time.monotonic() + 120
        while True:
            operation = api("/v1/operations/" + operation_id)
            if operation["status"] in {"completed", "failed", "uncertain"}:
                break
            require(time.monotonic() < deadline, "Observation timeout; retain operation and SAME key")
            time.sleep(2)
        self.evidence("power-operation", operation)
        require(operation["status"] == "completed" and sum(e["code"] == "power_completed" for e in operation["events"]) == 1, "Power operation requires investigation")
        observed = api(path)
        require(observed["power_state"] == "running" and observed["guest_ready"] is None, "Power postcondition failed")
        require(self.state()["state"] == intent["state"], "Terraform state changed during power operation")
        after = self.snapshot()
        require(all(after["tables"][name] == value for name, value in intent["history"].items()), "Provisioning history changed")
        self.evidence("first-start-completed", {"operation_id": operation_id, "state_unchanged": True, "history_unchanged": True,
                                               "power_state": "running", "guest_ready": None})

    def rollback(self):
        self.authorize("functional-rollback")
        # API closes admission first. Known UPIDs keep observing with lifecycle=0.
        # Do not stop a worker while it may still be observing a native task.
        self.flags(admission=0, lifecycle=0, deletion=0)
        self.up("api")
        self.workers_idle(lifecycle=True)
        self.up("lifecycle-worker", "terraform-worker")
        report = self.snapshot()
        require(report["schema"] == [HEAD], "Keep current schema head")
        self.evidence("rollback", {"lifecycle": 0, "admission": 0, "apply": 1, "schema": HEAD})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["inventory", "preflight", "promote", "publish", "verify", "expectations", "validate-certificate", "record-certificate", "activate", "first-start", "rollback", "admit", "enable-delete", "activate-pilot", "activate-core", "attest-root-transport", "recover-queued-guest-init"])
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--evidence-dir", type=Path, required=True, help="New private directory, outside the checkout")
    parser.add_argument("--instance-id", help="UUID exacto de la instancia para la recuperación")
    parser.add_argument("--vmid", type=int, help="VMID exacto de la instancia para la recuperación")
    parser.add_argument("--operation-id", help="UUID exacto de la operación de Guest Initialization")
    parser.add_argument("--job-id", help="UUID exacto del Job de Guest Initialization")
    parser.add_argument("--operator", help="Identidad del operador del registro de cambio")
    parser.add_argument("--change-reference", help="Referencia del registro de cambio")
    args = parser.parse_args()
    os.umask(0o077)
    output = args.evidence_dir.resolve()
    require(not output.is_relative_to(ROOT), "Keep operational evidence outside the checkout")
    recovery_request = None
    if args.action == "recover-queued-guest-init":
        from uuid import UUID
        values = {"instance_id": args.instance_id, "operation_id": args.operation_id, "job_id": args.job_id}
        for field, value in values.items():
            try:
                canonical = str(UUID(value))
            except (AttributeError, TypeError, ValueError) as error:
                raise RuntimeError(field + " must be a canonical UUID") from error
            require(canonical == value, field + " must be a canonical UUID")
        require(args.vmid is not None and args.vmid >= 30000,
                "A managed VMID is required for this recovery action")
        if args.operator is not None:
            require(len(args.operator) <= 128, "Operator identity is invalid")
        if args.change_reference is not None:
            require(len(args.change_reference) <= 256, "Change reference is invalid")
        recovery_request = {
            **values,
            "vmid": args.vmid,
            "actor": args.operator,
            "change_reference": args.change_reference,
        }
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    manifest = json.loads(args.manifest.read_text())
    private_json(output / "manifest-digest.json", {"sha256": digest(args.manifest), "action": args.action, "at": stamp()})
    try:
        promotion = Promotion(manifest, output)
        # flock is host-local; external writers still require explicit exclusion.
        with open(Path(manifest["environment_file"]).parent / ".promotion.lock", "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if args.action == "inventory":
                promotion.snapshot()
                promotion.workers_idle(lifecycle=any(c["Config"]["Labels"].get("com.docker.compose.service") == "lifecycle-worker" for c in promotion.containers()))
            elif args.action == "recover-queued-guest-init":
                promotion.recover_queued_guest_init(recovery_request)
            elif args.action in {"validate-certificate", "record-certificate"}:
                promotion.certify(record=args.action == "record-certificate")
            else:
                getattr(promotion, args.action.replace("-", "_"))()
        private_json(output / "result.json", {"passed": True, "action": args.action, "at": stamp()})
        print(json.dumps({"passed": True, "evidence_dir": str(output)}))
    except Exception as error:
        # Only local driver messages are safe. Subprocess output is never retained.
        reason = str(error) if type(error) is RuntimeError else type(error).__name__
        private_json(output / "result.json", {"passed": False, "reason": reason, "at": stamp()})
        parser.exit(1, "Promotion stopped: " + reason + ". Admission is not automatically reopened.\n")


if __name__ == "__main__":
    main()
