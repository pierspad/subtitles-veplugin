<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
# Subtitles

Subtitles is a first-party subtitle search, inspection, retiming, and extraction plugin for [Vesta](https://github.com/pierspad/vesta). It discovers and downloads human-authored subtitles from OpenSubtitles, extracts embedded tracks from local video containers via FFmpeg, inspects and retimes `.srt` subtitles through the native `vesta.cli` bridge, and integrates directly with Vesta Flashcards.

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

## Subtitle Revision & Retiming (`vesta.cli`)

Once a subtitle is downloaded or extracted, the plugin displays the **Subtitle Revision & Retiming (Vesta CLI)** panel:
- **Inspect Cues (`vesta-cli extract`)**: Calls `vesta.cli` (`extract --input <file> --json`) to inspect cue count, timing span, and preview the first cues in a declarative `table` widget.
- **Apply Time Shift (`vesta-cli sync`)**: Shifts all subtitle timestamps by any positive or negative millisecond offset via `vesta.cli` (`sync --input <file> --output <shifted.srt> --offset-ms <ms>`) before handing the file off to Flashcards via `vesta.openInFlashcards`.

## Normal vs. Expert Mode

- **Normal Mode**: Subtitle search by title or TMDB/IMDb IDs, preferred language selection, and downloads to a local folder.
- **Expert Mode**: Toggle Expert Mode in Vesta's sidebar to access local video container extraction (FFmpeg/ffprobe), custom executable paths, optional user login for higher download quotas, and dry-run execution.

## Disclaimer

OpenSubtitles does not endorse or maintain this plugin. Users are responsible for obtaining their own API keys, managing download quotas, and complying with all applicable copyright laws and provider terms.
