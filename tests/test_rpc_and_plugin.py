# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for Subtitles plugin controller, declarative UI, patches, and JSON-RPC 2.0 stdio server."""

import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from subtitles_plugin.config import ConfigManager, OpenSubtitlesCredentials
from subtitles_plugin.extractor import ExtractionResult, LocalSubExtractor, SubtitleTrackInfo
from subtitles_plugin.opensubtitles import OpenSubtitlesClient, SubtitleFile, SubtitleItem
from subtitles_plugin.plugin import SubtitlesPlugin
from subtitles_plugin.rpc import JsonRpcServer
from subtitles_plugin.schema import (
    apply_patches,
    button,
    card,
    column,
    external_link,
    find_node,
    footer,
    heading,
    image,
    language_select,
    make_patch,
    pagination,
    password_input,
    path_input,
    row,
    segmented,
    text,
    text_input,
)


class FakeOpenSubtitlesClient(OpenSubtitlesClient):
    """Mock client for offline testing."""

    def __init__(self):
        super().__init__(OpenSubtitlesCredentials("fake_key", None, None, "test"))
        self.items = [
            SubtitleItem(
                id="sub_test_1",
                subtitle_id="101",
                language="en",
                download_count=4200,
                new_download_count=10,
                hearing_impaired=False,
                hd=True,
                fps=23.976,
                ratings=8.8,
                votes=50,
                from_trusted=True,
                foreign_parts_only=False,
                ai_translated=False,
                machine_translated=False,
                release="The.Matrix.1999.1080p.BluRay",
                comments="Human verified",
                movie_name="The Matrix",
                files=[
                    SubtitleFile(file_id=8801, cd_number=1, file_name="The.Matrix.1999.srt")
                ],
            )
        ]

    def search(self, **kwargs) -> list[SubtitleItem]:
        query = kwargs.get("query", "").lower()
        if "matrix" in query or kwargs.get("tmdb_id") or kwargs.get("imdb_id"):
            return self.items
        return []

    def download_subtitle(self, file_id: int, destination_dir: str | Path, custom_filename: str | None = None) -> Path:
        target = Path(destination_dir) / (custom_filename or "The.Matrix.1999.srt")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("1\n00:00:01,000 --> 00:00:05,000\nWelcome to the real world.\n", encoding="utf-8")
        return target

    def test_connection(self) -> tuple[bool, str]:
        return True, "Mock connected to OpenSubtitles successfully"


class FakeLocalSubExtractor(LocalSubExtractor):
    """Mock extractor for offline testing."""

    def __init__(self):
        super().__init__(dry_run=True)

    def check_binaries(self) -> dict[str, bool]:
        return {"ffmpeg": True, "ffprobe": True}

    def inspect_video_tracks(self, video_path: str | Path) -> list[SubtitleTrackInfo]:
        return [
            SubtitleTrackInfo(
                index=2,
                subtitle_index=0,
                codec_name="subrip",
                language="eng",
                title="English Dialogue",
            ),
            SubtitleTrackInfo(
                index=3,
                subtitle_index=1,
                codec_name="ass",
                language="jpn",
                title="Japanese Commentary",
            ),
        ]

    def extract_track(self, video_path: str | Path, stream_index: int, **kwargs) -> ExtractionResult:
        out_path = Path(kwargs.get("output_dir", "/tmp")) / f"video.track_{stream_index}.srt"
        return ExtractionResult(
            success=True,
            command=["ffmpeg", "-y", "-i", str(video_path), "-map", f"0:{stream_index}", "-c:s", "srt", str(out_path)],
            output_path=str(out_path),
            dry_run=True,
            message=f"Subtitle track {stream_index} extracted successfully to {out_path.name}.",
        )


def test_plugin_full_flow(tmp_path: Path):
    data_dir = tmp_path / "data"
    mgr = ConfigManager(repo_root=tmp_path, data_dir=data_dir)
    client = FakeOpenSubtitlesClient()
    extractor = FakeLocalSubExtractor()

    notifications: list[tuple[str, dict[str, Any]]] = []

    def record_notify(method: str, params: dict[str, Any]):
        notifications.append((method, params))

    plugin = SubtitlesPlugin(
        config_manager=mgr,
        opensubtitles_client=client,
        extractor=extractor,
        notifier=record_notify,
    )

    # 1. Initialize
    init_res = plugin.initialize(
        api_version=1,
        data_dir=str(data_dir),
        locale="en",
        settings={"preferred_languages": "en,fr"},
    )
    assert init_res["status"] == "ok"
    assert init_res["ready"] is True
    assert init_res["root"]["type"] == "column"
    assert mgr.settings.preferred_languages == "en,fr"

    # 2. Search for Matrix
    search_res = plugin.handle_event(
        screen="search",
        widget_id="btn_do_search",
        event_type="click",
        values={"search_query": "Matrix", "search_languages": "en"},
    )
    assert "root" in search_res
    assert len(plugin.search_results) == 1
    assert plugin.search_results[0].release == "The.Matrix.1999.1080p.BluRay"

    # 3. Select subtitle item
    select_res = plugin.handle_event(
        screen="search",
        widget_id="action_select_sub_test_1",
        event_type="click",
        values={},
    )
    assert plugin.selected_subtitle is not None
    assert plugin.selected_subtitle.id == "sub_test_1"

    # 4. Download subtitle
    dl_res = plugin.handle_event(
        screen="search",
        widget_id="btn_download_now",
        event_type="click",
        values={
            "download_dest_dir": str(tmp_path / "subs"),
            "download_custom_name": "Matrix.srt",
        },
    )
    assert plugin.downloaded_subtitle_path is not None
    assert "Matrix.srt" in plugin.downloaded_subtitle_path

    # 5. Emit Flashcards handoff
    fc_res = plugin.handle_event(
        screen="search",
        widget_id="btn_use_in_flashcards",
        event_type="click",
        values={"video_for_flashcards": "/media/matrix.mkv"},
    )
    assert "patch" in fc_res
    # Verify notification was sent
    flashcard_notifs = [n for n in notifications if n[0] == "vesta.openInFlashcards"]
    assert len(flashcard_notifs) == 1
    assert flashcard_notifs[0][1]["videoPath"] == "/media/matrix.mkv"
    assert "Matrix.srt" in flashcard_notifs[0][1]["subtitlePath"]


