from app.runtime import apply_db_overlay, looks_encrypted


class FakeRepo:
    def __init__(self, rows):
        self._rows = rows

    def all_settings(self):
        return self._rows


def test_looks_encrypted_detects_fernet_prefix():
    assert looks_encrypted("gAAAAABabcdef")
    assert not looks_encrypted("actual-password")


def test_overlay_skips_undecryptable_secret(monkeypatch):
    from app.settings import get_settings
    import app.db.repo as repo_mod

    settings = get_settings()
    settings.aquira_password = "env-password"
    monkeypatch.setattr(repo_mod, "Repo", lambda: FakeRepo({"aquira_password": "gAAAAABnotvalidciphertext"}))
    apply_db_overlay()
    assert get_settings().aquira_password == "env-password"


def test_overlay_does_not_override_process_role(monkeypatch):
    from app.settings import get_settings
    import app.db.repo as repo_mod

    get_settings.cache_clear()
    monkeypatch.setenv("HUBQUIRA_ROLE", "web")
    get_settings.cache_clear()
    monkeypatch.setattr(repo_mod, "Repo", lambda: FakeRepo({"hubquira_role": "all", "whatif": "false"}))
    apply_db_overlay()
    assert get_settings().hubquira_role == "web"
    get_settings.cache_clear()
