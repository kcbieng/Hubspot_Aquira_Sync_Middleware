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
