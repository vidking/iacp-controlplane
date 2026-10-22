"""Snapshots del objeto S3: lectura de bytes y versión en una misma respuesta GET."""
from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from .config import Cancelled, PlanError

LIMIT = 16 * 1024 * 1024


def canonical_state(state):
    """Ordenar solo colecciones estructurales sin orden; conservar todos los datos.

    No ordenar listas de atributos del provider: pueden tener semántica posicional.
    Terraform 1.16.6 serializa check_results recorriendo mapas Go sin ordenarlos.
    """
    value = deepcopy(state)
    key = lambda item: json.dumps(item, sort_keys=True, separators=(",", ":"), allow_nan=False)
    for check in value.get("check_results") or []:
        if isinstance(check.get("objects"), list):
            check["objects"].sort(key=key)
    if isinstance(value.get("check_results"), list):
        value["check_results"].sort(key=key)
    for resource in value.get("resources") or []:
        if isinstance(resource.get("instances"), list):
            resource["instances"].sort(key=key)
    if isinstance(value.get("resources"), list):
        value["resources"].sort(key=key)
    return key(value).encode()


def unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("Duplicate JSON key")
        value[key] = item
    return value


def invalid_constant(value):
    raise ValueError("Non-finite JSON number")


@dataclass(frozen=True)
class StateSnapshot:
    state: dict
    version_id: str | None
    sha256: str
    content_sha256: str

    @classmethod
    def parse(cls, data, version_id):
        try:
            state = json.loads(data, object_pairs_hook=unique_object,
                               parse_constant=invalid_constant)
            if (not isinstance(state, dict) or type(state.get("version")) is not int or state["version"] != 4
                    or not isinstance(state.get("lineage"), str) or not state["lineage"]
                    or type(state.get("serial")) is not int or state["serial"] < 0
                    or not isinstance(state.get("resources"), list)):
                raise ValueError("Invalid state header")
            if version_id is not None and not isinstance(version_id, str):
                raise ValueError("Invalid object version")
            digest = hashlib.sha256(canonical_state(state)).hexdigest()
        except (ValueError, TypeError, AttributeError, KeyError, UnicodeError):
            raise PlanError("INVALID_TERRAFORM_OUTPUT", phase="observe.state") from None
        return cls(state, version_id, hashlib.sha256(data).hexdigest(), digest)

    def metadata(self):
        return {"version_id": self.version_id, "lineage": self.state["lineage"],
                "serial": self.state["serial"], "sha256": self.sha256,
                "content_sha256": self.content_sha256}

    def verify(self, latest):
        before, after = self.metadata(), latest.metadata()
        if before == after:
            # Sin VersionId inmutable no podemos detectar una escritura ABA.
            if not self.version_id or self.version_id == "null":
                failure = PlanError("STATE_VERSION_UNAVAILABLE", phase="observe.state_verify")
                failure.state_consistency = {"change": "version_unavailable", "before": before, "after": after}
                raise failure
            return {"change": "unchanged", "before": before, "after": after}
        if before["lineage"] != after["lineage"]:
            change = "lineage_changed"
        elif before["serial"] != after["serial"]:
            change = "serial_advanced" if after["serial"] > before["serial"] else "stale_or_regressed"
        elif before["content_sha256"] != after["content_sha256"]:
            change = "content_changed"
        elif before["sha256"] != after["sha256"]:
            change = "representation_rewrite"
        else:
            change = "version_rewrite"
        if self.version_id == latest.version_id and self.sha256 != latest.sha256:
            change = "inconsistent_version_read"
        failure = PlanError("STATE_CHANGED_DURING_OBSERVATION", phase="observe.state_verify")
        failure.state_consistency = {"change": change, "before": before, "after": after}
        raise failure


class S3StateReader:
    """Sin PUT/DELETE, sin credenciales ambientales ni fallback a otro backend."""
    def __init__(self, backend, env):
        self.bucket, self.key = backend["bucket"], backend["key"]
        self.client = boto3.Session().client(
            "s3", endpoint_url=backend["endpoints"]["s3"], region_name=backend["region"],
            aws_access_key_id=env["AWS_ACCESS_KEY_ID"], aws_secret_access_key=env["AWS_SECRET_ACCESS_KEY"],
            aws_session_token=env.get("AWS_SESSION_TOKEN"), verify=env.get("SSL_CERT_FILE", True),
            config=Config(signature_version="s3v4", s3={"addressing_style": "path"}, proxies={},
                          connect_timeout=5, read_timeout=30, retries={"mode": "standard", "total_max_attempts": 2}))

    def read(self, cancelled, phase="observe.state"):
        if cancelled():
            raise Cancelled()
        try:
            response = self.client.get_object(Bucket=self.bucket, Key=self.key)
            with response["Body"] as body:
                data = body.read(LIMIT + 1)
            if cancelled():
                raise Cancelled()
            if len(data) > LIMIT:
                raise PlanError("OUTPUT_LIMIT_EXCEEDED", phase=phase)
            if len(data) != response["ContentLength"] or response["ResponseMetadata"]["HTTPStatusCode"] != 200:
                raise PlanError("STATE_BACKEND_READ_FAILED", phase=phase)
            return StateSnapshot.parse(data, response.get("VersionId"))
        except self.client.exceptions.NoSuchKey:
            # No inicializar un objeto nuevo ni inferir ausencia de infraestructura.
            raise PlanError("STATE_BACKEND_OBJECT_MISSING", phase=phase) from None
        except (ClientError, BotoCoreError, OSError) as error:
            status = error.response.get("ResponseMetadata", {}).get("HTTPStatusCode") if isinstance(error, ClientError) else None
            raise PlanError("STATE_BACKEND_READ_FAILED", phase=phase, http_status=status) from None
        except (ValueError, TypeError, KeyError):
            raise PlanError("STATE_BACKEND_READ_FAILED", phase=phase) from None
        except PlanError as failure:
            failure.phase = phase
            raise

    def close(self):
        self.client.close()
