#!/usr/bin/env python3
"""Generar un bootstrap fijo para una copia versionada de la imagen AlmaLinux.

Solo genera archivos revisables; no modifica el sistema ni accede a Proxmox.
"""

import argparse
import ast
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INSTALL = r"""#!/bin/bash
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo 'Se requiere root dentro de la copia de imagen'; exit 1; }
. /etc/os-release
[[ "$ID" == almalinux && "$VERSION_ID" =~ ^(9|10)(\.|$) ]] || { echo 'Solo AlmaLinux 9/10'; exit 1; }
[[ "$(getenforce)" == Enforcing ]] || { echo 'Se requiere SELinux Enforcing'; exit 1; }
getsebool virt_qemu_ga_run_unconfined >/dev/null
command -v semanage >/dev/null || { echo 'Instalar policycoreutils-python-utils antes de continuar'; exit 1; }
bundle_dir=$(cd -- "$(dirname -- "$0")" && pwd)
python3 - "$bundle_dir" <<'VERIFY'
import hashlib,json,sys
from pathlib import Path
root=Path(sys.argv[1]);manifest=json.loads((root/'manifest.json').read_text())
assert hashlib.sha256((root/'iacp-guest-bootstrap').read_bytes()).hexdigest()==manifest['helper_sha256']
VERIFY
install -d -o root -g root -m 0755 /usr/local/libexec
if [[ -f /usr/local/libexec/iacp-guest-bootstrap ]]; then
  cp -p /usr/local/libexec/iacp-guest-bootstrap "/root/iacp-guest-bootstrap.before-$(date -u +%Y%m%dT%H%M%SZ)"
fi
install -o root -g root -m 0755 "$bundle_dir/iacp-guest-bootstrap" /usr/local/libexec/iacp-guest-bootstrap
if ! matchpathcon -n /usr/local/libexec/iacp-guest-bootstrap | grep -q ':virt_qemu_ga_unconfined_exec_t:'; then
  semanage fcontext -a -t virt_qemu_ga_unconfined_exec_t '/usr/local/libexec/iacp-guest-bootstrap'
fi
restorecon -v /usr/local/libexec/iacp-guest-bootstrap
setsebool -P virt_qemu_ga_run_unconfined on
[[ "$(getenforce)" == Enforcing ]]
matchpathcon -V /usr/local/libexec/iacp-guest-bootstrap
printf '%s\n' 'Bootstrap fijo instalado. SELinux sigue Enforcing. Validar en la copia antes de publicar la nueva imagen.'
"""


def prepare(output: Path):
    tree = ast.parse((ROOT / "worker/service_bootstrap.py").read_text())
    program = next(
        ast.literal_eval(node.value)
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "PROGRAM"
            for target in node.targets
        )
    )
    header = """#!/usr/bin/python3 -I
import ipaddress,os,re,sys,uuid
assert os.geteuid()==0 and len(sys.argv)==6
os.environ['PATH']='/usr/sbin:/usr/bin:/sbin:/bin'
assert str(uuid.UUID(sys.argv[1]))==sys.argv[1]
assert re.fullmatch(r'[0-9a-f]{2}(:[0-9a-f]{2}){5}',sys.argv[2])
assert sys.argv[3] in {'dhcp','static'}
if sys.argv[3]=='static':
 ip=ipaddress.ip_interface(sys.argv[4])
 assert ip.network==ipaddress.ip_network('190.0.231.0/24') and sys.argv[5]=='190.0.231.1'
 assert ip.ip not in {ip.network.network_address,ip.network.broadcast_address,ipaddress.ip_address('190.0.231.1')}
else:assert not sys.argv[4] and not sys.argv[5]
"""
    program = program.replace(
        "print(json.dumps({'instance_id':identifier",
        "print(json.dumps({'bootstrap_protocol':1,'instance_id':identifier",
    )
    helper = header + program
    compile(helper, "iacp-guest-bootstrap", "exec")
    output.mkdir(mode=0o700, parents=False, exist_ok=False)
    (output / "iacp-guest-bootstrap").write_text(helper)
    (output / "iacp-guest-bootstrap").chmod(0o755)
    (output / "install.sh").write_text(INSTALL)
    (output / "install.sh").chmod(0o700)
    (output / "manifest.json").write_text(
        json.dumps(
            {
                "bootstrap_protocol": 1,
                "helper_sha256": hashlib.sha256(helper.encode()).hexdigest(),
            },
            indent=2,
        )
        + "\n"
    )
    return output


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    print(prepare(parser.parse_args().output))