def test_plugin_local_extraction_flow(tmp_path: Path):
    data_dir = tmp_path / "data"
    mgr = ConfigManager(repo_root=tmp_path, data_dir=data_dir)
    client = FakeOpenSubtitlesClient()
    extractor = FakeLocalSubExtractor()

    notifications: list[tuple[str, dict[str, Any]]] = []

    def record_notify(method: str, params: dict[str, Any]):
        notifications.append((method, params))

    plugin = SubtitlesPlugin(
        config_manager=mgr,
        opensubtitles_client=client,
        extractor=extractor,
        notifier=record_notify,
    )

    # 1. Switch to local screen
    nav_res = plugin.handle_event(
        screen="search",
        widget_id="btn_nav_local",
        event_type="click",
        values={},
    )
    assert plugin.current_screen == "local"

    # 2. Inspect embedded tracks
    inspect_res = plugin.handle_event(
        screen="local",
        widget_id="btn_inspect_tracks",
        event_type="click",
        values={"local_video_path": "/media/anime.mkv"},
    )
    assert len(plugin.local_tracks) == 2
    assert plugin.local_tracks[0].codec_name == "subrip"
    assert plugin.local_tracks[1].codec_name == "ass"

    # 3. Extract track #2
    extract_res = plugin.handle_event(
        screen="local",
        widget_id="action_extract_2",
        event_type="click",
        values={},
    )
    assert plugin.extracted_subtitle_path is not None
    assert "track_2.srt" in plugin.extracted_subtitle_path

    # 4. Use in flashcards from local screen
    fc_res = plugin.handle_event(
        screen="local",
        widget_id="btn_use_in_flashcards_local",
        event_type="click",
        values={"local_video_path": "/media/anime.mkv"},
    )
    assert "patch" in fc_res
    flashcard_notifs = [n for n in notifications if n[0] == "vesta.openInFlashcards"]
    assert len(flashcard_notifs) == 1
    assert flashcard_notifs[0][1]["videoPath"] == "/media/anime.mkv"
    assert "track_2.srt" in flashcard_notifs[0][1]["subtitlePath"]


def test_plugin_settings_save_and_test(tmp_path: Path):
    data_dir = tmp_path / "data"
    mgr = ConfigManager(repo_root=tmp_path, data_dir=data_dir)
    client = FakeOpenSubtitlesClient()
    plugin = SubtitlesPlugin(config_manager=mgr, opensubtitles_client=client)

    rejected = plugin.handle_event("settings", "btn_save_settings", "click", values={"cfg_api_key": "  "})
    assert rejected["snackbar"]["type"] == "warning"
    assert not mgr.settings.opensubtitles_api_key

    # Test settings save
    save_res = plugin.handle_event(
        screen="settings",
        widget_id="btn_save_settings",
        event_type="click",
        values={
            "cfg_api_key": "new_saved_key",
            "cfg_username": "new_user",
            "cfg_preferred_languages": "ja,en",
            "cfg_dry_run_default": True,
        },
    )
    assert mgr.settings.opensubtitles_api_key == "new_saved_key"
    assert mgr.settings.opensubtitles_username == "new_user"
    assert mgr.settings.preferred_languages == "ja,en"
    assert mgr.settings.dry_run_default is True

    # Test connection test
    test_res = plugin.handle_event(
        screen="settings",
        widget_id="btn_test_credentials",
        event_type="click",
        values={"cfg_api_key": "test_key"},
    )
    assert "Mock connected" in plugin.status_message


def test_json_rpc_server_loop(tmp_path: Path):
    in_stream = io.StringIO(
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"apiVersion": 1}}) + "\n"
        + json.dumps({"jsonrpc": "2.0", "id": 2, "method": "render", "params": {"screen": "search"}}) + "\n"
        + json.dumps({"jsonrpc": "2.0", "id": 3, "method": "shutdown", "params": {}}) + "\n"
    )
    out_stream = io.StringIO()

    mgr = ConfigManager(repo_root=tmp_path)
    client = FakeOpenSubtitlesClient()
    plugin = SubtitlesPlugin(config_manager=mgr, opensubtitles_client=client)

    server = JsonRpcServer(plugin=plugin, input_stream=in_stream, output_stream=out_stream)
    server.run()

    lines = [json.loads(line) for line in out_stream.getvalue().strip().splitlines() if line]
    assert len(lines) == 3

    # Resp 1: initialize
    assert lines[0]["id"] == 1
    assert lines[0]["result"]["status"] == "ok"
    assert lines[0]["result"]["ready"] is True

    # Resp 2: render
    assert lines[1]["id"] == 2
    assert "root" in lines[1]["result"]

    # Resp 3: shutdown
    assert lines[2]["id"] == 3
    assert lines[2]["result"]["status"] == "ok"


def test_schema_single_object_patch():
    initial_tree = column(
        [
            heading("Title", level=1, id="title_node"),
            text("Initial text", id="desc_node", variant="normal"),
            row(
                [
                    text_input("inp_1", label="First"),
                    text_input("inp_2", label="Second"),
                ],
                id="row_1",
            ),
        ],
        id="main_root",
    )

    patch = make_patch(
        set_props={
            "desc_node": {"text": "Updated text", "variant": "success"},
            "inp_1": {"disabled": True},
        },
        visibility={
            "title_node": False,
        },
    )

    updated_tree = apply_patches(initial_tree, patch)

    # Check updated props
    desc, _, _ = find_node(updated_tree, "desc_node")
    assert desc is not None
    assert desc["text"] == "Updated text"
    assert desc["variant"] == "success"

    # Check visibility
    title, _, _ = find_node(updated_tree, "title_node")
    assert title is not None
    assert title["hidden"] is True

    # Check input disabled prop
    inp1, _, _ = find_node(updated_tree, "inp_1")
    assert inp1 is not None
    assert inp1["disabled"] is True


