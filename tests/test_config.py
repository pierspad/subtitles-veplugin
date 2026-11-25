# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for configuration management, precedence rules, and credential masking."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from subtitles_plugin.config import (
    ConfigManager,
    OpenSubtitlesCredentials,
    PluginSettings,
    mask_secret,
    parse_env_file,
)


def test_mask_secret():
    assert mask_secret(None) == "(not configured)"
    assert mask_secret("") == "(not configured)"
    assert mask_secret("short") == "********"
    assert mask_secret("12345678") == "********"
    assert mask_secret("123456789") == "1234...6789"
    assert mask_secret("opensubtitles_consumer_secret_key") == "open..._key"


def test_parse_env_file(tmp_path: Path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        """
        # Leading comment
        OPENSUBTITLES_API_KEY="test_key_from_dotenv"
        export OPENSUBTITLES_USERNAME='sub_user'
        OPENSUBTITLES_PASSWORD=raw_secret_pass
        OPENSUBTITLES_LANGUAGES="en,ja,fr"
        OPENSUBTITLES_DRY_RUN=true
        INVALID_LINE_NO_EQUALS
        # Another comment
        """,
        encoding="utf-8",
    )

    parsed = parse_env_file(env_file)
    assert parsed["OPENSUBTITLES_API_KEY"] == "test_key_from_dotenv"
    assert parsed["OPENSUBTITLES_USERNAME"] == "sub_user"
    assert parsed["OPENSUBTITLES_PASSWORD"] == "raw_secret_pass"
    assert parsed["OPENSUBTITLES_LANGUAGES"] == "en,ja,fr"
    assert parsed["OPENSUBTITLES_DRY_RUN"] == "true"
    assert "INVALID_LINE_NO_EQUALS" not in parsed


def test_config_precedence(tmp_path: Path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        """
        OPENSUBTITLES_API_KEY=key_from_dotenv
        OPENSUBTITLES_USERNAME=user_from_dotenv
        OPENSUBTITLES_PASSWORD=pass_from_dotenv
        OPENSUBTITLES_LANGUAGES=dotenv_lang
        OPENSUBTITLES_DOWNLOAD_DIR=/dotenv/download
        OPENSUBTITLES_DRY_RUN=false
        """,
        encoding="utf-8",
    )

    clean_env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith("OPENSUBTITLES_")
    }
    with patch.dict(os.environ, clean_env, clear=True):
        mgr = ConfigManager(repo_root=tmp_path)
        creds = mgr.resolve_credentials()
        assert creds.api_key == "key_from_dotenv"
        assert creds.username == "user_from_dotenv"
        assert creds.password == "pass_from_dotenv"
        assert creds.source == "dotenv"
        assert mgr.resolve_preferred_languages() == ["dotenv_lang"]

        os.environ["OPENSUBTITLES_API_KEY"] = "key_from_environ"
        os.environ["OPENSUBTITLES_USERNAME"] = "user_from_environ"
        mgr2 = ConfigManager(repo_root=tmp_path)
        creds2 = mgr2.resolve_credentials()
        assert creds2.api_key == "key_from_environ"
        assert creds2.username == "user_from_environ"
        assert creds2.password == "pass_from_dotenv"
        assert creds2.source == "environment"

        mgr2.update_settings(
            {
                "opensubtitles_api_key": "key_from_settings",
                "preferred_languages": "it,es",
            },
            save=False,
        )
        creds3 = mgr2.resolve_credentials()
        assert creds3.api_key == "key_from_settings"
        assert creds3.username == "user_from_environ"
        assert creds3.source == "settings"
        assert mgr2.resolve_preferred_languages() == ["it", "es"]


def test_settings_persistence(tmp_path: Path):
    data_dir = tmp_path / "data"
    mgr = ConfigManager(repo_root=tmp_path, data_dir=data_dir)
    mgr.update_settings(
        {
            "opensubtitles_api_key": "persisted_key_12345",
            "preferred_languages": "ja,en",
            "dry_run_default": True,
        },
        save=True,
    )

    settings_file = data_dir / "settings.json"
    assert settings_file.is_file()

    mgr_reloaded = ConfigManager(repo_root=tmp_path, data_dir=data_dir)
    assert mgr_reloaded.settings.opensubtitles_api_key == "persisted_key_12345"
    assert mgr_reloaded.settings.preferred_languages == "ja,en"
    assert mgr_reloaded.settings.dry_run_default is True
    assert mgr_reloaded.resolve_dry_run() is True


def test_resolve_download_dir(tmp_path: Path):
    clean_env = {
        k: v
        for k, v in os.environ.items()
        if k not in ("OPENSUBTITLES_DOWNLOAD_DIR", "VESTA_DOWNLOADS_DIR")
    }
    with patch.dict(os.environ, clean_env, clear=True):
        data_dir = tmp_path / "data"
        mgr = ConfigManager(repo_root=tmp_path, data_dir=data_dir)

        # create=False must not eagerly mkdir unused fallback directories
        non_created = mgr.resolve_download_dir(create=False)
        assert non_created == data_dir / "downloads"
        assert not non_created.exists()

        resolved = mgr.resolve_download_dir()
        assert resolved == data_dir / "downloads"
        assert resolved.is_dir()

        # VESTA_DOWNLOADS_DIR takes precedence over data_dir/downloads
        vesta_env_dl = tmp_path / "os_downloads_env"
        os.environ["VESTA_DOWNLOADS_DIR"] = str(vesta_env_dl)
        assert mgr.resolve_download_dir(create=False) == vesta_env_dl.resolve()
        assert not vesta_env_dl.exists()

        # Explicit permissions.downloadsDir takes precedence over VESTA_DOWNLOADS_DIR
        perm_dl = tmp_path / "os_downloads_perm"
        mgr.set_downloads_dir(perm_dl)
        assert mgr.resolve_download_dir(create=False) == perm_dl.resolve()

        # OPENSUBTITLES_DOWNLOAD_DIR env overrides Vesta OS downloadsDir
        env_override = tmp_path / "env_override_dl"
        os.environ["OPENSUBTITLES_DOWNLOAD_DIR"] = str(env_override)
        assert mgr.resolve_download_dir(create=False) == env_override.resolve()

        # default_download_dir setting overrides everything
        custom_dir = tmp_path / "my_custom_downloads"
        mgr.update_settings({"default_download_dir": str(custom_dir)}, save=False)
        resolved_custom = mgr.resolve_download_dir()
        assert resolved_custom == custom_dir
        assert resolved_custom.is_dir()


class TestConfigSuite(unittest.TestCase):
    def test_all(self):
        test_mask_secret()
        with tempfile.TemporaryDirectory() as td:
            test_parse_env_file(Path(td))
        with tempfile.TemporaryDirectory() as td:
            test_config_precedence(Path(td))
        with tempfile.TemporaryDirectory() as td:
            test_settings_persistence(Path(td))
        with tempfile.TemporaryDirectory() as td:
            test_resolve_download_dir(Path(td))
