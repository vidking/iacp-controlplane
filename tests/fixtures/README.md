# Fixture refresh-only de Terraform 1.16.6

`terraform_1_16_6_refresh_only.json` es salida real de `terraform show -json`
capturada con Terraform 1.16.6 oficial (checksum SHA-256 del ZIP comprobado
contra el archivo oficial de la misma versión). Usa exclusivamente un módulo
local y `terraform_data`, integrado en Terraform. Todos los valores son
sintéticos; no proviene del state del incidente. No requiere descargar providers,
credenciales, Wasabi, Proxmox ni una base de datos.

Las regresiones reutilizan su estructura y sustituyen únicamente la lista de
recursos del módulo por atributos KVM sintéticos. El campo planned_values
permanece tal como lo produjo Terraform. No representa una captura de bpg/proxmox.

Reproducción opcional con un binario 1.16.6 previamente verificado. Ejecútese desde
la raíz del repositorio; solo crea archivos en un directorio temporal y no
ejecuta Apply, destroy, import ni escrituras de state mediante Terraform:

```sh
IACP_TF_CLI=/ruta/absoluta/a/terraform-1.16.6 python - <<'PY'
import json
import os
from pathlib import Path
import subprocess
import tempfile

binary = os.environ["IACP_TF_CLI"]
with tempfile.TemporaryDirectory(prefix="iacp-refresh-only-") as directory:
    root = Path(directory)
    (root / "kvm").mkdir()
    (root / "main.tf").write_text('module "kvm" { source = "./kvm" }\n')
    (root / "kvm/main.tf").write_text('resource "terraform_data" "this" { input = "synthetic-fixture" }\n')
    attributes = {"id": "11111111-1111-4111-8111-111111111111",
                  "input": {"value": "synthetic-fixture", "type": "string"},
                  "output": {"value": "synthetic-fixture", "type": "string"}, "triggers_replace": None}
    state = {"version": 4, "terraform_version": "1.16.6", "serial": 3,
             "lineage": "d954af88-6ae6-2d04-9ead-55ed1f95d644", "outputs": {},
             "resources": [{"module": "module.kvm", "mode": "managed", "type": "terraform_data",
                            "name": "this", "provider": 'provider["terraform.io/builtin/terraform"]',
                            "instances": [{"schema_version": 0, "attributes": attributes,
                                           "sensitive_attributes": []}]}]}
    path = root / "terraform.tfstate"
    path.write_text(json.dumps(state))
    before = path.read_bytes()
    env = {"PATH": os.environ["PATH"], "HOME": directory, "TF_INPUT": "0",
           "TF_IN_AUTOMATION": "1", "CHECKPOINT_DISABLE": "1"}
    def run(*args):
        return subprocess.run([binary, *args], cwd=root, env=env, capture_output=True, check=True).stdout
    assert json.loads(run("version", "-json"))["terraform_version"] == "1.16.6"
    run("init", "-backend=false", "-input=false", "-no-color")
    run("plan", "-refresh-only", "-input=false", "-no-color", "-lock-timeout=30s", "-out=observation.tfplan")
    observed = json.loads(run("show", "-json", "observation.tfplan"))
    assert observed["planned_values"] == {"root_module": {}}
    resource = observed["prior_state"]["values"]["root_module"]["child_modules"][0]["resources"][0]
    assert resource["address"] == "module.kvm.terraform_data.this"
    assert path.read_bytes() == before
    print("planned_values vacío, recurso en prior_state y state local intacto")
PY
```
