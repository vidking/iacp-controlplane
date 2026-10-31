#!/usr/bin/env python3
"""Entrada operativa del Compose técnico, sin dependencias comerciales."""

import argparse
import os
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
COMPOSE_DEFAULT = ROOT / "compose.technical.yaml"


def compose_args(args: argparse.Namespace) -> list[str]:
    command = ["docker", "compose"]
    if args.env_file:
        command.extend(["--env-file", str(args.env_file.resolve())])
    command.extend(["-p", args.project, "-f", str(args.compose_file.resolve())])
    return command


def require_private_env(path: Path) -> None:
    if not path.exists():
        raise RuntimeError(f"No existe el archivo de entorno técnico: {path}")
    if path.is_symlink() or path.stat().st_mode & 0o077:
        raise RuntimeError("El archivo de entorno técnico debe ser regular y tener modo 0600")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=Path("/etc/iacp/technical.env"))
    parser.add_argument("--compose-file", type=Path, default=COMPOSE_DEFAULT)
    parser.add_argument("--project", default=os.getenv("IACP_TECHNICAL_PROJECT", "iacp-controlplane"))
    parser.add_argument(
        "action",
        choices=("config-check", "status", "deploy", "migrate", "logs", "test", "plan", "apply", "destroy"),
    )
    parser.add_argument("service", nargs="?", help="Servicio para logs")
    parser.add_argument("--tail", type=int, default=100)
    args, extra = parser.parse_known_args(argv)

    if args.action == "test":
        return subprocess.run(["bash", str(ROOT / "scripts/test.sh"), *extra], check=False).returncode

    if not args.compose_file.is_file():
        raise RuntimeError(f"No existe el Compose técnico: {args.compose_file}")
    require_private_env(args.env_file)
    base = compose_args(args)

    if args.action == "config-check":
        command = [*base, "config", "--quiet"]
    elif args.action == "status":
        command = [*base, "ps"]
    elif args.action == "deploy":
        subprocess.run([*base, "config", "--quiet"], check=True)
        command = [*base, "up", "--build", "-d", "--wait"]
    elif args.action == "migrate":
        command = [*base, "run", "--rm", "migrate"]
    elif args.action == "logs":
        if not args.service:
            raise RuntimeError("logs requiere el nombre de un servicio")
        command = [*base, "logs", "--tail", str(args.tail), args.service]
    elif args.action in {"plan", "apply", "destroy"}:
        script = ROOT / "scripts" / f"{args.action}.sh"
        return subprocess.run([str(script), *extra], check=False).returncode
    else:
        raise RuntimeError("Acción técnica no soportada")

    return subprocess.run(command, check=False).returncode


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, subprocess.CalledProcessError) as error:
        print(f"iacpctl técnico detenido: {error}", file=sys.stderr)
        raise SystemExit(1)