def test_ui_layout_and_expert_widgets(tmp_path: Path):
    mgr = ConfigManager(repo_root=tmp_path)
    client = FakeOpenSubtitlesClient()
    plugin = SubtitlesPlugin(config_manager=mgr, opensubtitles_client=client)

    # 1. Search screen layout: single active search mode + content type selector
    search_root = plugin.render_screen("search")
    q_node, _, _ = find_node(search_root, "search_query")
    assert q_node is not None
    assert q_node["flex"] == 3
    assert q_node["icon"] == "search"
    assert q_node["label"] == "Title"

    lang_node, _, _ = find_node(search_root, "search_languages")
    assert lang_node is not None
    assert lang_node["type"] == "languageSelect"
    assert lang_node["flex"] == 1

    mode_node, _, _ = find_node(search_root, "search_mode")
    assert mode_node is not None
    assert mode_node["type"] == "segmented"

    media_node, _, _ = find_node(search_root, "search_media_type")
    assert media_node is not None
    assert media_node["type"] == "segmented"

    # Only Title input is shown in default 'title' mode
    assert find_node(search_root, "search_tmdb_id")[0] is None
    assert find_node(search_root, "search_imdb_id")[0] is None
    assert find_node(search_root, "search_season")[0] is None

    # Switching to TMDB ID renders ONLY search_tmdb_id
    tmdb_screen = plugin.handle_event("search", "search_mode", "change", {"search_mode": "tmdb"})["root"]
    tmdb_node, _, _ = find_node(tmdb_screen, "search_tmdb_id")
    assert tmdb_node is not None
    assert tmdb_node["icon"] == "tmdb"
    assert find_node(tmdb_screen, "search_query")[0] is None
    assert find_node(tmdb_screen, "search_imdb_id")[0] is None
    assert find_node(tmdb_screen, "btn_do_search")[0]["requiresAny"] == ["search_tmdb_id"]

    # Switching to IMDb ID renders ONLY search_imdb_id
    imdb_screen = plugin.handle_event("search", "search_mode", "change", {"search_mode": "imdb"})["root"]
    imdb_node, _, _ = find_node(imdb_screen, "search_imdb_id")
    assert imdb_node is not None
    assert imdb_node["icon"] == "imdb"
    assert find_node(imdb_screen, "search_query")[0] is None
    assert find_node(imdb_screen, "search_tmdb_id")[0] is None
    assert find_node(imdb_screen, "btn_do_search")[0]["requiresAny"] == ["search_imdb_id"]

    # Switching Content Type to TV Series reveals Season & Episode inputs
    tv_screen = plugin.handle_event("search", "search_media_type", "change", {"search_media_type": "tvshow"})["root"]
    assert find_node(tv_screen, "search_season")[0] is not None
    assert find_node(tv_screen, "search_episode")[0] is not None

    # Navigation bar buttons have monochrome icons (Local Video File is inside Settings)
    nav_search, _, _ = find_node(search_root, "btn_nav_search")
    assert nav_search is None

    nav_local, _, _ = find_node(search_root, "btn_nav_local")
    assert nav_local is None

    nav_settings, _, _ = find_node(search_root, "btn_nav_settings")
    assert nav_settings is not None
    assert nav_settings["icon"] == "settings"
    assert nav_settings["placement"] == "header"
    assert "⚙" not in nav_settings["label"]

    # 2. Local video extraction is embedded in Settings with expertOnly
    settings_root = plugin.render_screen("settings")
    local_vid_node, _, _ = find_node(settings_root, "local_video_path")
    assert local_vid_node is not None
    assert local_vid_node.get("expertOnly") is True
    api_key_node, _, _ = find_node(settings_root, "cfg_api_key")
    assert api_key_node is not None
    assert api_key_node["link"]["url"] == "https://www.opensubtitles.com/en/consumers"

    # Preferred language is languageSelect
    pref_node, _, _ = find_node(settings_root, "cfg_preferred_languages")
    assert pref_node is not None
    assert pref_node["type"] == "languageSelect"

    # Download dir is pathInput
    dl_node, _, _ = find_node(settings_root, "cfg_default_download_dir")
    assert dl_node is not None
    assert dl_node["type"] == "pathInput"
    assert dl_node["pickerType"] == "folder"

    # Expert-only fields
    for expert_id in ("cfg_username", "cfg_password", "cfg_dry_run_default", "cfg_ffmpeg_path", "cfg_ffprobe_path"):
        f_node, _, _ = find_node(settings_root, expert_id)
        assert f_node is not None, f"Node {expert_id} missing"
        assert f_node.get("expertOnly") is True, f"Node {expert_id} not expertOnly"



def test_search_guidance_and_local_key(tmp_path: Path):
    clean_env = {k: v for k, v in os.environ.items() if k != "OPENSUBTITLES_API_KEY"}
    with patch.dict(os.environ, clean_env, clear=True):
        mgr = ConfigManager(repo_root=tmp_path)
        plugin = SubtitlesPlugin(config_manager=mgr)
        plugin.initialize(data_dir=str(tmp_path / "data"), locale="it-IT")
        root = plugin.render_screen("search")
        search, _, _ = find_node(root, "btn_do_search")
        settings, _, _ = find_node(root, "btn_nav_settings")
        mode_ctrl, _, _ = find_node(root, "search_mode")
        assert search["disabled"] is True
        assert search["disabledFocus"] == "btn_nav_settings"
        assert search["requiresAny"] == ["search_query"]
        assert settings["status"] == "attention"
        assert mode_ctrl is not None
        assert find_node(root, "api_warning_box")[0] is None

        settings_root = plugin.render_screen("settings")
        guidance, _, _ = find_node(settings_root, "opensubtitles_setup_guidance")
        field, _, _ = find_node(settings_root, "cfg_api_key")
        assert guidance is None
        assert field["emptyWarning"]

        mgr.update_settings({"opensubtitles_api_key": "saved-key"})
        restored = SubtitlesPlugin(config_manager=ConfigManager(repo_root=tmp_path))
        restored.initialize(data_dir=str(tmp_path / "data"))
        ready = restored.render_screen("search")
        assert find_node(ready, "btn_do_search")[0]["disabled"] is False
        assert find_node(ready, "btn_nav_settings")[0].get("status") is None

        assert find_node(restored.render_screen("settings"), "opensubtitles_setup_guidance")[0] is None


