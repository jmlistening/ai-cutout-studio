# coding: utf-8

from __future__ import annotations

import config


def _clean_env(monkeypatch, path):
    monkeypatch.setattr(config, "ENV_PATH", path)
    monkeypatch.delenv(config.API_KEY_ENV, raising=False)
    monkeypatch.delenv(config.ENDPOINT_ENV, raising=False)


def test_env_with_bom_is_parsed(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_bytes(b"\xef\xbb\xbfMEDIAKIT_API_KEY=mk-test-123456\n")
    _clean_env(monkeypatch, env)

    settings = config.Settings()
    assert settings.api_key == "mk-test-123456"
    assert settings.configured is True
    assert settings.masked_key.startswith("mk-tes")
    assert "123456" not in settings.masked_key


def test_env_encoded_in_gbk_is_readable(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_bytes(
        "# 中文注释，记事本可能存成 ANSI\n"
        "MEDIAKIT_API_KEY=mk-gbk-123456\n".encode("gbk")
    )
    _clean_env(monkeypatch, env)

    settings = config.Settings()
    assert settings.api_key == "mk-gbk-123456"


def test_save_api_key(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    _clean_env(monkeypatch, env)

    settings = config.Settings()
    assert settings.configured is False
    settings.save_api_key("mk-saved-123456")
    assert settings.configured is True
    assert settings.api_key == "mk-saved-123456"
    assert settings.masked_key.startswith("mk-sav")
    assert "MEDIAKIT_API_KEY=mk-saved-123456" in env.read_text(encoding="utf-8")


def test_env_variable_overrides_file(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("MEDIAKIT_API_KEY=mk-file-123456\n", encoding="utf-8")
    _clean_env(monkeypatch, env)
    monkeypatch.setenv(config.API_KEY_ENV, "mk-env-123456")

    settings = config.Settings()
    assert settings.api_key == "mk-env-123456"


def test_invalid_api_key_rejected(tmp_path, monkeypatch):
    _clean_env(monkeypatch, tmp_path / ".env")
    settings = config.Settings()
    try:
        settings.save_api_key("short")
    except ValueError:
        pass
    else:
        raise AssertionError("应拒绝过短的 API Key")


def test_bearer_prefix_is_cleaned(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    _clean_env(monkeypatch, env)
    settings = config.Settings()
    settings.save_api_key("Bearer mk-bearer-123456")
    assert settings.api_key == "mk-bearer-123456"
    assert "MEDIAKIT_API_KEY=mk-bearer-123456" in env.read_text(encoding="utf-8")
