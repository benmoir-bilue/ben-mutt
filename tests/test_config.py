from __future__ import annotations

import os
import stat
import tomllib

import pytest

import bem.config as config_mod
from bem.config import Config


@pytest.fixture
def tmp_config(tmp_path, monkeypatch):
    monkeypatch.setattr(config_mod, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(config_mod, "CONFIG_FILE", tmp_path / "config.toml")
    return tmp_path / "config.toml"


def test_save_writes_valid_toml(tmp_config, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("EDITOR", raising=False)
    cfg = Config()
    cfg.safe_mode = True
    cfg.threads_per_page = 25
    cfg.editor = "nvim"
    cfg.save()

    with open(tmp_config, "rb") as f:
        data = tomllib.load(f)  # would raise on `True`/invalid TOML
    assert data["safe_mode"] is True
    assert data["threads_per_page"] == 25
    assert data["editor"] == "nvim"


def test_save_load_round_trip(tmp_config, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    cfg = Config()
    cfg.safe_mode = False
    cfg.realname = 'Ben "BM" Moir'
    cfg.save()

    loaded = Config.load()
    assert loaded.safe_mode is False
    assert loaded.realname == 'Ben "BM" Moir'


def test_save_omits_api_key_from_environment(tmp_config, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-secret")
    cfg = Config()  # picks the key up from the env
    cfg.save()
    assert "sk-ant-secret" not in tmp_config.read_text()


def test_save_keeps_api_key_that_came_from_config(tmp_config, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    cfg = Config()
    cfg.anthropic_api_key = "sk-ant-from-file"
    cfg.save()
    assert "sk-ant-from-file" in tmp_config.read_text()


def test_save_sets_owner_only_permissions(tmp_config, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    Config().save()
    mode = stat.S_IMODE(os.stat(tmp_config).st_mode)
    assert mode == 0o600