def test_remove_active_key_clears_form_and_disk(tmp_path: Path):
    clean_env = {k: v for k, v in os.environ.items() if k != "OPENSUBTITLES_API_KEY"}
    with patch.dict(os.environ, clean_env, clear=True):
        data = tmp_path / "data"
        manager = ConfigManager(repo_root=tmp_path, data_dir=data)
        plugin = SubtitlesPlugin(config_manager=manager)
        manager.update_settings({"opensubtitles_api_key": "old-key"})

        result = plugin.handle_event("settings", "action_delete_key_0", "click", {"cfg_api_key": "old-key"})
        assert result["patch"]["values"] == {"cfg_api_key": ""}
        assert find_node(result["root"], "saved_keys_list")[0] is None

        reloaded = ConfigManager(repo_root=tmp_path, data_dir=data)
        assert not reloaded.resolve_credentials().api_key
        assert reloaded.settings.saved_api_keys == []

        manager.settings.opensubtitles_api_key = "other-key"
        plugin.handle_event("settings", "btn_test_credentials", "click", {"cfg_api_key": ""})
        assert plugin.status_variant == "danger"
        assert "not configured" in plugin.status_message


def test_vesta_cli_inspect_and_sync_workflow(tmp_path: Path):
    data_dir = tmp_path / "data"
    mgr = ConfigManager(repo_root=tmp_path, data_dir=data_dir)
    client = FakeOpenSubtitlesClient()

    cli_calls: list[tuple[str, dict[str, Any]]] = []

    def fake_requester(method: str, params: dict[str, Any]) -> Any:
        cli_calls.append((method, params))
        if method == "vesta.cli" and params.get("command") == "extract":
            return {
                "exitCode": 0,
                "stdout": json.dumps(
                    {
                        "cues": [
                            {"start": "00:00:01,000", "end": "00:00:05,000", "text": "Welcome to the real world."}
                        ]
                    }
                ),
                "stderr": "",
                "success": True,
            }
        if method == "vesta.cli" and params.get("command") == "sync":
            args = params.get("args", [])
            out_idx = args.index("--output") + 1
            out_file = Path(args[out_idx])
            out_file.write_text(
                "1\n00:00:01,500 --> 00:00:05,500\nWelcome to the real world.\n",
                encoding="utf-8",
            )
            return {"exitCode": 0, "stdout": "Synced 1 cues", "stderr": "", "success": True}
        return {}

    plugin = SubtitlesPlugin(
        config_manager=mgr,
        opensubtitles_client=client,
        requester=fake_requester,
    )
    plugin.initialize(api_version=1, data_dir=str(data_dir))
    plugin.handle_event("search", "btn_do_search", "click", {"search_query": "Matrix"})
    plugin.handle_event("search", "action_select_sub_test_1", "click", {})
    plugin.handle_event(
        "search",
        "btn_download_now",
        "click",
        {"download_dest_dir": str(tmp_path / "subs"), "download_custom_name": "Matrix.srt"},
    )
    assert plugin.downloaded_subtitle_path is not None

    # 1. Inspect Cues via vesta.cli
    inspect_res = plugin.handle_event("search", "btn_cli_inspect", "click", {})
    assert any(c[0] == "vesta.cli" and c[1]["command"] == "extract" for c in cli_calls)
    summary_node, _, _ = find_node(inspect_res["root"], "cli_inspection_summary")
    assert summary_node is not None
    assert "1 cue(s)" in summary_node["text"]
    table_node, _, _ = find_node(inspect_res["root"], "cli_cue_preview_table")
    assert table_node is not None
    assert table_node["type"] == "table"
    assert len(table_node["rows"]) == 1
    assert table_node["rows"][0][2] == "Welcome to the real world."

    # 2. Apply Time Shift (+500ms) via vesta.cli sync
    sync_res = plugin.handle_event("search", "btn_cli_sync", "click", {"cli_shift_ms": 500})
    assert any(
        c[0] == "vesta.cli"
        and c[1]["command"] == "sync"
        and "--offset-ms" in c[1]["args"]
        and "500" in c[1]["args"]
        for c in cli_calls
    )
    assert plugin.downloaded_subtitle_path.endswith("Matrix.shifted_+500ms.srt")
    summary_after, _, _ = find_node(sync_res["root"], "cli_inspection_summary")
    assert summary_after is not None
    assert "Matrix.shifted_+500ms.srt" in summary_after["text"]


def test_rpc_send_request_synchronous_response(tmp_path: Path):
    in_stream = io.StringIO(
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1000,
                "result": {"exitCode": 0, "stdout": "{\"ok\":true}", "stderr": "", "success": True},
            }
        )
        + "\n"
    )
    out_stream = io.StringIO()
    mgr = ConfigManager(repo_root=tmp_path)
    plugin = SubtitlesPlugin(config_manager=mgr)
    server = JsonRpcServer(plugin=plugin, input_stream=in_stream, output_stream=out_stream)

    res = plugin.run_cli("extract", ["--input", "/tmp/demo.srt", "--json"])
    assert res["exitCode"] == 0
    assert res["success"] is True
    assert res["json"] == {"ok": True}


