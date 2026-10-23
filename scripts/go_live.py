#!/usr/bin/env python3
"""Operator promotion driver. No schema rollback, state mutation or direct PVE POST.

Approvals are externally reviewed change records, never inferred from a CLI flag.
The explicit first-start phase uses IACP only. Failed promotion never reopens
admission automatically. Resume a stage only after reviewing its journal.
"""
import argparse
from datetime import datetime, timezone
import fcntl
import hashlib
import ipaddress
import json
import os
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

HEAD = "0006_power_ownership"
SOURCE = "9aeb8ec846342d279a5d16be7c8e20157e9bf5ae"
ARCHIVE = "98f0900d571a8d05e4bbb0eabdb10550a372655f61fda35aaa3a601537daa94a"
ROLES = ["api", "worker", "terraform-worker", "lifecycle-worker"]


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def digest(path):
    value = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def stamp():
    return datetime.now(timezone.utc).isoformat()


def private_json(path, value):
    # Evidence is immutable. A rerun gets a new numbered artifact.
    with Path(path).open("x", encoding="utf8") as stream:
        os.chmod(path, 0o600)
        json.dump(value, stream, sort_keys=True, indent=2)
        stream.write("\n")


def check_idle(report):
    require(not any(report["nonterminal"].values()), "Nonterminal DB work; use its approved recovery workflow")
    require(not any(report["queues"].values()) and report["unacked"] == 0, "Broker has pending work")


def compare_history(before, after):
    for name, evidence in before["tables"].items():
        require(after["tables"].get(name) == evidence, "Historical rows changed: " + name)


