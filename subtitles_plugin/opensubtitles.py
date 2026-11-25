# SPDX-License-Identifier: GPL-3.0-or-later
"""OpenSubtitles.com REST API v1 Client.

Independent client interacting with OpenSubtitles.com REST API v1:
- Subtitle search by text query, TMDB ID, or IMDb ID with language filtering.
- Strict quality filtering: excludes machine-translated and AI-translated subtitles.
- Prioritizes verified, human-authored subtitles from trusted sources.
- Subtitle file download workflow via POST /api/v1/download.
- Optional user authentication for increased download quotas.
- Zero external runtime dependencies (urllib.request standard library).
"""

from __future__ import annotations

import json
import re
import shutil
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from subtitles_plugin.config import OpenSubtitlesCredentials

BASE_URL = "https://api.opensubtitles.com/api/v1"
USER_AGENT = "subtitles-veplugin v1.0.0"


@dataclass
class SubtitleFile:
    """Individual subtitle file inside an OpenSubtitles item."""

    file_id: int
    cd_number: int = 1
    file_name: str = ""


@dataclass
class SubtitleItem:
    """Normalized subtitle metadata returned by OpenSubtitles search."""

    id: str
    subtitle_id: str
    language: str
    download_count: int
    new_download_count: int
    hearing_impaired: bool
    hd: bool
    fps: float | None
    ratings: float | None
    votes: int
    from_trusted: bool
    foreign_parts_only: bool
    ai_translated: bool
    machine_translated: bool
    release: str
    comments: str
    movie_name: str
    feature_id: int | None = None
    year: int | None = None
    imdb_id: int | None = None
    tmdb_id: int | None = None
    poster_url: str | None = None
    files: list[SubtitleFile] = field(default_factory=list)
    feature_type: str | None = None
    parent_title: str = ""
    episode_title: str = ""
    season_number: int | None = None
    episode_number: int | None = None
    parent_imdb_id: int | None = None
    parent_tmdb_id: int | None = None
    parent_feature_id: int | None = None

    @property
    def primary_file_id(self) -> int | None:
        if self.files:
            return self.files[0].file_id
        return None

    @property
    def primary_file_name(self) -> str:
        if self.files and self.files[0].file_name:
            return self.files[0].file_name
        if self.release:
            return f"{self.release}.srt"
        return f"subtitle_{self.id}.srt"

    @property
    def is_human_made(self) -> bool:
        return not (self.machine_translated or self.ai_translated)

    @property
    def is_episode(self) -> bool:
        return (
            (self.feature_type or "").strip().lower() == "episode"
            or self.season_number is not None
            or self.episode_number is not None
            or bool(self.parent_title.strip())
        )

    @property
    def episode_code(self) -> str:
        if self.season_number is not None and self.episode_number is not None:
            return f"S{self.season_number:02d}E{self.episode_number:02d}"
        if self.season_number is not None:
            return f"S{self.season_number:02d}"
        if self.episode_number is not None:
            return f"E{self.episode_number:02d}"
        return ""

    @property
    def clean_series_title(self) -> str:
        if self.parent_title.strip():
            return _strip_wrapping_quotes(self.parent_title)
        raw = self.movie_name.strip()
        # Strip leading "2011 - " release-year prefix before parsing series name
        raw = re.sub(r"^\d{4}\s*-\s*", "", raw).strip()
        if raw.startswith('"') and '"' in raw[1:]:
            end_quote = raw.find('"', 1)
            candidate = raw[1:end_quote].strip()
            if candidate:
                return candidate
        if " - " in raw:
            prefix = raw.split(" - ", 1)[0].strip()
            if prefix:
                return _strip_wrapping_quotes(prefix)
        return _strip_wrapping_quotes(raw)

    @property
    def display_title(self) -> str:
        if self.is_episode:
            series = self.clean_series_title
            ep = _strip_wrapping_quotes(self.episode_title)
            if not ep and self.movie_name:
                raw = re.sub(r"^\d{4}\s*-\s*", "", self.movie_name.strip()).strip()
                if raw.startswith('"') and '"' in raw[1:]:
                    ep = raw[raw.find('"', 1) + 1 :].strip(" -:")
                elif series and raw.lower().startswith(series.lower()):
                    remainder = raw[len(series) :].strip(" -:")
                    # Strip leading S00E45 / 1x01 code if already in badge
                    _, _, stripped_ep = extract_episode_from_text(remainder)
                    ep = stripped_ep or remainder
            if series and ep and ep.lower() != series.lower():
                return f"{series} — {ep}"
            return series or ep or self.primary_file_name
        return self.movie_name or self.primary_file_name

    @property
    def badge_text(self) -> str:
        badges = []
        if self.episode_code:
            badges.append(self.episode_code)
        if self.from_trusted:
            badges.append("TRUSTED")
        if self.hearing_impaired:
            badges.append("HI")
        if self.hd:
            badges.append("HD")
        return " | ".join(badges)