def test_plugin_emits_vesta_log_and_detects_tmdb_keys(tmp_path: Path):
    from subtitles_plugin.opensubtitles import looks_like_tmdb_key_or_token

    assert looks_like_tmdb_key_or_token("0123456789abcdef0123456789abcdef") is not None
    assert looks_like_tmdb_key_or_token("eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.sig") is not None
    assert looks_like_tmdb_key_or_token("AbCdEfGhIjKlMnOpQrStUvWxYz123456") is None

    notifications: list[tuple[str, dict[str, Any]]] = []
    mgr = ConfigManager(repo_root=tmp_path, data_dir=tmp_path / "data")
    plugin = SubtitlesPlugin(
        config_manager=mgr,
        opensubtitles_client=FakeOpenSubtitlesClient(),
        notifier=lambda method, params: notifications.append((method, params)),
    )
    plugin.handle_event("search", "btn_do_search", "click", {"search_query": "Matrix"})
    log_events = [n for n in notifications if n[0] == "vesta.log"]
    assert len(log_events) >= 2
    assert any("Searching OpenSubtitles" in e[1]["msg"] for e in log_events)

    save_res = plugin.handle_event(
        "settings",
        "btn_save_settings",
        "click",
        {"cfg_api_key": "0123456789abcdef0123456789abcdef"},
    )
    assert save_res["root"]["snackbar"]["type"] == "warning"
    assert "TMDB" in save_res["root"]["snackbar"]["message"]


def test_schema_new_builders():
    c = card([text("hello")], title="Selected", icon="subtitles", accent="purple", id="c1")
    assert c["type"] == "card"
    assert c["accent"] == "purple"
    assert c["title"] == "Selected"

    p = pagination("pg1", page=2, total_pages=5, total_items=42, page_size=10)
    assert p["type"] == "pagination"
    assert p["page"] == 2
    assert p["totalPages"] == 5
    assert p["totalItems"] == 42
    assert p["pageSize"] == 10

    f = footer(
        left=[path_input("d1", default_value="/tmp", clearable=False, compact=True, default_dir="downloads")],
        right=[button("b1", "Go", disabled=True, disabled_tooltip="Need item")],
    )
    assert f["left"][0]["clearable"] is False
    assert f["left"][0]["compact"] is True
    assert f["left"][0]["defaultDir"] == "downloads"
    assert f["left"][0]["defaultValue"] == "/tmp"
    assert f["right"][0]["disabledTooltip"] == "Need item"

    img = image("https://example.com/poster.jpg", alt="Poster", width=130, height=195, aspect="poster", fit="cover")
    assert img["aspect"] == "poster"
    assert img["fit"] == "cover"
    assert img["width"] == 130
    assert img["height"] == 195


def test_initialize_downloads_dir_no_eager_mkdir_and_sticky_footer(tmp_path: Path):
    clean_env = {k: v for k, v in os.environ.items() if k not in ("OPENSUBTITLES_DOWNLOAD_DIR", "VESTA_DOWNLOADS_DIR")}
    with patch.dict(os.environ, clean_env, clear=True):
        os_downloads = tmp_path / "UserDownloads"
        data_dir = tmp_path / "data"
        mgr = ConfigManager(repo_root=tmp_path)
        client = FakeOpenSubtitlesClient()
        plugin = SubtitlesPlugin(config_manager=mgr, opensubtitles_client=client)

        init_res = plugin.initialize(
            api_version=1,
            data_dir=str(data_dir),
            permissions={"downloadsDir": str(os_downloads)},
        )
        assert not (data_dir / "downloads").exists()
        assert not (tmp_path / "downloads").exists()
        assert not os_downloads.exists()

        foot = init_res["footer"]
        dest_node = foot["left"][0]
        dl_btn = foot["right"][0]
        assert dest_node["id"] == "download_dest_dir"
        assert dest_node["defaultValue"] == str(os_downloads.resolve())
        assert dest_node["clearable"] is False
        assert dest_node["compact"] is True
        assert dest_node["defaultDir"] == "downloads"
        assert dl_btn["id"] == "btn_download_now"
        assert dl_btn["disabled"] is True
        assert dl_btn["disabledTooltip"] == "Select a subtitle from the results first"


