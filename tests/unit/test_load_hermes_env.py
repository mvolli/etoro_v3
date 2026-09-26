"""load_hermes_env: zentrale .env-Ladung der Cron-Worker (refactor/dedupe-load-env)."""
import logging

import bot.config as config


def test_sets_missing_vars_verbatim_and_keeps_existing(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text(
        "# comment\n"
        "\n"
        "NEW_KEY = abc \n"
        "QUOTED=\"keep-quotes\"\n"
        "EXISTING=from-file\n"
        "no_equals_line\n"
    )
    monkeypatch.setattr(config, "HERMES_ENV_PATH", env)
    monkeypatch.delenv("NEW_KEY", raising=False)
    monkeypatch.delenv("QUOTED", raising=False)
    monkeypatch.setenv("EXISTING", "from-process")

    assert config.load_hermes_env() is True

    import os
    assert os.environ["NEW_KEY"] == "abc"
    assert os.environ["QUOTED"] == '"keep-quotes"'  # Worker-Semantik: Quotes bleiben
    assert os.environ["EXISTING"] == "from-process"  # setdefault, nie ueberschreiben


def test_missing_file_returns_false_and_warns(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(config, "HERMES_ENV_PATH", tmp_path / "missing.env")
    assert config.load_hermes_env() is False
    with caplog.at_level(logging.WARNING):
        assert config.load_hermes_env(logging.getLogger("t")) is False
    assert ".env not found" in caplog.text