def _strip_wrapping_quotes(text: str) -> str:
    cleaned = (text or "").strip()
    if len(cleaned) >= 2 and ((cleaned[0] == '"' and cleaned[-1] == '"') or (cleaned[0] == "'" and cleaned[-1] == "'")):
        return cleaned[1:-1].strip()
    return cleaned


def _parse_optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _parse_poster_url(related_links: Any) -> str | None:
    """Extract and sanitize a poster `img_url` from OpenSubtitles `related_links`."""
    if isinstance(related_links, dict):
        candidates = [related_links]
    elif isinstance(related_links, list):
        candidates = [entry for entry in related_links if isinstance(entry, dict)]
    else:
        return None

    for entry in candidates:
        raw = entry.get("img_url")
        if not isinstance(raw, str):
            continue
        url = raw.strip()
        if not url:
            continue
        if url.lower().startswith("http://"):
            url = "https://" + url[7:]
        return url
    return None


def _normalize_tokens(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", (text or "").lower())


def _clean_movie_tokens(movie_name: str) -> list[str]:
    tokens = _normalize_tokens(movie_name)
    # Strip leading or trailing 4-digit release years (e.g. "1994 - Pulp Fiction" or "Inception (2010)")
    while len(tokens) > 1 and len(tokens[0]) == 4 and tokens[0].isdigit():
        tokens = tokens[1:]
    while len(tokens) > 1 and len(tokens[-1]) == 4 and tokens[-1].isdigit():
        tokens = tokens[:-1]
    return tokens


_EPISODE_PATTERNS = (
    # S01E02, s1e2, S01.E02, S01 E02, S01-E02
    re.compile(r"(?i)\bS(?P<season>\d{1,3})[.\s_-]?E(?P<episode>\d{1,3})\b"),
    # 1x02, 01x02
    re.compile(r"(?i)\b(?P<season>\d{1,2})x(?P<episode>\d{1,3})\b"),
    # Season 1 Episode 2 / Season 1 Ep 2
    re.compile(r"(?i)\bSeason\s+(?P<season>\d{1,3})(?:\s+(?:Episode|Ep\.?)\s+(?P<episode>\d{1,3}))?\b"),
)
_SEASON_ONLY_PATTERN = re.compile(r"(?i)\bS(?P<season>\d{1,2})\b")


def extract_episode_from_text(text: str) -> tuple[int | None, int | None, str]:
    """Extract `(season, episode, cleaned_text)` from a title or release string."""
    raw = (text or "").strip()
    if not raw:
        return None, None, ""

    for pattern in _EPISODE_PATTERNS:
        match = pattern.search(raw)
        if match:
            season = int(match.group("season"))
            ep_raw = match.groupdict().get("episode")
            episode = int(ep_raw) if ep_raw is not None else None
            cleaned = (raw[: match.start()] + " " + raw[match.end() :]).strip(" -_:.")
            cleaned = re.sub(r"\s{2,}", " ", cleaned).strip()
            return season, episode, cleaned

    # Only strip standalone S01 if there are other words in the query (avoid mangling single-token titles)
    if len(raw.split()) > 1:
        match = _SEASON_ONLY_PATTERN.search(raw)
        if match:
            season = int(match.group("season"))
            cleaned = (raw[: match.start()] + " " + raw[match.end() :]).strip(" -_:.")
            cleaned = re.sub(r"\s{2,}", " ", cleaned).strip()
            return season, None, cleaned

    return None, None, raw


def parse_tmdb_id(raw: int | str | None) -> tuple[int | None, str | None]:
    """Normalize a TMDB ID or themoviedb.org URL into `(numeric_id, inferred_media_type)`."""
    if raw is None:
        return None, None
    cleaned = str(raw).strip()
    if not cleaned:
        return None, None
    if cleaned.isdigit():
        val = int(cleaned)
        if val <= 0:
            raise ValueError("Invalid TMDB ID. Must be a positive integer (e.g. 27205 or 1396).")
        return val, None

    url_match = re.search(r"themoviedb\.org/(tv|movie)/(\d+)", cleaned, flags=re.IGNORECASE)
    if url_match:
        kind = url_match.group(1).lower()
        return int(url_match.group(2)), ("tvshow" if kind == "tv" else "movie")

    prefix_match = re.match(r"^(tv|movie)[/:](\d+)$", cleaned, flags=re.IGNORECASE)
    if prefix_match:
        kind = prefix_match.group(1).lower()
        return int(prefix_match.group(2)), ("tvshow" if kind == "tv" else "movie")

    raise ValueError("Invalid TMDB ID. Enter a numeric ID (e.g. 27205) or a themoviedb.org URL.")


def parse_imdb_id(raw: int | str | None) -> int | None:
    """Normalize an IMDb ID (`tt1375666`, `1375666`, or imdb.com URL) into an integer ID."""
    if raw is None:
        return None
    cleaned = str(raw).strip()
    if not cleaned:
        return None

    tt_match = re.search(r"(?i)\btt(\d+)\b", cleaned)
    if tt_match:
        val = int(tt_match.group(1))
        if val <= 0:
            raise ValueError("Invalid IMDb ID.")
        return val

    if cleaned.isdigit():
        val = int(cleaned)
        if val <= 0:
            raise ValueError("Invalid IMDb ID.")
        return val

    raise ValueError("Invalid IMDb ID. Enter an ID like tt1375666 (or an imdb.com title URL).")


def parse_season_episode_value(raw: int | str | None, *, kind: str) -> int | None:
    """Parse an optional Season or Episode input (accepts '1', '01', 'S01', 'E05')."""
    if raw is None:
        return None
    cleaned = str(raw).strip()
    if not cleaned:
        return None
    prefix_char = "s" if kind == "season" else "e"
    if cleaned.lower().startswith(prefix_char) and cleaned[1:].isdigit():
        cleaned = cleaned[1:]
    if not cleaned.isdigit():
        label = "Season" if kind == "season" else "Episode"
        raise ValueError(f"Invalid {label} number '{raw}'. Enter a whole number (e.g. 1).")
    return int(cleaned)


def compute_query_relevance(item: SubtitleItem, query: str) -> int:
    """Computes a title/release relevance tier (0..4) for `item` against `query`.

    - 4: Clean movie/series title exactly matches the query phrase.
    - 3: Clean movie/series title contains the exact query phrase as a contiguous sequence.
    - 2: All query words appear in the title, or the exact query phrase appears in the release name.
    - 1: All query words appear across the combined title + release name.
    - 0: One or more query words are missing (partial/noise match).
    """
    q_tokens = _normalize_tokens(query)
    if not q_tokens:
        return 4
    q_phrase = " ".join(q_tokens)

    candidate_titles = [item.movie_name]
    if item.parent_title:
        candidate_titles.append(item.parent_title)
        if item.episode_title:
            candidate_titles.append(f"{item.parent_title} {item.episode_title}")
    if item.episode_title:
        candidate_titles.append(item.episode_title)

    release_tokens = _normalize_tokens(item.release or item.primary_file_name)
    release_phrase = " ".join(release_tokens)

    best_tier = 0
    combined_title_tokens: set[str] = set()
    for candidate in candidate_titles:
        if not candidate:
            continue
        title_tokens = _clean_movie_tokens(candidate)
        combined_title_tokens.update(title_tokens)
        title_phrase = " ".join(title_tokens)
        if title_phrase == q_phrase:
            return 4
        if q_phrase and f" {q_phrase} " in f" {title_phrase} ":
            best_tier = max(best_tier, 3)
        elif all(t in set(title_tokens) for t in q_tokens):
            best_tier = max(best_tier, 2)

    if q_phrase and f" {q_phrase} " in f" {release_phrase} ":
        best_tier = max(best_tier, 2)
    if best_tier >= 2:
        return best_tier

    combined_set = combined_title_tokens | set(release_tokens)
    if all(t in combined_set for t in q_tokens):
        return 1
    return 0


def looks_like_tmdb_key_or_token(api_key: str | None) -> str | None:
    """Detects if the user accidentally pasted a TMDB v3 API Key (32-char lowercase hex)
    or a TMDB v4 Read Access Token (JWT starting with 'eyJ') into the OpenSubtitles API Key field.
    """
    if not api_key:
        return None
    cleaned = api_key.strip()
    if cleaned.startswith("eyJ") and cleaned.count(".") >= 2:
        return (
            "The configured key looks like a JWT Read Access Token (e.g. from TMDB). "
            "OpenSubtitles requires a Consumer API Key from https://www.opensubtitles.com/en/consumers."
        )
    if len(cleaned) == 32 and all(c in "0123456789abcdef" for c in cleaned):
        return (
            "The configured key is a 32-character hexadecimal string (typical of a TMDB v3 API Key). "
            "OpenSubtitles requires an OpenSubtitles.com Consumer API Key from https://www.opensubtitles.com/en/consumers."
        )
    return None


class OpenSubtitlesClient:
    """Client for OpenSubtitles.com REST API v1."""

    def __init__(
        self,
        credentials: OpenSubtitlesCredentials | None = None,
        base_url: str = BASE_URL,
        user_agent: str = USER_AGENT,
        timeout: int = 15,
    ):
        self.credentials = credentials or OpenSubtitlesCredentials(None, None, None, "none")
        self.base_url = base_url.rstrip("/")
        self.user_agent = user_agent
        self.timeout = timeout
        self.auth_token: str | None = None

    def _get_headers(self, requires_auth: bool = False) -> dict[str, str]:
        headers: dict[str, str] = {
            "User-Agent": self.user_agent,
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        if self.credentials.api_key:
            headers["Api-Key"] = self.credentials.api_key.strip()
        if self.auth_token:
            headers["Authorization"] = f"Bearer {self.auth_token}"
        return headers

    def _request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = f"{self.base_url}/{path.lstrip('/')}"
        if params:
            query_str = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None and v != ""})
            if query_str:
                url = f"{url}?{query_str}"

        data_bytes = None
        if body is not None:
            data_bytes = json.dumps(body).encode("utf-8")

        req = urllib.request.Request(
            url=url,
            data=data_bytes,
            headers=self._get_headers(),
            method=method.upper(),
        )

        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                resp_bytes = resp.read()
                if not resp_bytes:
                    return {}
                return json.loads(resp_bytes.decode("utf-8"))
        except urllib.error.HTTPError as e:
            raw_err = e.read().decode("utf-8", errors="replace")
            try:
                err_json = json.loads(raw_err)
                msg = err_json.get("message") or err_json.get("error") or raw_err
            except Exception:
                msg = raw_err or str(e)
            if e.code == 403 and "cannot consume this service" in str(msg).lower():
                hint = looks_like_tmdb_key_or_token(self.credentials.api_key)
                if hint:
                    msg = f"{msg} — {hint}"
                else:
                    msg = (
                        f"{msg} — Verify that your key is an active OpenSubtitles.com Consumer API Key "
                        "from https://www.opensubtitles.com/en/consumers (not a TMDB API Key or Read Access Token)."
                    )
            raise RuntimeError(f"OpenSubtitles API error (HTTP {e.code}): {msg}") from e
        except urllib.error.URLError as e:
            raise RuntimeError(f"OpenSubtitles connection failed: {e.reason}") from e

    def login(self, username: str | None = None, password: str | None = None) -> str:
        """Authenticates with OpenSubtitles to obtain a session bearer token."""
        user = username or (self.credentials.username if self.credentials else None)
        pwd = password or (self.credentials.password if self.credentials else None)

        if not user or not pwd:
            raise ValueError("Username and password are required for login.")

        payload = {"username": user, "password": pwd}
        res = self._request("POST", "/login", body=payload)
        token = res.get("token")
        if not token:
            raise RuntimeError("Login succeeded but no auth token returned.")
        self.auth_token = str(token)
        return self.auth_token

    def _parse_subtitle_items(
        self,
        raw_items: list[dict[str, Any]],
        *,
        exclude_machine_and_ai: bool,
    ) -> list[SubtitleItem]:
        parsed: list[SubtitleItem] = []
        for item in raw_items:
            attrs = item.get("attributes") or {}
            is_machine = bool(attrs.get("machine_translated", False))
            is_ai = bool(attrs.get("ai_translated", False))

            if exclude_machine_and_ai and (is_machine or is_ai):
                continue

            files_list: list[SubtitleFile] = []
            for f in attrs.get("files") or []:
                fid = f.get("file_id")
                if fid is not None:
                    files_list.append(
                        SubtitleFile(
                            file_id=int(fid),
                            cd_number=int(f.get("cd_number", 1)),
                            file_name=str(f.get("file_name", "")),
                        )
                    )

            feat = attrs.get("feature_details") or {}
            if not isinstance(feat, dict):
                feat = {}
            feature_type = str(feat.get("feature_type") or "").strip() or None
            parent_title = str(feat.get("parent_title") or "").strip()
            ep_title = str(feat.get("title") or "").strip()
            movie_title = feat.get("movie_name") or ep_title or ""
            season_num = _parse_optional_int(feat.get("season_number"))
            episode_num = _parse_optional_int(feat.get("episode_number"))
            release_str = str(attrs.get("release", "")).strip()

            if (feature_type or "").lower() != "movie" and (season_num is None or episode_num is None):
                fallback_src = f"{movie_title} {release_str}".strip()
                inferred_s, inferred_e, _ = extract_episode_from_text(fallback_src)
                if season_num is None and inferred_s is not None:
                    season_num = inferred_s
                if episode_num is None and inferred_e is not None:
                    episode_num = inferred_e

            subtitle = SubtitleItem(
                id=str(item.get("id", attrs.get("subtitle_id", ""))),
                subtitle_id=str(attrs.get("subtitle_id", item.get("id", ""))),
                language=str(attrs.get("language", "")).lower(),
                download_count=int(attrs.get("download_count", 0)),
                new_download_count=int(attrs.get("new_download_count", 0)),
                hearing_impaired=bool(attrs.get("hearing_impaired", False)),
                hd=bool(attrs.get("hd", False)),
                fps=float(attrs["fps"]) if attrs.get("fps") is not None else None,
                ratings=float(attrs["ratings"]) if attrs.get("ratings") is not None else None,
                votes=int(attrs.get("votes", 0)),
                from_trusted=bool(attrs.get("from_trusted", False)),
                foreign_parts_only=bool(attrs.get("foreign_parts_only", False)),
                ai_translated=is_ai,
                machine_translated=is_machine,
                release=release_str,
                comments=str(attrs.get("comments", "")).strip(),
                movie_name=str(movie_title),
                feature_id=_parse_optional_int(feat.get("feature_id")),
                year=_parse_optional_int(feat.get("year")),
                imdb_id=_parse_optional_int(feat.get("imdb_id")),
                tmdb_id=_parse_optional_int(feat.get("tmdb_id")),
                poster_url=_parse_poster_url(attrs.get("related_links")),
                files=files_list,
                feature_type=feature_type,
                parent_title=parent_title,
                episode_title=ep_title if (feature_type or "").lower() == "episode" or parent_title else "",
                season_number=season_num,
                episode_number=episode_num,
                parent_imdb_id=_parse_optional_int(feat.get("parent_imdb_id")),
                parent_tmdb_id=_parse_optional_int(feat.get("parent_tmdb_id")),
                parent_feature_id=_parse_optional_int(feat.get("parent_feature_id")),
            )
            parsed.append(subtitle)
        return parsed

    @staticmethod
    def _episode_match_score(
        item: SubtitleItem,
        season_number: int | None,
        episode_number: int | None,
    ) -> int:
        if season_number is None and episode_number is None:
            return 2
        if season_number is not None and item.season_number is not None and item.season_number != season_number:
            return 0
        if episode_number is not None and item.episode_number is not None and item.episode_number != episode_number:
            return 0
        season_ok = season_number is None or item.season_number == season_number
        episode_ok = episode_number is None or item.episode_number == episode_number
        if season_ok and episode_ok:
            return 2
        return 1

    def search(
        self,
        query: str = "",
        tmdb_id: int | str | None = None,
        imdb_id: int | str | None = None,
        languages: str | list[str] | None = None,
        hearing_impaired: str | None = None,  # 'include', 'exclude', 'only'
        exclude_machine_and_ai: bool = True,
        trusted_sources_only: bool = False,
        media_type: str | None = None,  # 'all', 'movie', 'tvshow', 'episode'
        season_number: int | None = None,
        episode_number: int | None = None,
    ) -> list[SubtitleItem]:
        """Search subtitles on OpenSubtitles.com.

        Excludes machine and AI translations and prioritizes human-made subtitles.
        Supports movies and TV series (parent IDs, season/episode numbers, and S01E01 query parsing).
        """
        params: dict[str, Any] = {}
        norm_media = (media_type or "all").strip().lower()
        clean_query = (query or "").strip()

        parsed_tmdb, url_media = parse_tmdb_id(tmdb_id)
        parsed_imdb = parse_imdb_id(imdb_id)
        if norm_media == "all" and url_media:
            norm_media = url_media

        # Extract embedded S01E01 / 1x01 / Season X Episode Y from text queries unless explicitly movie-only
        if clean_query and norm_media != "movie" and (season_number is None or episode_number is None):
            ext_season, ext_episode, stripped_query = extract_episode_from_text(clean_query)
            if ext_season is not None or ext_episode is not None:
                if season_number is None:
                    season_number = ext_season
                if episode_number is None:
                    episode_number = ext_episode
                if stripped_query:
                    clean_query = stripped_query
                if norm_media == "all":
                    norm_media = "tvshow"

        is_tv_search = norm_media in ("tvshow", "episode") or season_number is not None or episode_number is not None

        if clean_query:
            params["query"] = clean_query

        if is_tv_search:
            params["type"] = "episode"
            if parsed_tmdb is not None:
                params["parent_tmdb_id"] = parsed_tmdb
            if parsed_imdb is not None:
                params["parent_imdb_id"] = parsed_imdb
        else:
            if norm_media == "movie":
                params["type"] = "movie"
            if parsed_tmdb is not None:
                params["tmdb_id"] = parsed_tmdb
            if parsed_imdb is not None:
                params["imdb_id"] = parsed_imdb

        if season_number is not None:
            params["season_number"] = int(season_number)
        if episode_number is not None:
            params["episode_number"] = int(episode_number)

        if languages:
            if isinstance(languages, list):
                params["languages"] = ",".join(languages)
            else:
                params["languages"] = str(languages).strip()

        if hearing_impaired:
            params["hearing_impaired"] = hearing_impaired

        if trusted_sources_only:
            params["trusted_sources"] = "only"

        # Ask API to exclude machine and AI translated subtitles
        if exclude_machine_and_ai:
            params["machine_translated"] = "exclude"
            params["ai_translated"] = "exclude"

        params["order_by"] = "download_count"
        params["order_direction"] = "desc"

        res = self._request("GET", "/subtitles", params=params)
        parsed = self._parse_subtitle_items(res.get("data") or [], exclude_machine_and_ai=exclude_machine_and_ai)

        # Smart fallback for single-ID searches:
        # 1) In 'all' mode, a bare IMDb/TMDB ID might belong to a TV series (parent_*_id),
        #    or `imdb_id`/`tmdb_id` may only return Season 0 specials indexed on the show ID.
        # 2) In 'tvshow' mode without season/episode, a user may have pasted an individual episode ID.
        single_id_only = not clean_query and (bool(parsed_tmdb is not None) != bool(parsed_imdb is not None))
        only_specials = bool(parsed) and all(s.is_episode and s.season_number == 0 for s in parsed)
        if (not parsed or (not is_tv_search and only_specials)) and single_id_only:
            fallback_params = dict(params)
            if not is_tv_search:
                fallback_params["type"] = "episode"
                if "tmdb_id" in fallback_params:
                    fallback_params["parent_tmdb_id"] = fallback_params.pop("tmdb_id")
                if "imdb_id" in fallback_params:
                    fallback_params["parent_imdb_id"] = fallback_params.pop("imdb_id")
                res2 = self._request("GET", "/subtitles", params=fallback_params)
                extra_parsed = self._parse_subtitle_items(res2.get("data") or [], exclude_machine_and_ai=exclude_machine_and_ai)
                if extra_parsed:
                    seen_ids = {s.id for s in parsed}
                    merged = list(extra_parsed) + [s for s in parsed if s.id not in seen_ids]
                    parsed = merged
            elif season_number is None and episode_number is None:
                if "parent_tmdb_id" in fallback_params:
                    fallback_params["tmdb_id"] = fallback_params.pop("parent_tmdb_id")
                if "parent_imdb_id" in fallback_params:
                    fallback_params["imdb_id"] = fallback_params.pop("parent_imdb_id")
                res2 = self._request("GET", "/subtitles", params=fallback_params)
                parsed = self._parse_subtitle_items(res2.get("data") or [], exclude_machine_and_ai=exclude_machine_and_ai)

        # Enforce exact season/episode filtering when matching results exist
        if season_number is not None or episode_number is not None:
            ep_scored = [(self._episode_match_score(s, season_number, episode_number), s) for s in parsed]
            if any(score >= 1 for score, _ in ep_scored):
                parsed = [s for score, s in ep_scored if score >= 1]

        # Filter out partial-word noise when full-query matches exist, and rank results
        if clean_query:
            scored = [(compute_query_relevance(s, clean_query), s) for s in parsed]
            if any(rel >= 1 for rel, _ in scored):
                scored = [(rel, s) for rel, s in scored if rel >= 1]
            if is_tv_search and episode_number is None:
                scored.sort(
                    key=lambda pair: (
                        -pair[0],
                        0 if pair[1].season_number is not None else 1,
                        pair[1].season_number if pair[1].season_number is not None else 0,
                        0 if pair[1].episode_number is not None else 1,
                        pair[1].episode_number if pair[1].episode_number is not None else 0,
                        -(1 if pair[1].is_human_made else 0),
                        -(1 if pair[1].from_trusted else 0),
                        -pair[1].download_count,
                        -(pair[1].ratings or 0.0),
                    )
                )
            else:
                scored.sort(
                    key=lambda pair: (
                        pair[0],
                        self._episode_match_score(pair[1], season_number, episode_number),
                        1 if pair[1].is_human_made else 0,
                        1 if pair[1].from_trusted else 0,
                        pair[1].download_count,
                        pair[1].ratings or 0.0,
                    ),
                    reverse=True,
                )
            return [s for _, s in scored]

        if is_tv_search and episode_number is None:
            parsed.sort(
                key=lambda s: (
                    0 if s.season_number is not None else 1,
                    s.season_number if s.season_number is not None else 0,
                    0 if s.episode_number is not None else 1,
                    s.episode_number if s.episode_number is not None else 0,
                    -(1 if s.is_human_made else 0),
                    -(1 if s.from_trusted else 0),
                    -s.download_count,
                    -(s.ratings or 0.0),
                )
            )
            return parsed

        parsed.sort(
            key=lambda s: (
                self._episode_match_score(s, season_number, episode_number),
                1 if s.from_trusted else 0,
                1 if s.is_human_made else 0,
                s.download_count,
                s.ratings or 0.0,
            ),
            reverse=True,
        )

        return parsed

    def get_download_link(self, file_id: int) -> dict[str, Any]:
        """Requests a temporary subtitle download link for a file_id."""
        payload = {"file_id": file_id}
        return self._request("POST", "/download", body=payload)

    def download_subtitle(
        self,
        file_id: int,
        destination_dir: str | Path,
        custom_filename: str | None = None,
    ) -> Path:
        """Downloads a subtitle file given its file_id and writes it to destination_dir."""
        dest_dir = Path(destination_dir).resolve()
        dest_dir.mkdir(parents=True, exist_ok=True)

        info = self.get_download_link(file_id)
        download_url = info.get("link")
        if not download_url:
            raise RuntimeError("OpenSubtitles download response did not provide a download link.")

        file_name = custom_filename or info.get("file_name") or f"sub_{file_id}.srt"
        if not isinstance(file_name, str) or file_name in {".", ".."} or any(
            char in file_name for char in ("/", "\\", ":", "\x00")
        ):
            raise ValueError("Invalid subtitle filename.")
        if not file_name.lower().endswith((".srt", ".vtt")):
            file_name = f"{file_name}.srt"

        out_path = dest_dir / file_name

        req = urllib.request.Request(
            download_url,
            headers={"User-Agent": self.user_agent},
        )
        temporary = None
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                with tempfile.NamedTemporaryFile(dir=dest_dir, delete=False) as output:
                    temporary = Path(output.name)
                    shutil.copyfileobj(resp, output)
            temporary.replace(out_path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        return out_path

    def test_connection(self) -> tuple[bool, str]:
        """Tests the OpenSubtitles API key and connection."""
        if not self.credentials.api_key:
            return False, "OpenSubtitles API key is not configured."

        try:
            # If user auth is configured and not yet logged in, attempt login
            if self.credentials.has_user_auth and not self.auth_token:
                try:
                    self.login()
                except Exception as e:
                    return False, f"API key valid but user login failed: {e}"

            # Make a lightweight query to verify API key
            res = self._request("GET", "/subtitles", params={"query": "test", "languages": "en"})
            total = res.get("total_count", 0)
            user_suffix = " (authenticated user)" if self.auth_token else " (consumer key)"
            return True, f"Connected to OpenSubtitles successfully{user_suffix}. Found {total} test matches."
        except Exception as e:
            return False, f"Connection test failed: {e}"