def test_auto_select_poster_card_and_pagination(tmp_path: Path):
    items = [
        SubtitleItem(
            id=f"sub_{i}",
            subtitle_id=str(1000 + i),
            language="en",
            download_count=5000 - i * 10,
            new_download_count=1,
            hearing_impaired=False,
            hd=True,
            fps=23.976,
            ratings=8.5,
            votes=10,
            from_trusted=(i == 0),
            foreign_parts_only=False,
            ai_translated=False,
            machine_translated=False,
            release=f"Movie.2024.Release.{i}",
            comments="",
            movie_name="Movie",
            year=2024,
            poster_url="https://s9.osdb.link/poster.jpg" if i % 2 == 0 else None,
            files=[SubtitleFile(file_id=5000 + i, cd_number=1, file_name=f"Movie.{i}.srt")],
        )
        for i in range(25)
    ]

    class PagedClient(FakeOpenSubtitlesClient):
        def search(self, **kwargs) -> list[SubtitleItem]:
            return items

    mgr = ConfigManager(repo_root=tmp_path, data_dir=tmp_path / "data")
    plugin = SubtitlesPlugin(config_manager=mgr, opensubtitles_client=PagedClient())

    # Search auto-selects first result, resets page to 1, patches download_custom_name, and enables footer button
    search_res = plugin.handle_event("search", "btn_do_search", "click", {"search_query": "Movie"})
    assert plugin.selected_subtitle is not None
    assert plugin.selected_subtitle.id == "sub_0"
    assert plugin.current_page == 1
    assert search_res["patch"]["values"]["download_custom_name"] == "Movie.0.srt"
    assert search_res["footer"]["right"][0]["disabled"] is False

    # Selected card contains metadata+input on left and poster image on right
    card_node, _, _ = find_node(search_res["root"], "selected_subtitle_card")
    assert card_node is not None
    assert card_node["type"] == "card"
    assert card_node["accent"] == "purple"
    poster_node, _, _ = find_node(card_node, "selected_poster_image")
    assert poster_node is not None
    assert poster_node["src"] == "https://s9.osdb.link/poster.jpg"
    assert poster_node["aspect"] == "poster"
    assert poster_node["fit"] == "cover"

    # List renders 10 items on page 1 and no per-row posters
    list_node, _, _ = find_node(search_res["root"], "search_results_list")
    assert list_node is not None
    assert len(list_node["items"]) == 10
    assert list_node["items"][0]["id"] == "item_sub_0"
    assert list_node["items"][9]["id"] == "item_sub_9"
    assert all("image" not in item for item in list_node["items"])

    # Pagination widget present
    pg_node, _, _ = find_node(search_res["root"], "results_pagination")
    assert pg_node is not None
    assert pg_node["page"] == 1
    assert pg_node["totalPages"] == 3
    assert pg_node["totalItems"] == 25
    assert pg_node["pageSize"] == 10

    # Switch to page 2
    page2_res = plugin.handle_event("search", "results_pagination", "change", {"results_pagination": 2})
    assert page2_res.get("scrollTop") is True
    assert plugin.current_page == 2
    list_p2, _, _ = find_node(page2_res["root"], "search_results_list")
    assert len(list_p2["items"]) == 10
    assert list_p2["items"][0]["id"] == "item_sub_10"
    assert list_p2["items"][9]["id"] == "item_sub_19"

    # Select item without poster on page 2 -> patches download_custom_name and hides poster image
    sel_res = plugin.handle_event("search", "action_select_sub_11", "click", {})
    assert plugin.selected_subtitle.id == "sub_11"
    assert sel_res["patch"]["values"]["download_custom_name"] == "Movie.11.srt"
    assert find_node(sel_res["root"], "selected_poster_image")[0] is None
    assert plugin.current_page == 2

    # Switch to page 3 -> remaining 5 items
    page3_res = plugin.handle_event("search", "results_pagination", "change", {"results_pagination": 3})
    list_p3, _, _ = find_node(page3_res["root"], "search_results_list")
    assert len(list_p3["items"]) == 5
    assert list_p3["items"][0]["id"] == "item_sub_20"


def test_json_rpc_initialize_permissions_and_render_footer(tmp_path: Path):
    dl_dir = tmp_path / "RpcDownloads"
    in_stream = io.StringIO(
        json.dumps({
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"apiVersion": 1, "permissions": {"downloadsDir": str(dl_dir)}},
        }) + "\n"
        + json.dumps({"jsonrpc": "2.0", "id": 2, "method": "render", "params": {"screen": "search"}}) + "\n"
        + json.dumps({"jsonrpc": "2.0", "id": 3, "method": "render", "params": {"screen": "settings"}}) + "\n"
        + json.dumps({"jsonrpc": "2.0", "id": 4, "method": "shutdown", "params": {}}) + "\n"
    )
    out_stream = io.StringIO()
    mgr = ConfigManager(repo_root=tmp_path)
    plugin = SubtitlesPlugin(config_manager=mgr, opensubtitles_client=FakeOpenSubtitlesClient())
    server = JsonRpcServer(plugin=plugin, input_stream=in_stream, output_stream=out_stream)
    server.run()

    lines = [json.loads(line) for line in out_stream.getvalue().strip().splitlines() if line]
    assert len(lines) == 4
    assert lines[0]["result"]["footer"]["left"][0]["defaultValue"] == str(dl_dir.resolve())
    assert lines[1]["result"]["footer"]["left"][0]["id"] == "download_dest_dir"
    assert "footer" not in lines[2]["result"]


def test_single_search_mode_isolation_and_tv_series_ui(tmp_path: Path):
    captured_calls: list[dict[str, Any]] = []

    class CapturingClient(FakeOpenSubtitlesClient):
        def search(self, **kwargs) -> list[SubtitleItem]:
            captured_calls.append(kwargs)
            return [
                SubtitleItem(
                    id="bb_ep1",
                    subtitle_id="9001",
                    language="en",
                    download_count=15000,
                    new_download_count=100,
                    hearing_impaired=False,
                    hd=True,
                    fps=23.976,
                    ratings=9.4,
                    votes=80,
                    from_trusted=True,
                    foreign_parts_only=False,
                    ai_translated=False,
                    machine_translated=False,
                    release="Breaking.Bad.S01E01.720p.BluRay",
                    comments="",
                    movie_name='"Breaking Bad" Pilot',
                    year=2008,
                    feature_type="Episode",
                    parent_title="Breaking Bad",
                    episode_title="Pilot",
                    season_number=1,
                    episode_number=1,
                    parent_imdb_id=903747,
                    parent_tmdb_id=1396,
                    files=[SubtitleFile(file_id=9910, cd_number=1, file_name="Breaking.Bad.S01E01.srt")],
                )
            ]

    mgr = ConfigManager(repo_root=tmp_path, data_dir=tmp_path / "data")
    plugin = SubtitlesPlugin(config_manager=mgr, opensubtitles_client=CapturingClient())

    # Even if stale values exist for multiple fields, IMDb mode sends ONLY imdb_id (never query/tmdb_id)
    res = plugin.handle_event(
        "search",
        "btn_do_search",
        "click",
        {
            "search_mode": "imdb",
            "search_media_type": "tvshow",
            "search_query": "Breaking Bad",
            "search_tmdb_id": "1396",
            "search_imdb_id": "tt0903747",
            "search_season": "S01",
            "search_episode": "1",
            "search_languages": "en",
        },
    )
    assert len(captured_calls) == 1
    call = captured_calls[0]
    assert call["query"] == ""
    assert call["tmdb_id"] is None
    assert call["imdb_id"] == 903747
    assert call["media_type"] == "tvshow"
    assert call["season_number"] == 1
    assert call["episode_number"] == 1

    # Selected card shows Series & Episode lines cleanly
    card_node, _, _ = find_node(res["root"], "selected_subtitle_card")
    assert card_node is not None
    rendered_texts = json.dumps(card_node)
    assert "Series: Breaking Bad (2008)" in rendered_texts
    assert "Episode: S01E01" in rendered_texts
    assert "Pilot" in rendered_texts

    # List item shows clean series/episode title + S01E01 badge
    list_node, _, _ = find_node(res["root"], "search_results_list")
    assert list_node is not None
    item = list_node["items"][0]
    assert item["title"] == "Breaking Bad — Pilot"
    assert "S01E01" in item["badge"]

    # Invalid TMDB ID produces a clear validation error without calling client
    bad_res = plugin.handle_event(
        "search",
        "btn_do_search",
        "click",
        {"search_mode": "tmdb", "search_tmdb_id": "not-an-id"},
    )
    assert len(captured_calls) == 1
    assert plugin.status_variant == "danger"
    assert "Invalid TMDB ID" in (plugin.status_message or "")

    # Switching from tvshow back to movie clears season & episode
    switch_res = plugin.handle_event(
        "search",
        "search_media_type",
        "change",
        {"search_media_type": "movie", "search_season": "1", "search_episode": "2"},
    )
    assert plugin.search_season == ""
    assert plugin.search_episode == ""
    assert switch_res["patch"]["values"] == {"search_season": "", "search_episode": ""}
    assert find_node(switch_res["root"], "search_season")[0] is None


