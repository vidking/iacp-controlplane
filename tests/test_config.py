import secrets

from iacp.config import database_url


def test_database_url_preserves_reserved_characters_without_displaying_password(tmp_path, monkeypatch):
    password = secrets.token_urlsafe(32) + "@: /+%"
    path = tmp_path / "db_password"
    path.write_text(password + "\n")
    monkeypatch.setenv("DATABASE_PASSWORD_FILE", str(path))
    monkeypatch.setenv("POSTGRES_USER", "test user")
    monkeypatch.setenv("POSTGRES_DB", "test/database")
    monkeypatch.setenv("DATABASE_PORT", "55432")
    url = database_url()
    assert url.password == password
    assert url.username == "test user"
    assert url.database == "test/database"
    assert url.port == 55432
    assert password not in str(url)
    assert "***" in str(url)
