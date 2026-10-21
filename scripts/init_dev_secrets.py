"""Inicializa exclusivamente el volumen de secretos del entorno de desarrollo."""
import argparse
import os
from pathlib import Path
import secrets


def initialize(directory: Path, uid: int = 10001, gid: int = 10001) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    directory.chmod(0o700)
    os.chown(directory, uid, gid)
    for name in ("db_password", "api_token"):
        path = directory / name
        try:
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o400)
            with os.fdopen(descriptor, "w", encoding="utf8") as output:
                output.write(secrets.token_hex(32) + "\n")
        except FileExistsError:
            if path.is_symlink() or len(path.read_text(encoding="utf8").strip()) < 32:
                raise RuntimeError("Secreto de desarrollo existente inválido; no se reemplazó") from None
        path.chmod(0o400)
        os.chown(path, uid, gid)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    initialize(parser.parse_args().directory)
    print("Secretos de desarrollo preparados; no se reemplazaron los existentes.")