class TestRpcAndPluginSuite(unittest.TestCase):
    def test_all(self):
        test_schema_single_object_patch()
        test_schema_new_builders()
        for fn in (
            test_plugin_full_flow,
            test_plugin_local_extraction_flow,
            test_plugin_settings_save_and_test,
            test_json_rpc_server_loop,
            test_ui_layout_and_expert_widgets,
            test_search_guidance_and_local_key,
            test_remove_active_key_clears_form_and_disk,
            test_vesta_cli_inspect_and_sync_workflow,
            test_rpc_send_request_synchronous_response,
            test_plugin_emits_vesta_log_and_detects_tmdb_keys,
            test_initialize_downloads_dir_no_eager_mkdir_and_sticky_footer,
            test_auto_select_poster_card_and_pagination,
            test_json_rpc_initialize_permissions_and_render_footer,
            test_single_search_mode_isolation_and_tv_series_ui,
        ):
            with tempfile.TemporaryDirectory() as td:
                fn(Path(td))


def test_download_history_keeps_metadata_separate_from_search_and_clears_video(tmp_path: Path):
    client = FakeOpenSubtitlesClient()
    notifications = []
    plugin = SubtitlesPlugin(config_manager=ConfigManager(repo_root=tmp_path, data_dir=tmp_path / "data"),
                             opensubtitles_client=client, notifier=lambda method, params: notifications.append((method, params)))
    plugin.initialize(data_dir=str(tmp_path / "data"))
    plugin.handle_event("search", "btn_do_search", "click", {"search_query": "Matrix"})
    result = plugin.handle_event("search", "btn_download_now", "click", {
        "download_dest_dir": str(tmp_path / "subs"), "download_custom_name": "Matrix-NTG-HI.srt"})
    assert result["screen"] == "downloads"
    assert find_node(result["root"], "btn_use_in_synchronize")[0]["icon"] == "refresh"
    assert find_node(result["root"], "btn_use_in_flashcards")[0]["icon"] == "cards"
    assert find_node(plugin.render_screen("search"), "video_for_flashcards")[0] is None
    assert find_node(plugin.render_screen("search"), "btn_nav_downloads")[0]["placement"] == "header"
    plugin.handle_event("downloads", "action_download_0", "click", {"video_for_flashcards": "/media/matrix.mp4"})
    assert plugin.associated_video_path == "/media/matrix.mp4"
    # A subsequent search result must not change the downloaded file's language.
    client.items[0].language = "ja"
    plugin.handle_event("downloads", "btn_use_in_flashcards", "click", {"video_for_flashcards": "/media/matrix.mp4"})
    handoff = [params for method, params in notifications if method == "vesta.openInFlashcards"][-1]
    assert handoff["subtitleLanguage"] == "en"
    assert handoff["subtitlePath"].endswith("-HI.srt")
    plugin.handle_event("downloads", "btn_use_in_synchronize", "click", {"video_for_flashcards": ""})
    assert notifications[-1] == ("vesta.openInSynchronize", {"subtitlePath": handoff["subtitlePath"], "videoPath": ""})
    restored = SubtitlesPlugin(config_manager=ConfigManager(repo_root=tmp_path, data_dir=tmp_path / "data"))
    restored.initialize(data_dir=str(tmp_path / "data"))
    assert len(restored.download_history.items) == 1
    selected = restored.handle_event("downloads", "action_download_0", "click", {})
    assert selected["patch"]["values"]["video_for_flashcards"] == ""
    assert restored.downloaded_subtitle_language == "en"
    assert restored.downloaded_subtitle_path == handoff["subtitlePath"]


def test_download_history_recovers_from_corrupt_storage_and_bounds_entries(tmp_path: Path):
    from subtitles_plugin.downloads import DownloadHistory
    history_file = tmp_path / "download-history.json"
    history_file.write_text("invalid json")
    history = DownloadHistory(tmp_path)
    assert history.items == []
    history.LIMIT = 2
    for path in ["one.srt", "two.srt", "three.srt", "two.srt"]:
        history.remember({"path": path, "language": "en", "title": path, "video": ""})
    assert [entry["path"] for entry in history.items] == ["two.srt", "three.srt"]
    assert len(DownloadHistory(tmp_path).items) == 2


