"""Respaldo y prueba de restauración para recuperación ante desastres."""

import json

import pytest

from scripts import go_live

pytestmark = pytest.mark.no_database


def _backup_dir(tmp_path):
    directory = tmp_path / "respaldo"
    directory.mkdir()
    (directory / "postgres.dump").write_bytes(b"PGDMP-ficticio")
    (directory / "workdata.tar.gz").write_bytes(b"tar-ficticio")
    manifest = {
        "format": go_live.BACKUP_FORMAT,
        "database": {"schema_version": 3, "instances": 1, "operations": 1},
        "files": {
            name: {"sha256": go_live._sha256_file(directory / name), "bytes": 1}
            for name in go_live.BACKUP_FILES
        },
    }
    (directory / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return directory


def test_backup_verify_rechaza_respaldo_alterado(tmp_path, monkeypatch):
    directory = _backup_dir(tmp_path)
    (directory / "postgres.dump").write_bytes(b"alterado")
    monkeypatch.setattr(go_live, "_run", lambda *a, **k: pytest.fail("no debe restaurar"))
    with pytest.raises(go_live.GateError, match="SHA-256 de postgres.dump"):
        go_live._backup_verify(directory)


def test_backup_verify_rechaza_formato_desconocido(tmp_path):
    directory = _backup_dir(tmp_path)
    manifest = json.loads((directory / "manifest.json").read_text())
    manifest["format"] = 99
    (directory / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(go_live.GateError, match="Formato"):
        go_live._backup_verify(directory)


def test_stream_to_file_no_deja_archivo_parcial(tmp_path):
    target = tmp_path / "parcial.dump"
    with pytest.raises(go_live.GateError):
        go_live._stream_to_file(["sh", "-c", "printf datos; exit 3"], target, label="prueba")
    assert not target.exists()


def test_stream_to_file_crea_archivo_privado(tmp_path):
    target = tmp_path / "ok.dump"
    go_live._stream_to_file(["sh", "-c", "printf datos"], target, label="prueba")
    assert target.read_bytes() == b"datos"
    assert target.stat().st_mode & 0o777 == 0o600


def test_backup_exige_directorio_fuera_del_checkout(tmp_path):
    with pytest.raises(go_live.GateError, match="fuera del checkout"):
        go_live._backup({"project": "x"}, tmp_path, tmp_path, go_live.ROOT / "respaldo")


def test_backup_y_verify_requieren_backup_dir(tmp_path, capsys):
    with pytest.raises(SystemExit):
        go_live.main(["backup", "--manifest", str(tmp_path / "m.json"), "--evidence-dir", str(tmp_path / "e")])
