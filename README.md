<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
# Subtitles

Subtitles is a first-party subtitle search, inspection, retiming, and extraction plugin for [Vesta](https://github.com/pierspad/vesta). It discovers and downloads human-authored subtitles from OpenSubtitles, extracts embedded tracks from local video containers via FFmpeg, inspects and retimes `.srt` subtitles through the native `vesta.cli` bridge, and integrates directly with Vesta Flashcards and Synchronize.

## Install in Vesta

In Vesta, open **Plugins** and enter `https://github.com/pierspad/subtitles-veplugin`
as the repository URL. Review the permissions shown by Vesta (`network`, `filesystem:downloads`, `vesta:cli`) and confirm
installation. Vesta can check for newer stable releases from the Plugins tab.

For local development, clone this repository, open **Plugins → Local Folder**,
and select the folder containing `vesta-plugin.json`. Use **Update** on the
plugin card to copy later changes from that folder.

## Getting an OpenSubtitles API Key

1. Create a free account at [OpenSubtitles.com](https://www.opensubtitles.com).
2. Request a Consumer API key at [opensubtitles.com/en/consumers](https://www.opensubtitles.com/en/consumers).
3. Enter your Consumer API Key in the plugin Settings view.

## Downloads and Vesta workflows

Use **Downloads**, to the left of **Settings**, to view up to 200 recent downloads, including files from previous sessions. Selecting another search result does not change the saved file or its language. Each saved file has actions to copy its path, open it, or open its containing folder.

In **Settings**, turn off **Open Downloads after saving a subtitle** to keep the
search results and continue downloading. The preference is saved between sessions;
by default each successful download opens Downloads.

Each history entry has a **Remove** action. Confirm to remove the entry only, or
select **Also delete the SRT file from disk** to delete that subtitle too.
The file option starts unchecked; associated videos are always kept. Dry-run mode
keeps both the history and files.

Choose a **Matching video (optional)** from the same film or episode. Flashcards uses it for audio, images and clips; Synchronize uses it to preview and adjust timing. Leave it empty for subtitle-only work. The field includes a tooltip explaining both uses.

- **Use in Flashcards** opens the selected download and optional video. The subtitle language comes from the downloaded result's OpenSubtitles metadata, independently of later searches or ambiguous filename markers such as `HI` (hearing impaired).
- **Use in Synchronize** opens the same files in Vesta's timing workspace. Vesta asks before replacing an existing session and waits while synchronization is busy. This requires a Vesta build supporting `vesta.openInSynchronize`.

The Flashcards and Synchronize actions use their workspace colors (amber and cyan)
in Vesta builds supporting the corresponding button variants. Older hosts display
the actions with their default styling.

## Advanced subtitle tools (`vesta.cli`)

In **Expert Mode**, the Downloads page also exposes the **Subtitle Revision & Retiming (Vesta CLI)** panel:
- **Inspect Cues (`vesta-cli extract`)**: Calls `vesta.cli` (`extract --input <file> --json`) to inspect cue count, timing span, and preview the first cues in a declarative `table` widget.
- **Apply Time Shift (`vesta-cli sync`)**: Shifts all subtitle timestamps by any positive or negative millisecond offset via `vesta.cli` (`sync --input <file> --output <shifted.srt> --offset-ms <ms>`) before handing the file off to Flashcards via `vesta.openInFlashcards`.

## Normal vs. Expert Mode

- **Normal Mode**: Single-mode subtitle search by Title, TMDB ID, or IMDb ID (with dedicated TV Series season/episode filters and `S01E01` auto-detection), preferred language selection, and downloads to a local folder.
- **Expert Mode**: Toggle Expert Mode in Vesta's sidebar to access local video container extraction (FFmpeg/ffprobe), custom executable paths, optional user login for higher download quotas, and dry-run execution.

## Disclaimer

OpenSubtitles does not endorse or maintain this plugin. Users are responsible for obtaining their own API keys, managing download quotas, and complying with all applicable copyright laws and provider terms.