class Promotion:
    def __init__(self, manifest, output):
        self.m = manifest
        self.output = output
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

    def flags(self, *, admission, lifecycle):
        path = Path(self.m["environment_file"])
        require(not path.is_symlink(), "Environment file must not be a symlink")
        original = path.read_text()
        values = {"IACP_ADMISSION_ENABLED": str(admission), "IACP_KVM_LIFECYCLE_ENABLED": str(lifecycle),
                  "IACP_TERRAFORM_APPLY_ENABLED": "1"}
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
            comparison = compare(current, cfg, self.m.get("configuration_changes", []))
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
        lifecycle = services["lifecycle-worker"]
        ca_target = lifecycle["environment"].get("IACP_KVM_CA_FILE")
        ca_mounts = lifecycle.get("volumes", [])
        require(all(item["type"] == "bind" and item["target"] == ca_target and item.get("read_only") is True for item in ca_mounts), "Lifecycle may mount only its public CA readonly")
        require({s["source"] for s in lifecycle["secrets"]} == {"db_password", "kvm_credentials"}
                and lifecycle.get("read_only") is True, "Lifecycle isolation changed")
        for role in ("api", "worker", "terraform-worker"):
            old_mounts = {m["Destination"]: m["Source"] for m in self.container(role)["Mounts"] if m["Destination"].startswith("/run/")}
            planned = self.planned_mounts(cfg, role)
            for target, source in old_mounts.items():
                require(planned.get(target) == ("bind", source, False), "Existing secret/config mount changed")
        source_mount = next(m for m in services["terraform-worker"]["volumes"] if m["target"] == "/iac-source")
        require(source_mount["read_only"] is True, "IaC source must be readonly")
        archive = subprocess.run(["git", "-c", "safe.directory=" + source_mount["source"], "-C", source_mount["source"], "archive", SOURCE],
                                 capture_output=True, timeout=30, env=self.env, check=True).stdout
        require(hashlib.sha256(archive).hexdigest() == ARCHIVE, "IaC checksum mismatch")
        backup = self.m["backup"]
        require(digest(backup["path"]) == backup["sha256"], "Backup checksum mismatch")
        require(digest(backup["restore_evidence"]) == backup["restore_evidence_sha256"], "Restore evidence checksum mismatch")
        restored = json.loads(Path(backup["restore_evidence"]).read_text())
        require(restored["passed"] is True and restored["isolated"] is True
                and restored["backup_sha256"] == backup["sha256"] and restored["schema"] == "0004_authorized_apply"
                and restored["table_count"] >= 9, "Restore attestation is incomplete")
        report = self.snapshot()
        require(report["schema"] in [["0004_authorized_apply"], ["0005_kvm_lifecycle"], [HEAD]], "Unsupported schema")
        self.evidence("preflight", {"passed": True, "configuration_sha256": hashlib.sha256(json.dumps(cfg, sort_keys=True).encode()).hexdigest()})
        return report

    def canonical(self):
        if self.m.get("configuration_mode") != "canonical":
            return False
        files = self.m["compose_files"]
        require(len(files) == 1 and Path(files[0]).parts[0] == "deploy"
                and Path(files[0]).name == "compose.yaml" and (ROOT / files[0]).resolve().is_relative_to(ROOT / "deploy"), "One versioned canonical Compose required")
        return True

    def publish(self):
        self.approve("publish-api")
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

    def verify(self, *, admission=0, lifecycle=0, idle=True):
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
            if role in {"api", "lifecycle-worker"}:
                require(values.get("IACP_KVM_LIFECYCLE_ENABLED", "0") == str(lifecycle), "Lifecycle flag mismatch")
            if role == "api":
                require(values.get("IACP_ADMISSION_ENABLED") == str(admission), "Admission flag mismatch")
            if role == "lifecycle-worker":
                require(not c["HostConfig"]["PortBindings"] and c["HostConfig"]["ReadonlyRootfs"], "Lifecycle container is not isolated")
                allowed = {"/run/secrets/db_password", "/run/secrets/kvm_credentials"}
                if values.get("IACP_KVM_CA_FILE"):
                    allowed.add(values["IACP_KVM_CA_FILE"])
                require(set(actual_mounts) == allowed, "Unexpected Lifecycle mount")
            self.evidence("running", {"role": role, "image": c["Image"], "mounts": [{"target": m["Destination"], "rw": m["RW"]} for m in c["Mounts"]]})
        pins = json.loads(self.exec_python("terraform-worker", "import json,subprocess\nfrom iacp.terraform.config import SOURCE_REVISION\nprint(json.dumps({'source':SOURCE_REVISION,'terraform':json.loads(subprocess.check_output(['terraform','version','-json']))['terraform_version']}))"))
        require(pins == {"source": SOURCE, "terraform": "1.16.6"}, "Terraform pin/version mismatch")
        for role in ("api", "lifecycle-worker"):
            require(json.loads(self.exec_python(role, "import json\nfrom iacp.lifecycle.ownership import ownership_revision\nprint(json.dumps(ownership_revision()))")) == SOURCE, "Power ownership pin mismatch")
        report = self.snapshot()
        require(report["schema"] == [HEAD], "Schema is not 0006")
        if idle:
            check_idle(report)
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
        self.approve("deploy")
        before = self.preflight()
        current_api_flags = dict(v.split("=", 1) for v in self.container("api")["Config"]["Env"] if "=" in v)
        if self.canonical() and before["schema"] == [HEAD] and all(
            self.container(role)["Image"] == self.m["images"]["terraform" if role == "terraform-worker" else "api"] for role in ROLES) and (
                current_api_flags.get("IACP_ADMISSION_ENABLED") == "0" and current_api_flags.get("IACP_KVM_LIFECYCLE_ENABLED") == "0"):
            self.verify()
            self.evidence("canonical-adoption", {"passed": True, "containers_recreated": [], "schema_changed": False})
            return
        # Stop admission at the source, including internal clients. Never stop shared Traefik.
        self.compose("stop", "--timeout", "60", "api", timeout=75)
        self.flags(admission=0, lifecycle=0)
        deadline = time.monotonic() + 300
        while True:
            try:
                before = self.snapshot()
                check_idle(before)
                self.workers_idle(lifecycle=any(c["Config"]["Labels"].get("com.docker.compose.service") == "lifecycle-worker" for c in self.containers()))
                break
            except RuntimeError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(5)
        self.compose("stop", "--timeout", "60", "worker", "terraform-worker", "lifecycle-worker", timeout=75)
        check_idle(self.snapshot_stopped())
        # Alembic owns its version table. Its ordered graph keeps 0005 intact.
        self.compose("run", "--rm", "--no-deps", "--pull", "never", "-T", "migrate", timeout=180)
        self.up(*ROLES)
        after = self.verify()
        compare_history(before, after)
        self.evidence("promotion-completed", {"schema": HEAD, "lifecycle": 0, "admission": 0, "history_unchanged": True})

    def snapshot_stopped(self):
        # All application containers are now stopped. Read through a one-shot
        # migrate service with exactly the same DB secret; no migration is executed.
        report = json.loads(self.compose("run", "--rm", "--no-deps", "--pull", "never", "-T", "--entrypoint", "python", "migrate", "-",
                                         input=(ROOT / "scripts/runtime_snapshot.py").read_text()))
        self.evidence("snapshot-stopped", report)
        return report

    def certify(self, *, record=False):
        change = self.approve("record-certificate" if record else "validate-certificate")
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
        change = self.approve("validate-certificate")
        self.verify()
        result = json.loads(self.compose("run", "--rm", "--no-deps", "--pull", "never", "-T",
            "-e", "CERT_ACTOR=" + change["actor"], "-e", "CERT_CHANGE=" + change["reference"],
            "-e", "CERT_RELEASE=" + self.m["release_reference"], "--entrypoint", "python", "ownership-certifier", "-",
            input=(ROOT / "scripts/vm106_expectations.py").read_text(), timeout=90))
        private_json(self.m["expectations_file"], result)
        self.evidence("expectations", {"sha256": digest(self.m["expectations_file"])})

    def activate(self):
        self.approve("activate-lifecycle")
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
        self.approve("first-start")
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
        self.approve("functional-rollback")
        # API closes admission first. Known UPIDs keep observing with lifecycle=0.
        # Do not stop a worker while it may still be observing a native task.
        self.flags(admission=0, lifecycle=0)
        self.up("api")
        self.workers_idle(lifecycle=True)
        self.up("lifecycle-worker")
        report = self.snapshot()
        require(report["schema"] == [HEAD], "Keep schema 0006")
        self.evidence("rollback", {"lifecycle": 0, "admission": 0, "apply": 1, "schema": HEAD})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["inventory", "preflight", "promote", "publish", "verify", "expectations", "validate-certificate", "record-certificate", "activate", "first-start", "rollback"])
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--evidence-dir", type=Path, required=True, help="New private directory, outside the checkout")
    args = parser.parse_args()
    os.umask(0o077)
    output = args.evidence_dir.resolve()
    require(not output.is_relative_to(ROOT), "Keep operational evidence outside the checkout")
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
