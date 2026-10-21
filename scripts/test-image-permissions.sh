#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

docker_endpoint="${DOCKER_HOST:-$(docker context inspect --format '{{.Endpoints.docker.Host}}')}"
case "$docker_endpoint" in
  unix://*|npipe://*) ;;
  *) echo 'La prueba de imagen requiere un endpoint Docker local.' >&2; exit 1 ;;
esac

test_root="$(mktemp -d)"
image_tag="iacp-image-permissions:$(date +%s)-$$"
cleanup() {
  docker image rm "$image_tag" >/dev/null 2>&1 || true
  rm -rf -- "$test_root"
}
trap cleanup EXIT
context="$test_root/context"
mkdir -p "$context"

# Solo fuentes versionadas; no copiar secretos o configuración local al contexto.
git ls-files -z -- app/Dockerfile app/.dockerignore app/requirements.txt \
  app/alembic.ini app/iacp app/alembic |
  while IFS= read -r -d '' source; do
    destination="$context/${source#app/}"
    mkdir -p "$(dirname "$destination")"
    cp "$source" "$destination"
  done

# Simular un checkout con umask restrictiva sin modificar el árbol de trabajo.
find "$context" -type d -exec chmod 0700 {} +
find "$context" -type f -exec chmod 0600 {} +
target="${IACP_IMAGE_TARGET:-api}"
case "$target" in api|terraform-worker) ;; *) echo 'Target de imagen inválido.' >&2; exit 1 ;; esac
docker build --progress plain --target "$target" --tag "$image_tag" "$context"

# Ambos comandos usan el USER predeterminado, sin secretos, montajes ni red.
docker run --rm --network none "$image_tag" alembic -c /app/alembic.ini heads
docker run --rm --network none -i "$image_tag" python - <<'PY'
import os
import stat
from pathlib import Path

assert os.getuid() == 10001, f"UID inesperado: {os.getuid()}"
assert os.getgid() == 10001, f"GID inesperado: {os.getgid()}"


def check_path(path):
    metadata = path.stat()
    expected_mode = 0o755 if path.is_dir() else 0o644
    assert (metadata.st_uid, metadata.st_gid) == (0, 0), path
    assert stat.S_IMODE(metadata.st_mode) == expected_mode, path
    access = os.R_OK | (os.X_OK if path.is_dir() else 0)
    assert os.access(path, access), f"No puedo leer/recorrer {path}"
    assert not os.access(path, os.W_OK), f"El código permite escritura: {path}"


root = Path("/app")
check_path(root)


def fail_walk(error):
    raise error


for directory, subdirectories, filenames in os.walk(root, onerror=fail_walk):
    for name in subdirectories + filenames:
        check_path(Path(directory) / name)

from fastapi import FastAPI
from iacp.main import app
from iacp.worker import celery_app, smoke
from iacp.terraform.worker import plan_instance, apply_instance

assert isinstance(app, FastAPI)
assert smoke.name == "iacp.smoke"
assert callable(celery_app.tasks[smoke.name].run)
assert callable(celery_app.tasks[plan_instance.name].run)
assert callable(celery_app.tasks[apply_instance.name].run)
print("UID/GID 10001; fuentes root:root legibles sin escritura; FastAPI y worker importados.")
PY
if [[ "$target" == terraform-worker ]]; then
  docker run --rm --network none "$image_tag" terraform version
  docker run --rm --network none "$image_tag" git --version
fi
echo 'Regresión de permisos de imagen aprobada con archivos 600 y directorios 700 en el contexto.'