def test_download_navigation_preference_persists_and_keeps_search_results(tmp_path: Path):
    data = tmp_path / "data"
    manager = ConfigManager(repo_root=tmp_path, data_dir=data)
    plugin = SubtitlesPlugin(config_manager=manager, opensubtitles_client=FakeOpenSubtitlesClient())
    plugin.handle_event("search", "btn_do_search", "click", {"search_query": "Matrix"})
    plugin.handle_event("settings", "btn_save_settings", "click", {
        "cfg_api_key": "fake_key", "cfg_open_downloads_after_download": False,
    })
    assert ConfigManager(repo_root=tmp_path, data_dir=data).settings.open_downloads_after_download is False
    result = plugin.handle_event("search", "btn_download_now", "click", {"download_dest_dir": str(tmp_path / "subs")})
    assert result["screen"] == "search"
    assert plugin.search_query == "Matrix"
    assert len(plugin.search_results) == 1
    assert len(plugin.download_history.items) == 1
    assert find_node(result["root"], "btn_nav_downloads")[0] is not None


def _download_for_removal(tmp_path: Path):
    plugin = SubtitlesPlugin(config_manager=ConfigManager(repo_root=tmp_path, data_dir=tmp_path / "data"),
                             opensubtitles_client=FakeOpenSubtitlesClient())
    plugin.handle_event("search", "btn_do_search", "click", {"search_query": "Matrix"})
    plugin.handle_event("search", "btn_download_now", "click", {"download_dest_dir": str(tmp_path / "subs")})
    subtitle = Path(plugin.downloaded_subtitle_path)
    video = tmp_path / "matrix.mp4"
    video.write_bytes(b"video")
    plugin._save_download_video({"video_for_flashcards": str(video)})
    return plugin, subtitle, video


def test_remove_download_requires_confirmation_and_preserves_files_by_default(tmp_path: Path):
    plugin, subtitle, video = _download_for_removal(tmp_path)
    response = plugin.handle_event("downloads", "action_remove_download_0", "click", {})
    assert response["patch"]["values"]["remove_srt_file"] is False
    assert subtitle.exists() and len(plugin.download_history.items) == 1
    plugin.handle_event("downloads", "btn_cancel_remove_download", "click", {})
    plugin.handle_event("downloads", "btn_confirm_remove_download", "click", {"remove_srt_file": True})
    assert subtitle.exists() and len(plugin.download_history.items) == 1
    plugin.handle_event("downloads", "action_remove_download_0", "click", {})
    plugin.handle_event("downloads", "btn_confirm_remove_download", "click", {})
    assert subtitle.exists() and video.exists()
    assert plugin.download_history.items == []
    assert plugin.downloaded_subtitle_path is None
    assert SubtitlesPlugin(config_manager=plugin.config_manager).download_history.items == []


def test_remove_download_can_delete_only_the_srt_and_keeps_video(tmp_path: Path):
    plugin, subtitle, video = _download_for_removal(tmp_path)
    plugin.handle_event("downloads", "action_remove_download_0", "click", {})
    plugin.handle_event("downloads", "btn_confirm_remove_download", "click", {"remove_srt_file": True})
    assert not subtitle.exists() and video.exists()
    assert plugin.download_history.items == []


def test_missing_download_can_be_removed_and_dry_run_preserves_entries(tmp_path: Path):
    plugin, subtitle, video = _download_for_removal(tmp_path)
    plugin.config_manager.update_settings({"dry_run_default": True}, save=False)
    plugin.handle_event("downloads", "action_remove_download_0", "click", {})
    plugin.handle_event("downloads", "btn_confirm_remove_download", "click", {"remove_srt_file": True})
    assert subtitle.exists() and video.exists() and len(plugin.download_history.items) == 1
    plugin.config_manager.update_settings({"dry_run_default": False}, save=False)
    subtitle.unlink()
    plugin.handle_event("downloads", "action_remove_download_0", "click", {})
    plugin.handle_event("downloads", "btn_confirm_remove_download", "click", {"remove_srt_file": True})
    assert plugin.download_history.items == [] and video.exists()


def test_failed_file_deletion_preserves_download_history(tmp_path: Path):
    plugin, subtitle, video = _download_for_removal(tmp_path)
    plugin.handle_event("downloads", "action_remove_download_0", "click", {})
    with patch.object(Path, "unlink", side_effect=PermissionError("read-only")):
        plugin.handle_event("downloads", "btn_confirm_remove_download", "click", {"remove_srt_file": True})
    assert len(plugin.download_history.items) == 1 and subtitle.exists() and video.exists()


def test_plugin_version_matches_package_and_manifest():
    import re
    from subtitles_plugin import __version__
    root = Path(__file__).resolve().parents[1]
    assert __version__ == json.loads((root / "vesta-plugin.json").read_text())["version"]
    assert __version__ == re.search(r'(?m)^version = "([^"]+)"', (root / "pyproject.toml").read_text()).group(1)


def test_removal_targets_the_confirmed_path_after_history_reordering(tmp_path: Path):
    plugin, subtitle, video = _download_for_removal(tmp_path)
    plugin.handle_event("downloads", "action_remove_download_0", "click", {})
    other = tmp_path / "other.srt"
    other.write_text("Other subtitle")
    plugin.download_history.remember({"path": str(other), "language": "it", "title": "Other", "video": ""})
    plugin.handle_event("downloads", "btn_confirm_remove_download", "click", {"remove_srt_file": True})
    assert not subtitle.exists() and other.exists() and video.exists()
    assert [item["path"] for item in plugin.download_history.items] == [str(other)]


def test_removal_request_keeps_clicked_entry_when_saving_video_reorders_history(tmp_path: Path):
    plugin, subtitle, video = _download_for_removal(tmp_path)
    other = tmp_path / "other.srt"
    other.write_text("Other subtitle")
    plugin.download_history.remember({"path": str(other), "language": "it", "title": "Other", "video": ""})
    plugin.handle_event("downloads", "action_remove_download_0", "click", {"video_for_flashcards": ""})
    plugin.handle_event("downloads", "btn_confirm_remove_download", "click", {"remove_srt_file": True})
    assert subtitle.exists() and not other.exists() and video.exists()
    assert [item["path"] for item in plugin.download_history.items] == [str(subtitle)]
