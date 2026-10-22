"""Reproducción local con Terraform 1.16.6; sin provider externo ni servicios."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import zipfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--terraform", required=True, help="Ruta absoluta del binario 1.16.6 verificado")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="iacp-state-consistency-") as directory:
        root = Path(directory)
        (root / "kvm").mkdir()
        names = ("instance", "environment", "allowed_nodes", "node_name", "vm_id", "backend")
        configuration = 'module "kvm" { source = "./kvm" }\n'
        for name in names:
            configuration += ('variable "' + name + '" {\n type = string\n default = "synthetic"\n'
                              ' validation {\n condition = length(var.' + name + ') > 0\n'
                              ' error_message = "Must be nonempty."\n }\n}\n')
        (root / "main.tf").write_text(configuration)
        (root / "kvm/main.tf").write_text('resource "terraform_data" "this" { input = "synthetic" }\n')
        state = {"version": 4, "terraform_version": "1.16.6", "serial": 3,
                 "lineage": "d954af88-6ae6-2d04-9ead-55ed1f95d644", "outputs": {},
                 "resources": [{"module": "module.kvm", "mode": "managed", "type": "terraform_data",
                                "name": "this", "provider": 'provider["terraform.io/builtin/terraform"]',
                                "instances": [{"schema_version": 0, "attributes": {
                                    "id": "11111111-1111-4111-8111-111111111111",
                                    "input": {"value": "synthetic", "type": "string"},
                                    "output": {"value": "synthetic", "type": "string"}, "triggers_replace": None},
                                    "sensitive_attributes": []}]}],
                 "check_results": [{"object_kind": "var", "config_addr": "var." + name, "status": "pass",
                                    "objects": [{"object_addr": "var." + name, "status": "pass"}]} for name in names]}
        path = root / "terraform.tfstate"
        path.write_text(json.dumps(state))
        before = path.read_bytes()
        env = {"PATH": os.environ["PATH"], "HOME": directory, "TF_INPUT": "0",
               "TF_IN_AUTOMATION": "1", "CHECKPOINT_DISABLE": "1"}
        commands = []
        def run(*arguments):
            commands.append(list(arguments))
            return subprocess.run([args.terraform, *arguments], cwd=root, env=env, capture_output=True,
                                  check=True, timeout=60).stdout
        version = json.loads(run("version", "-json"))["terraform_version"]
        assert version == "1.16.6"
        run("init", "-backend=false", "-input=false", "-no-color")
        reads = [json.loads(run("state", "pull")) for _ in range(32)]
        unique = len({json.dumps(item, sort_keys=True) for item in reads})
        assert unique > 1, "No se manifestó la permutación en esta ejecución; repetir"
        normalized = []
        for item in reads:
            assert item["serial"] == state["serial"] and item["lineage"] == state["lineage"]
            item["check_results"].sort(key=lambda check: check["config_addr"])
            normalized.append(json.dumps(item, sort_keys=True))
        assert len(set(normalized)) == 1
        run("plan", "-refresh-only", "-input=false", "-no-color", "-lock-timeout=30s", "-out=observation.tfplan")
        observed = json.loads(run("show", "-json", "observation.tfplan"))
        with zipfile.ZipFile(root / "observation.tfplan") as archive:
            embedded = json.loads(archive.read("tfstate"))
        assert embedded["lineage"] == state["lineage"] and embedded["serial"] == state["serial"]
        assert observed["planned_values"] == {"root_module": {}}
        address = observed["prior_state"]["values"]["root_module"]["child_modules"][0]["resources"][0]["address"]
        assert address == "module.kvm.terraform_data.this"
        assert path.read_bytes() == before
        print(json.dumps({"terraform_version": version, "state_reads": len(reads), "distinct_decoded_states": unique,
                          "distinct_states_after_sorting_checks": len(set(normalized)),
                          "state_bytes_unchanged": True, "state_sha256": hashlib.sha256(before).hexdigest(),
                          "lineage": state["lineage"], "serial": state["serial"],
                          "embedded_plan_state": {"lineage": embedded["lineage"], "serial": embedded["serial"]},
                          "refresh_values_source": "prior_state.values", "commands": commands}, indent=2))


if __name__ == "__main__":
    main()
