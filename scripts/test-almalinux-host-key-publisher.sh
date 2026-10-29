#!/usr/bin/env bash
set -euo pipefail

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
PUBLISHER="$ROOT/deploy/mng/almalinux/iacp-publish-host-key"

for image in almalinux:9 almalinux:10; do
    docker run --rm -i -v "$PUBLISHER:/opt/iacp-publish-host-key:ro" "$image" bash -s -- "$image" <<'CONTAINER'
set -eu
image=$1
dnf -y -q install openssh-server openssh-clients diffutils >/dev/null
ssh-keygen -A >/dev/null
mkdir -p /run/sshd
if [ ! -e /usr/sbin/restorecon ] && [ ! -e /usr/bin/restorecon ]; then
    ln -s /usr/bin/true /usr/bin/restorecon
fi
install -m 0755 /opt/iacp-publish-host-key /usr/local/sbin/iacp-publish-host-key
/usr/local/sbin/iacp-publish-host-key

target=/var/lib/iacp/host-identity/ssh_host_ed25519_key.pub
test "$(stat -c '%U:%G:%a' "$target")" = 'root:root:444'
cmp -s /etc/ssh/ssh_host_ed25519_key.pub "$target"
printf 'PASS %s publisher\n' "$image"

/usr/sbin/sshd -D -e -p 2222 -o PidFile=none -o PasswordAuthentication=no \
    -o PermitRootLogin=no -o ListenAddress=127.0.0.1 >/tmp/iacp-sshd.log 2>&1 &
pid=$!
trap 'kill "$pid" 2>/dev/null || true' EXIT
presented=
for attempt in 1 2 3 4 5; do
    presented=$(ssh-keyscan -T 2 -p 2222 -t ed25519 127.0.0.1 2>/dev/null | awk '$1 !~ /^#/ && NF >= 3 {print $3; exit}') || true
    [ -n "$presented" ] && break
    sleep 1
done
[ "$presented" = "$(awk '$1 !~ /^#/ && NF >= 2 {print $2; exit}' "$target")" ]
printf 'PASS %s handshake\n' "$image"
CONTAINER
done
