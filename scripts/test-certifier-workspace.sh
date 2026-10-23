#!/usr/bin/env bash
set -euo pipefail
: "${IACP_TEST_IMAGE:?Use the locally built disposable validation image}"
docker --host unix:///var/run/docker.sock run --rm --pull never --network none \
  --read-only --user 10001:10001 --cap-drop ALL --security-opt no-new-privileges \
  --tmpfs /tmp:rw,noexec,nosuid,nodev,mode=1777 \
  --tmpfs /run/iacp-certification:rw,exec,nosuid,nodev,size=536870912,mode=0700,uid=10001,gid=10001 \
  --entrypoint python "$IACP_TEST_IMAGE" -c '
import os,shutil,subprocess
for root in ("/tmp", "/run/iacp-certification"):
    target=root+"/provider-exec-regression"
    shutil.copyfile("/bin/true",target);os.chmod(target,0o700)
    try:
        result=subprocess.run([target],check=True)
        assert root=="/run/iacp-certification"
    except PermissionError:
        assert root=="/tmp"
print("PASS: noexec reproduced; isolated certifier exec workspace works as UID 10001")
'
