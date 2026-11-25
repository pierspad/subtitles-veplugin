# SPDX-License-Identifier: GPL-3.0-or-later
"""Subtitles Vesta Plugin Controller.

Coordinates screens, events, OpenSubtitles search, local track extraction,
and spaced-repetition flashcards integration.
Emits declarative widget UI schemas and single-object PluginPatch updates
compliant with the Vesta Plugin System Specification (v1).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable

from subtitles_plugin.config import ConfigManager, OpenSubtitlesCredentials
from subtitles_plugin.extractor import ExtractionResult, LocalSubExtractor, SubtitleTrackInfo
from subtitles_plugin.opensubtitles import (
    OpenSubtitlesClient,
    SubtitleItem,
    parse_imdb_id,
    parse_season_episode_value,
    parse_tmdb_id,
)
from subtitles_plugin.schema import (
    apply_patches,
    button,
    card,
    checkbox,
    column,
    divider,
    external_link,
    file_output,
    footer,
    heading,
    image,
    language_select,
    link,
    list_item,
    list_widget,
    make_patch,
    number_input,
    pagination,
    password_input,
    path_input,
    progress,
    row,
    segmented,
    select,
    table,
    text,
    text_input,
)


class SubtitlesPlugin:
    """Core state and screen controller for the Subtitles Vesta plugin."""

    PAGE_SIZE: int = 10

    def __init__(
        self,
        config_manager: ConfigManager | None = None,
        opensubtitles_client: OpenSubtitlesClient | None = None,
        extractor: LocalSubExtractor | None = None,
        notifier: Callable[[str, dict[str, Any]], None] | None = None,
        requester: Callable[[str, dict[str, Any]], Any] | None = None,
    ):
        self.config_manager = config_manager or ConfigManager()
        self._custom_client = opensubtitles_client
        self._custom_extractor = extractor
        self.notifier = notifier
        self.requester = requester
        self.downloads_dir: str | None = None

        # Navigation & Screen State
        self.current_screen: str = "search"  # 'search', 'local', 'settings'
        self.status_message: str | None = None
        self.status_variant: str = "normal"  # 'normal', 'muted', 'danger', 'success'

        # Search Screen State
        self.search_mode: str = "title"  # 'title', 'tmdb', 'imdb'
        self.search_media_type: str = "all"  # 'all', 'movie', 'tvshow'
        self.search_query: str = ""
        self.search_tmdb_id: str = ""
        self.search_imdb_id: str = ""
        self.search_season: str = ""
        self.search_episode: str = ""
        self.search_languages: str = ""
        self.search_results: list[SubtitleItem] = []
        self.selected_subtitle: SubtitleItem | None = None
        self.current_page: int = 1
        self.last_search_executed: bool = False
        self.downloaded_subtitle_path: str | None = None
        self.associated_video_path: str = ""

        # Local File Extraction State
        self.local_video_path: str = ""
        self.local_tracks: list[SubtitleTrackInfo] = []
        self.extracted_subtitle_path: str | None = None
        self.last_extraction_result: ExtractionResult | None = None

        # Vesta CLI Subtitle Revision & Retiming State
        self.cli_inspection_summary: str | None = None
        self.cli_preview_rows: list[list[str]] = []
        self.cli_shift_ms: int = 0

    @property
    def opensubtitles_client(self) -> OpenSubtitlesClient:
        return self.create_client()

    def create_client(self, credentials: OpenSubtitlesCredentials | None = None) -> OpenSubtitlesClient:
        """Returns the configured OpenSubtitles client (or custom mock client)."""
        if self._custom_client:
            return self._custom_client
        creds = credentials or self.config_manager.resolve_credentials()
        return OpenSubtitlesClient(credentials=creds)

    @property
    def extractor(self) -> LocalSubExtractor:
        if self._custom_extractor:
            return self._custom_extractor
        return LocalSubExtractor(
            ffmpeg_bin=self.config_manager.settings.ffmpeg_path or "ffmpeg",
            ffprobe_bin=self.config_manager.settings.ffprobe_path or "ffprobe",
            dry_run=self.config_manager.resolve_dry_run(),
        )


    def emit_ui_update(
        self,
        screen: str = "search",
        patch: dict[str, Any] | None = None,
        root: dict[str, Any] | None = None,
    ) -> None:
        """Emits a 'ui.update' JSON-RPC notification to the Vesta host."""
        if not (self.notifier and callable(self.notifier)):
            return

        params: dict[str, Any] = {"screen": screen}
        if patch is not None:
            unwrapped = patch.get("patch", patch) if isinstance(patch, dict) else patch
            params["patch"] = unwrapped
            params["patches"] = unwrapped
        if root is not None:
            params["root"] = root

        self.notifier("ui.update", params)

    def notify(self, msg: str, level: str = "info") -> None:
        """Emits a standard toast 'ui.notify' to Vesta (info, success, warn, error) and records a support log."""
        if self.notifier and callable(self.notifier):
            self.notifier("ui.notify", {"level": level, "msg": msg})
        self.log(msg, level=level)

    def log(self, msg: str, level: str = "info") -> None:
        """Emits a structured 'vesta.log' diagnostic notification captured by Vesta Support Logs."""
        if self.notifier and callable(self.notifier):
            self.notifier("vesta.log", {"level": level, "msg": msg})

    def report_progress(self, id: str, value: float, label: str = "") -> None:
        """Emits a 'ui.progress' notification to Vesta."""
        if self.notifier and callable(self.notifier):
            self.notifier("ui.progress", {"id": id, "value": value, "label": label})

    def open_in_flashcards(self, video_path: str, subtitle_path: str) -> None:
        """Emits 'vesta.openInFlashcards' notification to switch to Flashcards."""
        if self.notifier and callable(self.notifier):
            self.notifier(
                "vesta.openInFlashcards",
                {"videoPath": str(video_path), "subtitlePath": str(subtitle_path)},
            )

    def get_values(self, screen: str = "search") -> dict[str, Any]:
        """Queries interactive screen values from Vesta via 'ui.getValues'."""
        if self.requester and callable(self.requester):
            res = self.requester("ui.getValues", {"screen": screen})
            if isinstance(res, dict) and "values" in res:
                return res["values"]
            return res if isinstance(res, dict) else {}
        return {}

    def run_cli(
        self,
        command: str,
        args: list[str] | None = None,
        timeout_secs: int = 120,
    ) -> dict[str, Any]:
        """Executes a `vesta-cli` subcommand via the Vesta host `vesta.cli` JSON-RPC bridge.

        Requires `"vesta:cli"` in `vesta-plugin.json` `permissions`.
        """
        if not (self.requester and callable(self.requester)):
            return {}
        import json as _json

        res = self.requester(
            "vesta.cli",
            {
                "command": str(command),
                "args": [str(a) for a in (args or [])],
                "timeoutSecs": int(timeout_secs),
            },
        )
        if not isinstance(res, dict):
            return {"result": res}
        stdout = res.get("stdout", "")
        if isinstance(stdout, str):
            stripped = stdout.strip()
            if stripped.startswith("{") or stripped.startswith("["):
                try:
                    res["json"] = _json.loads(stripped)
                except Exception:
                    pass
        return res

    # Lifecycle

    def initialize(
        self,
        api_version: int = 1,
        data_dir: str | None = None,
        locale: str = "en",
        settings: dict[str, Any] | None = None,
        permissions: dict[str, Any] | None = None,
        downloads_dir: str | None = None,
    ) -> dict[str, Any]:
        """Handles Vesta 'initialize' method."""
        if data_dir:
            self.config_manager.set_data_dir(Path(data_dir))
        resolved_downloads_dir = downloads_dir
        if resolved_downloads_dir is None and isinstance(permissions, dict):
            raw_dl = permissions.get("downloadsDir")
            if isinstance(raw_dl, str) and raw_dl.strip():
                resolved_downloads_dir = raw_dl.strip()
        if resolved_downloads_dir:
            self.downloads_dir = resolved_downloads_dir
            self.config_manager.set_downloads_dir(resolved_downloads_dir)
        if settings and isinstance(settings, dict):
            self.config_manager.update_settings(settings)

        result: dict[str, Any] = {
            "status": "ok",
            "ready": True,
            "initialScreen": "main",
            "root": self.render_screen("search"),
        }
        if footer_obj := self.render_footer("search"):
            result["footer"] = footer_obj
        return result

    # Screen Rendering

    def _finalize_root(self, root: dict[str, Any]) -> dict[str, Any]:
        """Attach a host snackbar notification to the root widget."""
        if self.status_message:
            stype = (
                "error"
                if self.status_variant == "danger"
                else ("success" if self.status_variant == "success" else "info")
            )
            root["snackbar"] = {"message": self.status_message, "type": stype}
        return root

    def render_screen(self, screen: str, values: dict[str, Any] | None = None) -> dict[str, Any]:
        """Renders declarative widget tree for the requested screen."""
        normalized = "search" if screen in ("main", "search") else screen
        self.current_screen = normalized

        if normalized == "search":
            root = self._render_search_screen(values)
        elif normalized == "local":
            root = self._render_local_screen(values)
        elif normalized == "settings":
            root = self._render_settings_screen(values)
        else:
            root = self._render_search_screen(values)
        return self._finalize_root(root)

    def render_footer(self, screen: str) -> dict[str, Any] | None:
        """Returns the sticky footer definition for `screen`, or None."""
        normalized = "search" if screen in ("main", "search") else screen
        if normalized == "search":
            return self._render_search_footer()
        return None

    def _screen_response(
        self,
        screen_name: str,
        values: dict[str, Any] | None = None,
        /,
        **extra: Any,
    ) -> dict[str, Any]:
        """Builds a screen response payload including `root` and optional `footer`."""
        res: dict[str, Any] = {"root": self.render_screen(screen_name, values)}
        if footer_obj := self.render_footer(screen_name):
            res["footer"] = footer_obj
        res.update(extra)
        return res

    def _render_search_footer(self) -> dict[str, Any]:
        """Builds the sticky bottom footer for the Search screen."""
        dest_dir = str(self.config_manager.resolve_download_dir(create=False))
        return footer(
            left=[
                path_input(
                    "download_dest_dir",
                    label="Destination Folder",
                    default_value=dest_dir,
                    picker="directory",
                    mode="directory",
                    clearable=False,
                    compact=True,
                    default_dir="downloads",
                    tooltip="Destination folder for downloaded subtitles",
                ),
            ],
            right=[
                button(
                    "btn_download_now",
                    "Download Subtitle",
                    variant="primary",
                    icon="download",
                    disabled=self.selected_subtitle is None,
                    disabled_tooltip="Select a subtitle from the results first",
                ),
            ],
        )

    def _settings_action(self) -> dict[str, Any]:
        key_ready = self.config_manager.resolve_credentials().is_configured
        return button(
            "btn_nav_settings", "Settings", variant="settings",
            status=None if key_ready else "attention",
            tooltip="OpenSubtitles key configured" if key_ready else "Add an OpenSubtitles API key",
            icon="settings", placement="header",
        )

    def _render_status_banner(self) -> dict[str, Any]:
        """Renders status message banner (hidden in favor of Vesta host snackbars)."""
        return text("", id="status_banner", hidden=True)

    def _render_cli_revision_section(self) -> list[dict[str, Any]]:
        """Renders the declarative 'Subtitle Revision & Retiming (Vesta CLI)' panel."""
        widgets: list[dict[str, Any]] = [
            divider(),
            heading("Subtitle Revision & Retiming (Vesta CLI)", level=3),
            text(
                "Inspect subtitle cues or shift timestamps via the native vesta-cli engine before opening in Flashcards.",
                variant="muted",
                size="sm",
            ),
            row(
                [
                    number_input(
                        "cli_shift_ms",
                        label="Time Shift (ms)",
                        defaultValue=self.cli_shift_ms,
                        step=100,
                        min_val=-3600000,
                        max_val=3600000,
                        tooltip="Shift all subtitle timestamps (+ delays, - advances) via vesta-cli sync",
                    ),
                    button(
                        "btn_cli_inspect",
                        "Inspect Cues (vesta-cli)",
                        variant="secondary",
                        icon="search",
                        tooltip="Inspect cue count, timing span, and preview rows via vesta.cli",
                    ),
                    button(
                        "btn_cli_sync",
                        "Apply Time Shift (vesta-cli)",
                        variant="secondary",
                        icon="refresh",
                        tooltip="Shift all subtitle timestamps using vesta-cli sync --offset-ms",
                    ),
                ],
                gap=3,
                align="end",
            ),
        ]
        if self.cli_inspection_summary:
            widgets.append(
                text(
                    self.cli_inspection_summary,
                    variant="success",
                    size="sm",
                    id="cli_inspection_summary",
                )
            )
        if self.cli_preview_rows:
            widgets.append(
                table(
                    ["#", "Timing", "Subtitle Text"],
                    self.cli_preview_rows,
                    id="cli_cue_preview_table",
                )
            )
        return widgets

    # Screen: Search

    @staticmethod
    def _active_search_input_id(mode: str) -> str:
        if mode == "tmdb":
            return "search_tmdb_id"
        if mode == "imdb":
            return "search_imdb_id"
        return "search_query"

    def _build_active_search_input(self) -> dict[str, Any]:
        is_tv = self.search_media_type == "tvshow"
        is_movie = self.search_media_type == "movie"
        if self.search_mode == "tmdb":
            return text_input(
                "search_tmdb_id",
                label="Series TMDB ID" if is_tv else "TMDB ID",
                placeholder="e.g. 1396 (Breaking Bad)" if is_tv else "e.g. 27205 (or themoviedb.org URL)",
                defaultValue=self.search_tmdb_id,
                flex=3,
                icon="tmdb",
                labelTooltip="TMDB TV Series ID (parent_tmdb_id)" if is_tv else "TMDB ID",
                tooltip="Enter a numeric TMDB ID or paste a themoviedb.org URL",
                submitAction="btn_do_search",
            )
        if self.search_mode == "imdb":
            return text_input(
                "search_imdb_id",
                label="Series IMDb ID" if is_tv else "IMDb ID",
                placeholder="e.g. tt0903747 (Breaking Bad)" if is_tv else "e.g. tt1375666 (or imdb.com URL)",
                defaultValue=self.search_imdb_id,
                flex=3,
                icon="imdb",
                labelTooltip="IMDb TV Series ID (parent_imdb_id)" if is_tv else "IMDb ID",
                tooltip="Enter an IMDb ID like tt0903747 or paste an imdb.com URL",
                submitAction="btn_do_search",
            )
        return text_input(
            "search_query",
            label="Series Title" if is_tv else ("Movie Title" if is_movie else "Title"),
            placeholder=(
                "e.g. Breaking Bad or Breaking Bad S01E01"
                if is_tv
                else (
                    "e.g. Inception, The Matrix, or Interstellar"
                    if is_movie
                    else "e.g. Inception, Breaking Bad, or Breaking Bad S01E01"
                )
            ),
            defaultValue=self.search_query,
            flex=3,
            icon="search",
            submitAction="btn_do_search",
        )

    def _render_search_screen(self, values: dict[str, Any] | None = None) -> dict[str, Any]:
        creds = self.config_manager.resolve_credentials()
        pref_langs = ",".join(self.config_manager.resolve_preferred_languages())
        active_input_id = self._active_search_input_id(self.search_mode)
        is_tv = self.search_media_type == "tvshow"

        children: list[dict[str, Any]] = [
            self._settings_action(),
            self._render_status_banner(),
        ]

        # Filter & Search Mode Controls (one search mode active at a time)
        search_bar_children: list[dict[str, Any]] = [self._build_active_search_input()]
        if is_tv:
            search_bar_children.extend(
                [
                    text_input(
                        "search_season",
                        label="Season",
                        placeholder="All",
                        defaultValue=self.search_season,
                        width=96,
                        tooltip="Season number (optional — leave blank for all seasons)",
                        submitAction="btn_do_search",
                    ),
                    text_input(
                        "search_episode",
                        label="Episode",
                        placeholder="All",
                        defaultValue=self.search_episode,
                        width=96,
                        tooltip="Episode number (optional — leave blank for full season)",
                        submitAction="btn_do_search",
                    ),
                ]
            )
        search_bar_children.extend(
            [
                button(
                    "btn_do_search",
                    "Search",
                    variant="accent",
                    icon="search",
                    tooltip="Search OpenSubtitles using the selected search mode",
                    disabled=not creds.is_configured,
                    disabledTooltip="Open Settings and add an OpenSubtitles API key to search",
                    disabledFocus="btn_nav_settings",
                    requiresAny=[active_input_id],
                ),
                button(
                    "btn_clear_search",
                    "Clear",
                    variant="danger",
                    icon="trash",
                    tooltip="Clear search filters and results",
                    requiresAny=[]
                    if (
                        self.search_results
                        or self.selected_subtitle
                        or self.last_search_executed
                        or (is_tv and (self.search_season or self.search_episode))
                    )
                    else [active_input_id],
                ),
            ]
        )

        children.extend(
            [
                row(
                    [
                        language_select(
                            "search_languages",
                            label="Language",
                            names="both",
                            defaultValue=self.search_languages or pref_langs,
                            flex=1,
                            tooltip="Filter subtitles by language",
                        ),
                        segmented(
                            "search_media_type",
                            label="Content",
                            defaultValue=self.search_media_type,
                            options=[
                                {
                                    "value": "all",
                                    "label": "All",
                                    "tooltip": "Search both movies and TV series",
                                },
                                {
                                    "value": "movie",
                                    "label": "Movie",
                                    "icon": "film",
                                    "tooltip": "Search movies only",
                                },
                                {
                                    "value": "tvshow",
                                    "label": "TV Series",
                                    "icon": "subtitles",
                                    "tooltip": "Search TV series with Season & Episode filters",
                                },
                            ],
                        ),
                        segmented(
                            "search_mode",
                            label="Search By",
                            defaultValue=self.search_mode,
                            options=[
                                {
                                    "value": "title",
                                    "label": "Title",
                                    "icon": "search",
                                    "tooltip": "Search by title or release name (supports S01E01)",
                                },
                                {
                                    "value": "tmdb",
                                    "label": "TMDB ID",
                                    "icon": "tmdb",
                                    "tooltip": "Search by TMDB ID or themoviedb.org URL",
                                },
                                {
                                    "value": "imdb",
                                    "label": "IMDb ID",
                                    "icon": "imdb",
                                    "tooltip": "Search by IMDb ID (e.g. tt1375666) or imdb.com URL",
                                },
                            ],
                        ),
                    ],
                    gap=3,
                    align="end",
                    id="search_filter_row",
                ),
                row(
                    search_bar_children,
                    gap=3,
                    align="end",
                    id="search_input_row",
                ),
                divider(),
            ]
        )

        # Selected Subtitle Card + Optional Poster (placed above results for immediate visibility)
        if self.selected_subtitle:
            sub = self.selected_subtitle
            rating_str = f"{sub.ratings:.1f}" if sub.ratings else "N/A"
            year_suffix = (
                f" ({sub.year})"
                if sub.year and str(sub.year) not in (sub.clean_series_title if sub.is_episode else (sub.movie_name or ""))
                else ""
            )

            detail_lines: list[dict[str, Any]] = []
            if sub.is_episode:
                series_name = sub.clean_series_title or "N/A"
                detail_lines.append(text(f"Series: {series_name}{year_suffix}"))
                ep_parts = [p for p in (sub.episode_code, sub.episode_title) if p]
                detail_lines.append(text(f"Episode: {' — '.join(ep_parts) if ep_parts else 'N/A'}"))
            else:
                movie_label = f"{sub.movie_name}{year_suffix}" if sub.movie_name else "N/A"
                detail_lines.append(text(f"Movie / Title: {movie_label}"))

            detail_lines.extend(
                [
                    text(f"Release: {sub.release or 'Standard'}"),
                    text(f"Language: {sub.language.upper()} | Downloads: {sub.download_count:,} | Rating: {rating_str}"),
                    text(f"Hearing Impaired: {'Yes' if sub.hearing_impaired else 'No'} | Trusted Source: {'Yes' if sub.from_trusted else 'No'}"),
                    text_input(
                        "download_custom_name",
                        label="File Name",
                        defaultValue=sub.primary_file_name,
                    ),
                ]
            )

            left_col = column(
                detail_lines,
                gap=2,
                flex=3,
            )
            card_row_children: list[dict[str, Any]] = [left_col]
            if sub.poster_url:
                card_row_children.append(
                    image(
                        src=sub.poster_url,
                        alt=sub.display_title or sub.primary_file_name,
                        width=130,
                        height=195,
                        aspect="poster",
                        fit="cover",
                        id="selected_poster_image",
                    )
                )

            children.extend(
                [
                    card(
                        [row(card_row_children, gap=4, align="start")],
                        title=f"Selected: {sub.primary_file_name}",
                        icon="subtitles",
                        accent="purple",
                        id="selected_subtitle_card",
                    ),
                    divider(),
                ]
            )

        # Downloaded Subtitle & Flashcards Handoff
        if self.downloaded_subtitle_path:
            children.extend(
                [
                    heading("Subtitle Downloaded", level=3),
                    file_output(self.downloaded_subtitle_path, label="Saved Subtitle (.srt)"),
                    path_input(
                        "video_for_flashcards",
                        label="Associated Video File (optional, for Flashcards)",
                        placeholder="Path to matching video file...",
                        defaultValue=self.associated_video_path,
                        picker="file",
                        mode="file",
                    ),
                    row(
                        [
                            button("btn_use_in_flashcards", "Use in Flashcards", variant="primary"),
                        ],
                        gap=2,
                    ),
                    *self._render_cli_revision_section(),
                    divider(),
                ]
            )

        # Search Results List (paginated at PAGE_SIZE per page)
        if self.search_results:
            total_items = len(self.search_results)
            total_pages = max(1, (total_items + self.PAGE_SIZE - 1) // self.PAGE_SIZE)
            page = max(1, min(self.current_page, total_pages))
            self.current_page = page
            start_idx = (page - 1) * self.PAGE_SIZE
            page_results = self.search_results[start_idx : start_idx + self.PAGE_SIZE]

            items: list[dict[str, Any]] = []
            for sub in page_results:
                is_selected = self.selected_subtitle is not None and self.selected_subtitle.id == sub.id
                title_line = sub.display_title
                if not sub.is_episode and sub.year and str(sub.year) not in title_line:
                    title_line = f"{title_line} ({sub.year})"
                rating_str = f"{sub.ratings:.1f}" if sub.ratings else "N/A"
                meta_items = [
                    f"{sub.download_count:,} downloads",
                    f"Rating: {rating_str}",
                ]
                sub_line = sub.release or sub.primary_file_name
                base_badge = sub.badge_text
                badge_text = (
                    f"SELECTED | {base_badge}"
                    if (is_selected and base_badge)
                    else ("SELECTED" if is_selected else base_badge)
                )
                items.append(
                    list_item(
                        id=f"item_{sub.id}",
                        title=title_line,
                        subtitle=sub_line,
                        meta=meta_items,
                        language=sub.language.lower() if sub.language else None,
                        badge=badge_text or None,
                        actions=[
                            {
                                "id": f"action_select_{sub.id}",
                                "label": "Selected" if is_selected else "Select",
                                "variant": "success" if is_selected else "primary",
                                "icon": "check",
                                "tooltip": "Currently selected subtitle track"
                                if is_selected
                                else "Select this subtitle track (or double-click row)",
                            },
                        ],
                    )
                )

            children.extend(
                [
                    heading(f"Results ({total_items})", level=3),
                    list_widget("search_results_list", items=items),
                ]
            )
            if total_items > self.PAGE_SIZE:
                children.append(
                    pagination(
                        "results_pagination",
                        page=page,
                        total_pages=total_pages,
                        total_items=total_items,
                        page_size=self.PAGE_SIZE,
                    )
                )
            children.append(divider())
        elif self.last_search_executed:
            empty_msg = "No subtitles found matching query."
            if is_tv and self.search_mode in ("tmdb", "imdb") and not (self.search_season or self.search_episode):
                empty_msg = (
                    "No subtitles found for this TV series ID. "
                    "Try specifying a Season and Episode number, or search by Series Title."
                )
            children.extend(
                [
                    text(empty_msg, variant="muted"),
                    divider(),
                ]
            )

        return column(children, gap=3, id="search_screen")

    # Screen: Local Video File Extraction

    def _render_local_screen(self, values: dict[str, Any] | None = None) -> dict[str, Any]:
        bin_status = self.extractor.check_binaries()
        ffprobe_ok = bin_status.get("ffprobe", False)
        ffmpeg_ok = bin_status.get("ffmpeg", False)

        children: list[dict[str, Any]] = [
            self._render_status_banner(),
        ]

        # Status of FFmpeg tools
        if not (ffprobe_ok and ffmpeg_ok):
            children.append(
                column(
                    [
                        text(
                            "FFmpeg / ffprobe tools were not found on your system PATH. "
                            "Extraction of embedded tracks requires FFmpeg.",
                            variant="danger",
                        ),
                        text(
                            f"Status: ffprobe={'Found' if ffprobe_ok else 'Missing'}, "
                            f"ffmpeg={'Found' if ffmpeg_ok else 'Missing'}",
                            variant="muted",
                            size="sm",
                        ),
                    ],
                    gap=1,
                    id="ffmpeg_warning_box",
                )
            )
        else:
            children.append(
                text(
                    "Inspect embedded subtitle tracks (SubRip, ASS, PGS) and extract to .srt.",
                    variant="muted",
                    size="sm",
                )
            )

        # Input video file
        children.extend(
            [
                path_input(
                    "local_video_path",
                    label="Video File Path",
                    placeholder="/path/to/movie.mkv or C:\\media\\movie.mp4",
                    defaultValue=self.local_video_path,
                    picker="file",
                    mode="file",
                ),
                row(
                    [
                        button("btn_inspect_tracks", "Inspect Tracks", variant="primary", icon="search"),
                    ],
                    gap=2,
                ),
                divider(),
            ]
        )

        # Discovered embedded tracks list
        if self.local_tracks:
            items: list[dict[str, Any]] = []
            for track in self.local_tracks:
                title_line = f"Track #{track.index} [{track.language.upper()}] - {track.codec_name}"
                sub_line = track.title or f"Embedded subtitle stream #{track.subtitle_index}"
                items.append(
                    list_item(
                        id=f"track_item_{track.index}",
                        title=title_line,
                        subtitle=sub_line,
                        badge=f"Stream #{track.index}",
                        actions=[
                            {"id": f"action_extract_{track.index}", "label": "Extract to .srt", "variant": "primary"},
                        ],
                    )
                )

            children.extend(
                [
                    heading(f"Embedded Subtitle Tracks ({len(self.local_tracks)})", level=3),
                    list_widget("local_tracks_list", items=items),
                    divider(),
                ]
            )
        elif self.local_video_path and not self.local_tracks:
            children.extend(
                [
                    text("No subtitle tracks detected in container.", variant="muted"),
                    divider(),
                ]
            )

        # Extraction result & Flashcards Handoff
        if self.extracted_subtitle_path:
            is_dry = bool(self.last_extraction_result and self.last_extraction_result.dry_run)
            children.extend(
                [
                    heading(
                        "Extracted Subtitle Ready" if not is_dry else "[Dry-run] Extraction Command Generated",
                        level=3,
                    ),
                    file_output(self.extracted_subtitle_path, label="Subtitle File (.srt)"),
                    row(
                        [
                            button("btn_use_in_flashcards_local", "Use in Flashcards", variant="primary"),
                        ],
                        gap=2,
                    ),
                    *self._render_cli_revision_section(),
                ]
            )

        return column(children, gap=3, id="local_screen", expertOnly=True)

    # Screen: Settings

    def _render_settings_screen(self, values: dict[str, Any] | None = None) -> dict[str, Any]:
        from subtitles_plugin.config import mask_secret

        creds = self.config_manager.resolve_credentials()
        settings = self.config_manager.settings

        children: list[dict[str, Any]] = []
        children.append(
            password_input(
                "cfg_api_key",
                label="OpenSubtitles API Key",
                defaultValue=creds.api_key or "",
                emptyWarning="Enter an OpenSubtitles Consumer API key.",
                submitAction="btn_save_settings",
                link={"label": "Where do I find this key?", "url": "https://www.opensubtitles.com/en/consumers"},
            )
        )

        saved_keys = self._saved_keys()

        if saved_keys:
            key_items: list[dict[str, Any]] = []
            for idx, k in enumerate(saved_keys):
                is_active = k == (settings.opensubtitles_api_key or creds.api_key)
                actions: list[dict[str, Any]] = []
                if not is_active:
                    actions.append({"id": f"action_use_key_{idx}", "label": "Use", "variant": "primary"})
                actions.append({"id": f"action_delete_key_{idx}", "label": "Remove", "icon": "trash", "iconOnly": True, "variant": "danger"})
                key_items.append(
                    list_item(
                        id=f"saved_key_{idx}",
                        title=mask_secret(k),
                        badge="Active" if is_active else None,
                        actions=actions,
                    )
                )
            children.append(list_widget("saved_keys_list", items=key_items))

        children.extend(
            [
                row(
                    [
                        language_select(
                            "cfg_preferred_languages",
                            label="Preferred Language",
                            names="both",
                            defaultValue=settings.preferred_languages or "en",
                            flex=1,
                        ),
                        path_input(
                            "cfg_default_download_dir",
                            label="Default Download Directory",
                            defaultValue=settings.default_download_dir,
                            placeholder="Default: downloads folder",
                            picker="directory",
                            mode="directory",
                            flex=2,
                        ),
                    ],
                    gap=3,
                ),
                row(
                    [
                        text_input(
                            "cfg_username",
                            label="Username (optional)",
                            placeholder="OpenSubtitles username",
                            defaultValue=settings.opensubtitles_username or (creds.username or ""),
                            flex=1,
                            expertOnly=True,
                        ),
                        password_input(
                            "cfg_password",
                            label="Password (optional)",
                            placeholder="OpenSubtitles password",
                            defaultValue=settings.opensubtitles_password or (creds.password or ""),
                            flex=1,
                            expertOnly=True,
                        ),
                    ],
                    gap=3,
                    expertOnly=True,
                ),
                row(
                    [
                        text_input(
                            "cfg_ffmpeg_path",
                            label="Custom FFmpeg Path (optional)",
                            defaultValue=settings.ffmpeg_path,
                            placeholder="ffmpeg",
                            flex=1,
                            expertOnly=True,
                        ),
                        text_input(
                            "cfg_ffprobe_path",
                            label="Custom ffprobe Path (optional)",
                            defaultValue=settings.ffprobe_path,
                            placeholder="ffprobe",
                            flex=1,
                            expertOnly=True,
                        ),
                    ],
                    gap=3,
                    expertOnly=True,
                ),
                checkbox(
                    "cfg_dry_run_default",
                    label="Dry-run mode (simulate extraction and downloads)",
                    defaultValue=settings.dry_run_default,
                    expertOnly=True,
                ),
                divider(),
                row(
                    [
                        button(
                            "btn_save_settings", "Save Settings", variant="primary", icon="save",
                            requiresAny=["cfg_api_key"], disabledFocus="cfg_api_key",
                        ),
                        button(
                            "btn_test_credentials", "Test API Connection", variant="secondary", icon="refresh",
                            requiresAny=["cfg_api_key"], disabledFocus="cfg_api_key",
                        ),
                    ],
                    gap=2,
                ),
                divider(expertOnly=True),
                heading("Local Video Track Extraction", level=3, expertOnly=True),
                row(
                    [
                        path_input(
                            "local_video_path",
                            label="Video File Path",
                            placeholder="/path/to/movie.mkv",
                            defaultValue=self.local_video_path,
                            picker="file",
                            mode="file",
                            flex=3,
                            expertOnly=True,
                        ),
                        button(
                            "btn_inspect_tracks",
                            "Inspect Tracks",
                            variant="secondary",
                            icon="search",
                            expertOnly=True,
                        ),
                    ],
                    gap=3,
                    align="end",
                    expertOnly=True,
                ),
            ]
        )

        if self.local_tracks:
            track_items: list[dict[str, Any]] = []
            for track in self.local_tracks:
                title_line = f"Track #{track.index} [{track.language.upper()}] - {track.codec_name}"
                sub_line = track.title or f"Stream #{track.subtitle_index}"
                track_items.append(
                    list_item(
                        id=f"track_item_{track.index}",
                        title=title_line,
                        subtitle=sub_line,
                        badge=f"#{track.index}",
                        actions=[
                            {"id": f"action_extract_{track.index}", "label": "Extract .srt", "variant": "primary"},
                        ],
                    )
                )
            children.append(list_widget("local_tracks_list", items=track_items, expertOnly=True))

        if self.extracted_subtitle_path:
            children.extend(
                [
                    file_output(self.extracted_subtitle_path, label="Extracted Subtitle (.srt)", expertOnly=True),
                    button("btn_use_in_flashcards_local", "Use in Flashcards", variant="primary", expertOnly=True),
                ]
            )

        return column(children, gap=3, id="settings_screen")

    # Event Handling

    def handle_event(
        self,
        screen: str,
        widget_id: str,
        event_type: str,
        values: dict[str, Any],
    ) -> dict[str, Any]:
        destination = {
            "btn_nav_search": "main",
            "btn_nav_local": "local",
            "btn_nav_settings": "settings",
        }.get(widget_id)
        if destination:
            self.status_message = None
            return self._screen_response(destination, values, screen=destination)

        if event_type == "submit" and widget_id in (
            "search_query",
            "search_tmdb_id",
            "search_imdb_id",
            "search_season",
            "search_episode",
        ):
            widget_id = "btn_do_search"

        actions = {
            "search_mode": self._handle_search_mode_change,
            "search_media_type": self._handle_search_media_type_change,
            "btn_do_search": self._handle_search_event,
            "btn_clear_search": self._clear_search,
            "btn_download_now": self._handle_download_event,
            "btn_use_in_flashcards": self._handle_flashcards_event,
            "btn_inspect_tracks": self._handle_inspect_tracks,
            "btn_use_in_flashcards_local": self._handle_flashcards_local_event,
            "btn_save_settings": self._handle_save_settings,
            "btn_test_credentials": self._handle_test_credentials,
            "btn_cli_inspect": self._handle_cli_inspect,
            "btn_cli_sync": self._handle_cli_sync,
            "results_pagination": self._handle_pagination_event,
        }
        if handler := actions.get(widget_id):
            return handler(values)

        for prefix, handler in (
            ("action_select_", self._handle_select_subtitle),
            ("action_extract_", self._handle_extract_track),
            ("action_use_key_", self._use_key),
            ("action_delete_key_", self._remove_key),
        ):
            if widget_id.startswith(prefix):
                return handler(widget_id.removeprefix(prefix), values)
        return {}

    def _sync_search_inputs_from_values(self, values: dict[str, Any]) -> None:
        if "search_languages" in values:
            self.search_languages = str(values.get("search_languages") or "").strip()
        if "search_query" in values:
            self.search_query = str(values.get("search_query") or "").strip()
        if "search_tmdb_id" in values:
            self.search_tmdb_id = str(values.get("search_tmdb_id") or "").strip()
        if "search_imdb_id" in values:
            self.search_imdb_id = str(values.get("search_imdb_id") or "").strip()
        if "search_season" in values:
            self.search_season = str(values.get("search_season") or "").strip()
        if "search_episode" in values:
            self.search_episode = str(values.get("search_episode") or "").strip()

    def _handle_search_mode_change(self, values: dict[str, Any]) -> dict[str, Any]:
        self._sync_search_inputs_from_values(values)
        raw_mode = str(values.get("search_mode") or self.search_mode).strip().lower()
        if raw_mode in ("title", "tmdb", "imdb"):
            self.search_mode = raw_mode
        self.status_message = None
        return self._screen_response("search", values)

    def _handle_search_media_type_change(self, values: dict[str, Any]) -> dict[str, Any]:
        self._sync_search_inputs_from_values(values)
        raw_type = str(values.get("search_media_type") or self.search_media_type).strip().lower()
        if raw_type in ("all", "movie", "tvshow"):
            self.search_media_type = raw_type
        self.status_message = None
        extra: dict[str, Any] = {}
        if self.search_media_type != "tvshow":
            self.search_season = ""
            self.search_episode = ""
            extra["patch"] = {"values": {"search_season": "", "search_episode": ""}}
        return self._screen_response("search", values, **extra)

    def _clear_search(self, values: dict[str, Any]) -> dict[str, Any]:
        self.search_query = self.search_tmdb_id = self.search_imdb_id = ""
        self.search_season = self.search_episode = ""
        self.search_results = []
        self.selected_subtitle = None
        self.current_page = 1
        self.last_search_executed = False
        self.status_message = None
        return self._screen_response(
            "search",
            values,
            patch={
                "values": {
                    "search_query": "",
                    "search_tmdb_id": "",
                    "search_imdb_id": "",
                    "search_season": "",
                    "search_episode": "",
                    "download_custom_name": "",
                }
            },
        )

    def _saved_keys(self) -> list[str]:
        settings = self.config_manager.settings
        keys = list(settings.saved_api_keys)
        if settings.opensubtitles_api_key and settings.opensubtitles_api_key not in keys:
            keys.insert(0, settings.opensubtitles_api_key)
        return keys

    def _use_key(self, index: str, values: dict[str, Any]) -> dict[str, Any]:
        return self._change_saved_key(index, values, remove=False)

    def _remove_key(self, index: str, values: dict[str, Any]) -> dict[str, Any]:
        return self._change_saved_key(index, values, remove=True)

    def _change_saved_key(
        self, index: str, values: dict[str, Any], *, remove: bool
    ) -> dict[str, Any]:
        keys = self._saved_keys()
        try:
            idx = int(index)
        except ValueError:
            return {}
        if not 0 <= idx < len(keys):
            return {}

        settings = self.config_manager.settings
        old_key, old_keys = settings.opensubtitles_api_key, settings.saved_api_keys
        selected = keys[idx]
        if remove:
            keys = [key for key in keys if key != selected]
            new_key = (keys[0] if keys else "") if selected == old_key else old_key
        else:
            new_key = selected

        settings.opensubtitles_api_key = new_key
        settings.saved_api_keys = keys
        if not self.config_manager.save_settings():
            settings.opensubtitles_api_key, settings.saved_api_keys = old_key, old_keys
            return {"snackbar": {"message": "Could not save API key changes.", "type": "error"}}

        self.status_message = None
        response = {
            "root": self.render_screen("settings"),
            "snackbar": {
                "message": "Removed saved API key." if remove else "Switched active API key.",
                "type": "success",
            },
        }
        if not remove or selected == old_key or values.get("cfg_api_key") == selected:
            response["patch"] = {"values": {"cfg_api_key": new_key}}
        return response

    def _handle_search_event(self, values: dict[str, Any]) -> dict[str, Any]:
        self._sync_search_inputs_from_values(values)

        # Resolve active search mode (respect explicit widget value; infer for headless callers)
        if "search_mode" in values and str(values["search_mode"]).strip().lower() in ("title", "tmdb", "imdb"):
            self.search_mode = str(values["search_mode"]).strip().lower()
        elif "search_mode" not in values:
            if str(values.get("search_query", "")).strip():
                self.search_mode = "title"
            elif str(values.get("search_tmdb_id", "")).strip():
                self.search_mode = "tmdb"
            elif str(values.get("search_imdb_id", "")).strip():
                self.search_mode = "imdb"

        if "search_media_type" in values and str(values["search_media_type"]).strip().lower() in ("all", "movie", "tvshow"):
            self.search_media_type = str(values["search_media_type"]).strip().lower()
        elif "search_media_type" not in values and (self.search_season or self.search_episode):
            self.search_media_type = "tvshow"

        # Strictly use ONLY the active mode's identifier so multiple inputs never conflict
        active_query = self.search_query if self.search_mode == "title" else ""
        active_tmdb = self.search_tmdb_id if self.search_mode == "tmdb" else ""
        active_imdb = self.search_imdb_id if self.search_mode == "imdb" else ""

        if not (active_query or active_tmdb or active_imdb):
            mode_label = {"title": "title", "tmdb": "TMDB ID", "imdb": "IMDb ID"}[self.search_mode]
            self.status_message = f"Please enter a {mode_label} to search."
            self.status_variant = "danger"
            self.notify(self.status_message, level="warn")
            return self._screen_response("search", values)

        # Validate & parse IDs and Season/Episode before calling API
        try:
            parsed_tmdb, url_media = parse_tmdb_id(active_tmdb) if active_tmdb else (None, None)
            parsed_imdb = parse_imdb_id(active_imdb) if active_imdb else None
            season_num = (
                parse_season_episode_value(self.search_season, kind="season")
                if self.search_media_type == "tvshow" and self.search_season
                else None
            )
            episode_num = (
                parse_season_episode_value(self.search_episode, kind="episode")
                if self.search_media_type == "tvshow" and self.search_episode
                else None
            )
        except ValueError as e:
            self.status_message = str(e)
            self.status_variant = "danger"
            self.notify(self.status_message, level="warn")
            return self._screen_response("search", values)

        effective_media_type = self.search_media_type
        if effective_media_type == "all" and url_media:
            effective_media_type = url_media
            self.search_media_type = url_media

        self.report_progress("search_op", 25.0, "Searching OpenSubtitles...")
        self.log(
            f"Searching OpenSubtitles: mode={self.search_mode}, type={effective_media_type}, "
            f"query='{active_query}', tmdb='{parsed_tmdb or ''}', imdb='{parsed_imdb or ''}', "
            f"season={season_num}, episode={episode_num}, languages='{self.search_languages}'",
            level="info",
        )

        extra: dict[str, Any] = {}
        try:
            results = self.opensubtitles_client.search(
                query=active_query,
                tmdb_id=parsed_tmdb,
                imdb_id=parsed_imdb,
                languages=self.search_languages or None,
                exclude_machine_and_ai=True,
                media_type=effective_media_type,
                season_number=season_num,
                episode_number=episode_num,
            )
            self.last_search_executed = True
            self.search_results = results
            self.current_page = 1
            self.selected_subtitle = results[0] if results else None
            self.status_message = None
            self.status_variant = "normal"
            if self.selected_subtitle is not None:
                extra["patch"] = {
                    "values": {
                        "download_custom_name": self.selected_subtitle.primary_file_name,
                    }
                }
            self.log(f"OpenSubtitles search completed: {len(results)} result(s) found", level="info")
            self.report_progress("search_op", 100.0, "Search completed")
        except Exception as e:
            self.last_search_executed = True
            self.search_results = []
            self.current_page = 1
            self.selected_subtitle = None
            self.status_message = f"Search failed: {e}"
            self.status_variant = "danger"
            self.notify(self.status_message, level="error")

        return self._screen_response("search", values, **extra)

    def _handle_pagination_event(self, values: dict[str, Any]) -> dict[str, Any]:
        total_items = len(self.search_results)
        total_pages = max(1, (total_items + self.PAGE_SIZE - 1) // self.PAGE_SIZE)
        raw_page = values.get("results_pagination", self.current_page)
        try:
            requested = int(raw_page)
        except (TypeError, ValueError):
            requested = self.current_page
        self.current_page = max(1, min(requested, total_pages))
        return self._screen_response("search", values, scrollTop=True)

    def _handle_select_subtitle(self, sub_id: str, values: dict[str, Any]) -> dict[str, Any]:
        match = next((s for s in self.search_results if s.id == sub_id), None)
        extra: dict[str, Any] = {}
        if match:
            self.selected_subtitle = match
            self.status_message = f"Selected '{match.primary_file_name}'"
            self.status_variant = "normal"
            extra["patch"] = {
                "values": {
                    "download_custom_name": match.primary_file_name,
                }
            }
        return self._screen_response("search", values, **extra)

    def _handle_download_event(self, values: dict[str, Any]) -> dict[str, Any]:
        if not self.selected_subtitle:
            self.status_message = "No subtitle selected for download."
            self.status_variant = "danger"
            return self._screen_response("search", values)

        fid = self.selected_subtitle.primary_file_id
        if fid is None:
            self.status_message = "Selected subtitle does not have an associated file ID."
            self.status_variant = "danger"
            self.notify(self.status_message, level="error")
            return self._screen_response("search", values)

        dest_dir = values.get("download_dest_dir") or str(self.config_manager.resolve_download_dir())
        custom_name = values.get("download_custom_name") or self.selected_subtitle.primary_file_name

        self.report_progress("dl_op", 30.0, f"Downloading {custom_name}...")
        try:
            is_dry = self.config_manager.resolve_dry_run()
            self.cli_inspection_summary = None
            self.cli_preview_rows = []
            if is_dry:
                out_path = Path(dest_dir).resolve() / custom_name
                self.downloaded_subtitle_path = str(out_path)
                self.status_message = f"[Dry-run] Simulated download of {custom_name} to {out_path}."
                self.status_variant = "success"
                self.notify(self.status_message, level="info")
            else:
                out_path = self.opensubtitles_client.download_subtitle(
                    file_id=fid,
                    destination_dir=dest_dir,
                    custom_filename=custom_name,
                )
                self.downloaded_subtitle_path = str(out_path)
                self.status_message = f"Downloaded subtitle saved to {out_path}."
                self.status_variant = "success"
                self.notify(self.status_message, level="success")

            self.report_progress("dl_op", 100.0, "Download complete")
        except Exception as e:
            self.status_message = f"Download failed: {e}"
            self.status_variant = "danger"
            self.notify(self.status_message, level="error")

        return self._screen_response("search", values)

    def _handle_flashcards_event(self, values: dict[str, Any]) -> dict[str, Any]:
        vid_path = str(values.get("video_for_flashcards") or self.associated_video_path or "").strip()
        sub_path = str(self.downloaded_subtitle_path or "").strip()

        if not sub_path:
            self.status_message = "No subtitle file available to load into Flashcards."
            self.status_variant = "danger"
            self.notify(self.status_message, level="warn")
            return self._screen_response("search", values)

        self.associated_video_path = vid_path
        self.open_in_flashcards(video_path=vid_path, subtitle_path=sub_path)
        self.status_message = f"Emitted vesta.openInFlashcards with {Path(sub_path).name}"
        self.status_variant = "success"
        self.notify("Loaded subtitle and video into Vesta Flashcards tab!", level="success")

        # Emit live patch update for instant feedback
        patch = make_patch(
            set_props={
                "status_banner": {"text": self.status_message, "variant": "success", "hidden": False}
            }
        )
        self.emit_ui_update(screen="search", patch=patch)
        return {"patch": patch}

    def _handle_inspect_tracks(self, values: dict[str, Any]) -> dict[str, Any]:
        target_screen = self.current_screen if self.current_screen in ("settings", "local") else "local"
        raw_path = str(values.get("local_video_path") or "").strip()
        if not raw_path:
            self.status_message = "Please provide an absolute path to a video file."
            self.status_variant = "danger"
            self.notify(self.status_message, level="warn")
            return {"root": self.render_screen(target_screen, values)}

        self.local_video_path = raw_path
        self.report_progress("probe_op", 20.0, "Inspecting subtitle streams...")

        try:
            tracks = self.extractor.inspect_video_tracks(self.local_video_path)
            self.local_tracks = tracks
            self.extracted_subtitle_path = None
            self.status_message = f"Found {len(tracks)} embedded subtitle track(s)."
            self.status_variant = "success"
            self.notify(self.status_message, level="info")
            self.report_progress("probe_op", 100.0, "Inspection complete")
        except Exception as e:
            self.local_tracks = []
            self.status_message = f"Inspection error: {e}"
            self.status_variant = "danger"
            self.notify(self.status_message, level="error")

        return {"root": self.render_screen(target_screen, values)}

    def _handle_extract_track(self, track_idx_str: str, values: dict[str, Any]) -> dict[str, Any]:
        target_screen = self.current_screen if self.current_screen in ("settings", "local") else "local"
        try:
            stream_idx = int(track_idx_str)
        except ValueError:
            self.status_message = f"Invalid track index: {track_idx_str}"
            self.status_variant = "danger"
            return {"root": self.render_screen(target_screen, values)}

        self.report_progress("extract_op", 30.0, f"Extracting track #{stream_idx}...")
        res = self.extractor.extract_track(
            video_path=self.local_video_path,
            stream_index=stream_idx,
            output_dir=self.config_manager.resolve_download_dir(),
        )
        self.last_extraction_result = res

        if res.success:
            self.extracted_subtitle_path = res.output_path
            self.cli_inspection_summary = None
            self.cli_preview_rows = []
            self.status_message = res.message
            self.status_variant = "success"
            self.notify(res.message, level="success")
            self.report_progress("extract_op", 100.0, "Extraction complete")
        else:
            self.status_message = res.message
            self.status_variant = "danger"
            self.notify(res.message, level="error")

        return {"root": self.render_screen(target_screen, values)}

    def _handle_flashcards_local_event(self, values: dict[str, Any]) -> dict[str, Any]:
        target_screen = self.current_screen if self.current_screen in ("settings", "local") else "local"
        vid_path = str(self.local_video_path or values.get("local_video_path", "")).strip()
        sub_path = str(self.extracted_subtitle_path or "").strip()

        if not sub_path:
            self.status_message = "No extracted subtitle track available."
            self.status_variant = "danger"
            self.notify(self.status_message, level="warn")
            return {"root": self.render_screen(target_screen, values)}

        self.open_in_flashcards(video_path=vid_path, subtitle_path=sub_path)
        self.status_message = f"Loaded extracted subtitle into Flashcards ({Path(sub_path).name})"
        self.status_variant = "success"
        self.notify("Loaded extracted track into Vesta Flashcards tab!", level="success")

        patch = make_patch(
            set_props={
                "status_banner": {"text": self.status_message, "variant": "success", "hidden": False}
            }
        )
        self.emit_ui_update(screen=target_screen, patch=patch)
        return {"patch": patch}

    def _handle_save_settings(self, values: dict[str, Any]) -> dict[str, Any]:
        from subtitles_plugin.opensubtitles import looks_like_tmdb_key_or_token

        api_key = str(values.get("cfg_api_key") or "").strip()
        if not api_key:
            return {"snackbar": {"message": "Enter an OpenSubtitles Consumer API key.", "type": "warning"}}

        tmdb_warning = looks_like_tmdb_key_or_token(api_key)
        if tmdb_warning:
            self.log(f"API key format warning on save: {tmdb_warning}", level="warn")

        updates = {
            "opensubtitles_api_key": api_key,
            "opensubtitles_username": values.get("cfg_username", ""),
            "opensubtitles_password": values.get("cfg_password", ""),
            "preferred_languages": values.get("cfg_preferred_languages", "en"),
            "default_download_dir": values.get("cfg_default_download_dir", ""),
            "dry_run_default": values.get("cfg_dry_run_default", False),
            "ffmpeg_path": values.get("cfg_ffmpeg_path", ""),
            "ffprobe_path": values.get("cfg_ffprobe_path", ""),
        }
        self.config_manager.update_settings(updates, save=True)

        self.status_message = None
        root = self.render_screen("settings")
        if tmdb_warning:
            root["snackbar"] = {"message": f"Settings saved, but warning: {tmdb_warning}", "type": "warning"}
        else:
            root["snackbar"] = {"message": "Settings saved successfully.", "type": "success"}
        return {"root": root}

    def _handle_test_credentials(self, values: dict[str, Any]) -> dict[str, Any]:
        # Temporarily use UI values for testing if provided
        test_key = values.get("cfg_api_key", self.config_manager.settings.opensubtitles_api_key)
        test_user = values.get("cfg_username", self.config_manager.settings.opensubtitles_username)
        test_pass = values.get("cfg_password", self.config_manager.settings.opensubtitles_password)

        from subtitles_plugin.config import OpenSubtitlesCredentials

        temp_creds = OpenSubtitlesCredentials(
            api_key=str(test_key).strip() or None,
            username=str(test_user).strip() or None,
            password=str(test_pass).strip() or None,
            source="settings_test",
        )
        temp_client = self.create_client(temp_creds)

        ok, msg = temp_client.test_connection()
        self.status_message = msg
        self.status_variant = "success" if ok else "danger"
        self.notify(msg, level="success" if ok else "error")

        return {"root": self.render_screen("settings", values)}

    def _active_subtitle_path(self) -> tuple[str, str]:
        """Returns `(target_screen, subtitle_path)` for the active screen."""
        if self.current_screen in ("local", "settings") and self.extracted_subtitle_path:
            return self.current_screen, self.extracted_subtitle_path
        if self.downloaded_subtitle_path:
            return "search", self.downloaded_subtitle_path
        if self.extracted_subtitle_path:
            return "local", self.extracted_subtitle_path
        return "search", ""

    @staticmethod
    def _parse_srt_cues(content: str) -> list[tuple[str, str, str]]:
        """Parses raw `.srt` text into a list of `(index, timing, text)` tuples."""
        cues: list[tuple[str, str, str]] = []
        blocks = [b.strip() for b in content.replace("\r\n", "\n").split("\n\n") if b.strip()]
        for block in blocks:
            lines = [ln.strip() for ln in block.split("\n") if ln.strip()]
            if len(lines) >= 2 and "-->" in lines[1]:
                idx = lines[0]
                timing = lines[1]
                body = " ".join(lines[2:]) if len(lines) > 2 else ""
                cues.append((idx, timing, body))
            elif len(lines) >= 1 and "-->" in lines[0]:
                timing = lines[0]
                body = " ".join(lines[1:]) if len(lines) > 1 else ""
                cues.append((str(len(cues) + 1), timing, body))
        return cues

    def _handle_cli_inspect(self, values: dict[str, Any]) -> dict[str, Any]:
        target_screen, sub_path = self._active_subtitle_path()
        if not sub_path:
            self.status_message = "No subtitle file available to inspect."
            self.status_variant = "danger"
            self.notify(self.status_message, level="warn")
            return {"root": self.render_screen(target_screen, values)}

        self.report_progress("cli_inspect_op", 25.0, "Inspecting subtitle via vesta-cli...")
        cli_res = self.run_cli("extract", ["--input", sub_path, "--json"])

        cues: list[tuple[str, str, str]] = []
        p = Path(sub_path)
        if p.is_file():
            try:
                cues = self._parse_srt_cues(p.read_text(encoding="utf-8", errors="replace"))
            except Exception:
                cues = []

        if not cues and isinstance(cli_res.get("stdout"), str) and "-->" in cli_res["stdout"]:
            cues = self._parse_srt_cues(cli_res["stdout"])

        cue_count = len(cues)
        if isinstance(cli_res.get("json"), dict) and "cues" in cli_res["json"]:
            raw_cues = cli_res["json"]["cues"]
            if isinstance(raw_cues, list) and not cues:
                cue_count = len(raw_cues)
                for idx, item in enumerate(raw_cues[:8], start=1):
                    if isinstance(item, dict):
                        start = str(item.get("start", item.get("start_ms", "")))
                        end = str(item.get("end", item.get("end_ms", "")))
                        txt = str(item.get("text", ""))
                        cues.append((str(idx), f"{start} --> {end}", txt))

        self.cli_preview_rows = [[idx, timing, txt] for idx, timing, txt in cues[:8]]
        span_info = ""
        if cues:
            first_start = cues[0][1].split("-->")[0].strip()
            last_end = cues[-1][1].split("-->")[-1].strip()
            span_info = f" • {first_start} → {last_end}"

        exit_code = cli_res.get("exitCode", 0) if isinstance(cli_res, dict) else 0
        self.cli_inspection_summary = (
            f"Vesta CLI Inspection (exit={exit_code}): {cue_count} cue(s) in {p.name}{span_info}"
        )
        self.status_message = self.cli_inspection_summary
        self.status_variant = "success"
        self.notify(self.cli_inspection_summary, level="success")
        self.report_progress("cli_inspect_op", 100.0, "CLI inspection complete")
        return self._screen_response(target_screen, values)

    def _handle_cli_sync(self, values: dict[str, Any]) -> dict[str, Any]:
        target_screen, sub_path = self._active_subtitle_path()
        if not sub_path:
            self.status_message = "No subtitle file available to retime."
            self.status_variant = "danger"
            self.notify(self.status_message, level="warn")
            return {"root": self.render_screen(target_screen, values)}

        raw_shift = values.get("cli_shift_ms", self.cli_shift_ms)
        try:
            offset_ms = int(float(raw_shift or 0))
        except (TypeError, ValueError):
            offset_ms = 0
        self.cli_shift_ms = offset_ms

        if offset_ms == 0:
            self.status_message = "Enter a non-zero time shift in milliseconds (e.g. 500 or -350)."
            self.status_variant = "danger"
            self.notify(self.status_message, level="warn")
            return {"root": self.render_screen(target_screen, values)}

        src = Path(sub_path)
        sign = f"+{offset_ms}ms" if offset_ms > 0 else f"{offset_ms}ms"
        stem = src.stem
        out_path = src.with_name(f"{stem}.shifted_{sign}.srt")

        self.report_progress("cli_sync_op", 30.0, f"Shifting timestamps by {sign} via vesta-cli sync...")
        cli_res = self.run_cli(
            "sync",
            [
                "--input",
                str(src),
                "--output",
                str(out_path),
                "--offset-ms",
                str(offset_ms),
            ],
        )

        success = bool(isinstance(cli_res, dict) and cli_res.get("success", True))
        if not success:
            stderr = cli_res.get("stderr") if isinstance(cli_res, dict) else "CLI error"
            self.status_message = f"vesta-cli sync failed: {stderr}"
            self.status_variant = "danger"
            self.notify(self.status_message, level="error")
            return {"root": self.render_screen(target_screen, values)}

        if target_screen == "search" or self.downloaded_subtitle_path == sub_path:
            self.downloaded_subtitle_path = str(out_path)
        else:
            self.extracted_subtitle_path = str(out_path)

        self.cli_inspection_summary = (
            f"Retimed via vesta-cli sync ({sign}) → {out_path.name}"
        )
        self.status_message = self.cli_inspection_summary
        self.status_variant = "success"
        self.notify(self.cli_inspection_summary, level="success")
        self.report_progress("cli_sync_op", 100.0, "Subtitle retimed")
        return self._screen_response(target_screen, values)
