#!/usr/bin/env python3
"""
core — shared brain for the media assistant.

Holds all the TMDB/Sonarr/Radarr tools, the Groq tool-calling loop, and
process_request(). Both the Telegram bot (bot.py) and the web chat frontend
(web.py) import from here so they share identical behaviour.

No Telegram dependency lives in this module.
"""

import json
import logging
import os
import re
import time
from pathlib import Path

import httpx
from groq import Groq

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────

GROQ_API_KEY = os.environ["GROQ_API_KEY"]
# ADMIN_ID is only used by the Telegram frontend (first allowed user / admin
# commands). Optional for a web-only deploy.
ADMIN_ID     = int(os.environ.get("ADMIN_ID", "0"))
TMDB_TOKEN   = os.environ["TMDB_TOKEN"]

SONARR_URL = os.environ.get("SONARR_URL", "http://sonarr:8989")
SONARR_KEY = os.environ["SONARR_KEY"]
RADARR_URL = os.environ.get("RADARR_URL", "http://radarr:7878")
RADARR_KEY = os.environ["RADARR_KEY"]

# Optional approval gate: when on, non-admin "add" requests are queued and the
# admin is pinged on Telegram with Approve/Deny buttons instead of adding right away.
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "")
REQUIRE_APPROVAL = os.environ.get("REQUIRE_APPROVAL", "").strip().lower() in ("1", "true", "yes", "on")
PENDING_FILE     = Path("/data/pending.json")

GROQ_MODEL  = "llama-3.3-70b-versatile"
TMDB_BASE   = "https://api.themoviedb.org/3"
DATA_FILE   = Path("/data/users.json")
SESSION_TTL = 600  # 10 minutes of inactivity clears history

# Keyword IDs that trigger a genre-specific keyword discover (AND logic = higher precision)
# Each tuple: (set of required keyword IDs, discover param string)
_KW_GENRE_GROUPS = [
    ({9715, 9717}, "9715,9717"),   # superhero AND based on comic
    ({9715, 33637}, "9715,33637"), # superhero AND super power
]

# ── User store ────────────────────────────────────────────────────────────────

def load_users() -> set:
    if DATA_FILE.exists():
        return set(json.loads(DATA_FILE.read_text()))
    return {ADMIN_ID}

def save_users(users: set):
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    DATA_FILE.write_text(json.dumps(list(users)))

ALLOWED_USERS: set = load_users()

def is_allowed(user_id: int) -> bool:
    return user_id == ADMIN_ID or user_id in ALLOWED_USERS

# ── API helpers ───────────────────────────────────────────────────────────────

def arr_get(base: str, key: str, path: str, params: dict = None):
    r = httpx.get(f"{base}/api/v3{path}", params={"apikey": key, **(params or {})}, timeout=10)
    r.raise_for_status()
    return r.json()

def arr_post(base: str, key: str, path: str, body: dict):
    r = httpx.post(f"{base}/api/v3{path}", params={"apikey": key}, json=body, timeout=10)
    r.raise_for_status()
    return r.json()

def tmdb_get(path: str, params: dict = None):
    r = httpx.get(
        f"{TMDB_BASE}{path}",
        headers={"Authorization": f"Bearer {TMDB_TOKEN}"},
        params=params or {},
        timeout=10
    )
    r.raise_for_status()
    return r.json()

_cache: dict = {}

def cached(key: str, fn):
    if key not in _cache:
        _cache[key] = fn()
    return _cache[key]

_MEDIA_WORDS = re.compile(
    r'\b(movies?|films?|tv\s*shows?|shows?|series|episodes?|filmography)\b', re.I
)

def _person_query(q: str) -> str:
    return _MEDIA_WORDS.sub('', q).strip(' ,')

# Strip connective / recommendation noise so the LLM can pass a whole phrase
# (e.g. "movies like Star Wars") and we still search TMDB for just the title.
_NOISE_WORDS = re.compile(
    r'\b(similar(?:\s+to)?|recommendations?|recommend|suggestions?|suggest|'
    r'something\s+like|stuff\s+like|like|'
    r'movies?|films?|tv\s*shows?|shows?|series|episodes?|filmography)\b', re.I
)

def _clean_query(q: str) -> str:
    """Reduce a phrase like 'movies like Star Wars' to its core title 'Star Wars'."""
    return re.sub(r'\s{2,}', ' ', _NOISE_WORDS.sub('', q)).strip(' ,')

# Permissive: grab function name + JSON blob, ignore any separator or closing tag variant
_MALFORMED_CALL = re.compile(r'<function=(\w+)[=\[\]\s]*(\{.+?\})', re.DOTALL)

def _recover_malformed(error_str: str):
    """Parse a Groq tool_use_failed error and return (func_name, args_dict) or (None, None)."""
    m = _MALFORMED_CALL.search(error_str)
    if m:
        try:
            return m.group(1), json.loads(m.group(2))
        except (json.JSONDecodeError, ValueError):
            pass
    return None, None

def _english_only(items: list) -> list:
    """Filter TMDB results to English original language only."""
    return [r for r in items if r.get("original_language", "en") == "en"]

# ── Tool implementations ──────────────────────────────────────────────────────

def search_movie(query: str) -> str:
    try:
        results = arr_get(RADARR_URL, RADARR_KEY, "/movie/lookup", {"term": query})
        if not results:
            return "No movies found."
        return json.dumps([
            {"tmdb_id": r["tmdbId"], "title": r["title"], "year": r.get("year", "?")}
            for r in results[:5]
        ])
    except Exception as e:
        return f"Radarr unreachable: {e}"

def add_movie(tmdb_id, title: str) -> str:
    try:
        tmdb_id = int(tmdb_id)
    except (TypeError, ValueError):
        tmdb_id = 0
    try:
        # Resolve/verify the TMDB id by title — the LLM sometimes passes a guessed
        # id (e.g. the year) on a cold "add X" with no prior search. If the given id
        # isn't among the title's lookup results, trust the best title match instead.
        lookup = arr_get(RADARR_URL, RADARR_KEY, "/movie/lookup", {"term": title})
        if lookup and not any(r.get("tmdbId") == tmdb_id for r in lookup):
            tmdb_id = lookup[0].get("tmdbId", tmdb_id)
            title = lookup[0].get("title", title)
        existing = arr_get(RADARR_URL, RADARR_KEY, "/movie")
        match = next((m for m in existing if m["tmdbId"] == tmdb_id), None)
        if match:
            if match.get("hasFile"):
                return f"ALREADY_HAVE: '{title}' is already downloaded and in your library."
            arr_post(RADARR_URL, RADARR_KEY, "/command", {
                "name": "MoviesSearch", "movieIds": [match["id"]]
            })
            return f"SEARCH_TRIGGERED: '{title}' was in Radarr but not downloaded — triggered a fresh search."
        qp = cached("radarr_qp", lambda: arr_get(RADARR_URL, RADARR_KEY, "/qualityprofile")[0]["id"])
        rf = cached("radarr_rf", lambda: arr_get(RADARR_URL, RADARR_KEY, "/rootfolder")[0]["path"])
        arr_post(RADARR_URL, RADARR_KEY, "/movie", {
            "tmdbId": tmdb_id, "title": title,
            "qualityProfileId": qp, "rootFolderPath": rf,
            "monitored": True, "addOptions": {"searchForMovie": True}
        })
        return f"ADDED: '{title}' added to Radarr and searching now."
    except Exception as e:
        return f"ERROR: {e}"

def search_tv(query: str) -> str:
    try:
        results = arr_get(SONARR_URL, SONARR_KEY, "/series/lookup", {"term": query})
        if not results:
            return "No TV shows found."
        return json.dumps([
            {
                "tmdb_id": r.get("tmdbId"),
                "title": r["title"],
                "year": r.get("year", "?"),
                "season_count": len([s for s in r.get("seasons", []) if s.get("seasonNumber", 0) > 0])
            }
            for r in results[:5]
        ])
    except Exception as e:
        return f"Sonarr unreachable: {e}"

def add_tv(tmdb_id, title: str, seasons: str = "all") -> str:
    """
    Add a TV show to Sonarr.
    seasons: "all" | "first" | "latest" | comma-separated numbers e.g. "1,2,3"

    Note: only "all" uses Sonarr's addOptions.monitor shortcut — "first", "latest",
    and specific numbers set the seasons array directly (Sonarr v4 doesn't accept
    those strings in addOptions.monitor).
    """
    try:
        tmdb_id = int(tmdb_id)
    except (TypeError, ValueError):
        tmdb_id = 0
    try:
        # Look up the full series object by title first (also needed for the seasons
        # array on POST). This corrects a guessed tmdb_id from a cold "add X" — prefer
        # an exact id match, else fall back to the best title match.
        lookup = arr_get(SONARR_URL, SONARR_KEY, "/series/lookup", {"term": title})
        if not lookup:
            return f"ERROR: Could not find '{title}' in Sonarr's database."
        series_data = next((r for r in lookup if r.get("tmdbId") == tmdb_id), lookup[0])
        tmdb_id = series_data.get("tmdbId", tmdb_id)
        title = series_data.get("title", title)

        # Check existing library by the resolved TMDB id
        existing = arr_get(SONARR_URL, SONARR_KEY, "/series")
        match = next((s for s in existing if s.get("tmdbId") == tmdb_id), None)
        if match:
            stats = match.get("statistics", {})
            if stats.get("episodeFileCount", 0) > 0:
                pct = stats.get("percentOfEpisodes", 0)
                return f"ALREADY_HAVE: '{title}' is already in your library ({pct:.0f}% downloaded)."
            arr_post(SONARR_URL, SONARR_KEY, "/command", {
                "name": "SeriesSearch", "seriesId": match["id"]
            })
            return f"SEARCH_TRIGGERED: '{title}' was in Sonarr but not downloaded — triggered a fresh search."

        qp = cached("sonarr_qp", lambda: arr_get(SONARR_URL, SONARR_KEY, "/qualityprofile")[0]["id"])
        rf = cached("sonarr_rf", lambda: arr_get(SONARR_URL, SONARR_KEY, "/rootfolder")[0]["path"])

        seasons_str = (seasons or "all").strip().lower()
        all_nums = sorted(s["seasonNumber"] for s in series_data.get("seasons", []) if s.get("seasonNumber", 0) > 0)

        if seasons_str == "all":
            # Use Sonarr's built-in monitor="all" — the only shortcut string Sonarr v4 accepts reliably
            series_data.update({
                "qualityProfileId": qp, "rootFolderPath": rf,
                "monitored": True, "seasonFolder": True,
                "addOptions": {"searchForMissingEpisodes": True, "monitor": "all"}
            })
            added_msg = f"ADDED: '{title}' added to Sonarr — downloading all seasons."
        else:
            # Determine which season numbers to monitor
            if seasons_str == "first":
                wanted = {all_nums[0]} if all_nums else set()
                label = "Season 1 only"
            elif seasons_str == "latest":
                wanted = {all_nums[-1]} if all_nums else set()
                label = f"Season {all_nums[-1]} only" if all_nums else "latest season"
            else:
                # Comma-separated numbers e.g. "1,2,3"
                try:
                    wanted = {int(s.strip()) for s in seasons_str.split(",") if s.strip()}
                except ValueError:
                    wanted = set(all_nums)
                label = ", ".join(f"Season {s}" for s in sorted(wanted))

            # Set seasons array manually — omit addOptions.monitor so Sonarr respects the array
            for s in series_data.get("seasons", []):
                s["monitored"] = s.get("seasonNumber", 0) in wanted

            series_data.update({
                "qualityProfileId": qp, "rootFolderPath": rf,
                "monitored": True, "seasonFolder": True,
                "addOptions": {"searchForMissingEpisodes": True}
            })
            added_msg = f"ADDED: '{title}' added to Sonarr — downloading {label}."

        arr_post(SONARR_URL, SONARR_KEY, "/series", series_data)
        return added_msg
    except Exception as e:
        return f"ERROR: {e}"

def check_status() -> str:
    lines = []
    try:
        for item in arr_get(RADARR_URL, RADARR_KEY, "/queue").get("records", []):
            mb = round(item.get("sizeleft", 0) / 1024 / 1024)
            lines.append(f"Movie: {item['title']} — {item.get('status', '?')} ({mb}MB left)")
    except Exception as e:
        lines.append(f"Radarr: {e}")
    try:
        for item in arr_get(SONARR_URL, SONARR_KEY, "/queue").get("records", []):
            ep = f"S{item.get('seasonNumber', '?')}E{(item.get('episodeNumbers') or ['?'])[0]}"
            lines.append(f"TV: {item['series']['title']} {ep} — {item.get('status', '?')}")
    except Exception as e:
        lines.append(f"Sonarr: {e}")
    return "\n".join(lines) if lines else "Nothing currently downloading."

def discover(query: str, media_type: str = "movie") -> str:
    """TMDB-powered discovery: actor filmographies, similar titles, genre/keyword searches."""
    media_type = media_type.lower()
    if media_type not in ("movie", "tv"):
        media_type = "movie"
    try:
        tmdb_results = []
        source_desc = ""
        search_type = "movie" if media_type == "movie" else "tv"
        seed_genres = set()

        # Person search: only use result if popularity >= 5 to avoid matching show/band names
        person_query = _clean_query(query)
        persons = []
        for pq in ([person_query, query] if person_query != query else [query]):
            resp = tmdb_get("/search/person", {"query": pq, "include_adult": "false"})
            results = resp.get("results", [])
            if results and results[0].get("popularity", 0) >= 5:
                persons = results
                break

        if persons:
            person = persons[0]
            credits_key = "movie_credits" if media_type == "movie" else "tv_credits"
            credits = tmdb_get(f"/person/{person['id']}/{credits_key}")
            cast = credits.get("cast", [])
            cast.sort(key=lambda x: x.get("popularity", 0), reverse=True)
            tmdb_results = _english_only(cast)[:20]
            source_desc = f"{person['name']}'s top {media_type} credits"
        else:
            search_query = _clean_query(query) or query
            search_resp = tmdb_get(f"/search/{search_type}", {"query": search_query, "include_adult": "false"})
            search_hits = _english_only(search_resp.get("results", []))
            if search_hits:
                top = search_hits[0]
                try:
                    seed_genres = {g["name"].lower() for g in tmdb_get(f"/{search_type}/{top['id']}").get("genres", [])}
                except Exception:
                    seed_genres = set()
                similar = _english_only(tmdb_get(f"/{search_type}/{top['id']}/similar").get("results", []))
                recs    = _english_only(tmdb_get(f"/{search_type}/{top['id']}/recommendations").get("results", []))
                top_title = top.get("title") or top.get("name", query)

                # Keyword-based discover for genre-defining shows (e.g. superhero/comic).
                # TMDB's recommendations endpoint is poorly curated for these — keyword AND
                # logic gives much better results (Invincible, Umbrella Academy, etc.).
                kw_key = "results" if search_type == "tv" else "keywords"
                kw_resp = tmdb_get(f"/{search_type}/{top['id']}/keywords")
                top_kw_ids = {k["id"] for k in kw_resp.get(kw_key, [])}
                kw_bonus = []
                for required_kws, kw_param in _KW_GENRE_GROUPS:
                    if required_kws.issubset(top_kw_ids):
                        kw_disc = tmdb_get(f"/discover/{search_type}", {
                            "with_keywords": kw_param,
                            "sort_by": "popularity.desc",
                            "with_original_language": "en",
                        })
                        kw_bonus = _english_only([r for r in kw_disc.get("results", []) if r["id"] != top["id"]])[:10]
                        break

                # keyword results lead; recs/similar fill remaining slots
                tmdb_results = search_hits[:2] + kw_bonus[:8] + recs[:5] + similar[:5]
                source_desc = f"results similar to '{top_title}'"
            else:
                return f"Nothing found on TMDB for '{query}'."

        if media_type == "movie":
            radarr_movies = arr_get(RADARR_URL, RADARR_KEY, "/movie")
            library = {m["tmdbId"]: m.get("hasFile", False) for m in radarr_movies}
        else:
            sonarr = arr_get(SONARR_URL, SONARR_KEY, "/series")
            library_tmdb_ids = {s["tmdbId"] for s in sonarr if s.get("tmdbId")}

        output = []
        seen = set()
        for item in tmdb_results:
            tid = item.get("id")
            if tid in seen:
                continue
            seen.add(tid)
            if media_type == "movie":
                title = item.get("title", "Unknown")
                year  = (item.get("release_date") or "")[:4] or "?"
                in_lib = library.get(tid, False)
            else:
                title = item.get("name", "Unknown")
                year  = (item.get("first_air_date") or "")[:4] or "?"
                in_lib = tid in library_tmdb_ids
            overview = (item.get("overview") or "")[:120].rstrip()
            output.append({
                "title": title, "year": year,
                "tmdb_id": tid, "in_library": in_lib,
                "overview": overview
            })

        # Library genre pass: surface OWNED titles sharing the seed's genres, so
        # "what do I already have like this?" is answered from the whole library,
        # not just TMDB's similar list. Ranked by genre overlap, capped at 8.
        owned_extra = []
        if seed_genres:
            min_overlap = 2 if len(seed_genres) >= 2 else 1
            already = {o["tmdb_id"] for o in output}
            lib_items = radarr_movies if media_type == "movie" else sonarr
            scored = []
            for it in lib_items:
                tid = it.get("tmdbId")
                if not tid or tid in already:
                    continue
                if media_type == "movie" and not it.get("hasFile"):
                    continue
                g = {x.lower() for x in it.get("genres", [])}
                overlap = len(seed_genres & g)
                if overlap >= min_overlap:
                    # Jaccard ranks genre-pure matches above broad shows that merely
                    # share a common genre (e.g. a crime/mystery show beats a
                    # crime/action/sci-fi one when the seed is crime/mystery/drama).
                    jacc = overlap / (len(seed_genres | g) or 1)
                    scored.append((jacc, overlap, it))
            scored.sort(key=lambda x: (-x[0], -x[1], x[2].get("title", "")))
            for _, _, it in scored[:8]:
                owned_extra.append({
                    "title": it.get("title", "Unknown"),
                    "year": it.get("year", "?"),
                    "tmdb_id": it.get("tmdbId"),
                    "in_library": True,
                    "overview": (it.get("overview") or "")[:120].rstrip(),
                })

        # Owned genre matches first (the valuable "you already have this"), then the
        # TMDB suggestions; dedupe by tmdb_id.
        merged, seen_ids = [], set()
        for entry in owned_extra + output:
            if entry["tmdb_id"] in seen_ids:
                continue
            seen_ids.add(entry["tmdb_id"])
            merged.append(entry)

        return json.dumps({"source": source_desc, "results": merged[:20]}, ensure_ascii=False)

    except Exception as e:
        return f"ERROR: TMDB discovery failed: {e}"

def list_library(media_type: str = "movie", genre: str = None) -> str:
    """Browse the local Radarr/Sonarr library. Optionally filter by genre."""
    try:
        media_type = media_type.lower()
        if media_type == "movie":
            items = arr_get(RADARR_URL, RADARR_KEY, "/movie")
            output = []
            for m in items:
                if genre and genre.lower() not in [g.lower() for g in m.get("genres", [])]:
                    continue
                output.append({
                    "title": m["title"],
                    "year": m.get("year", "?"),
                    "genres": m.get("genres", []),
                    "has_file": m.get("hasFile", False),
                    "tmdb_id": m["tmdbId"]
                })
            output.sort(key=lambda x: x["title"])
            return json.dumps({"type": "movies", "count": len(output), "items": output})
        else:
            items = arr_get(SONARR_URL, SONARR_KEY, "/series")
            output = []
            for s in items:
                if genre and genre.lower() not in [g.lower() for g in s.get("genres", [])]:
                    continue
                stats = s.get("statistics", {})
                output.append({
                    "title": s["title"],
                    "year": s.get("year", "?"),
                    "genres": s.get("genres", []),
                    "episodes_downloaded": stats.get("episodeFileCount", 0),
                    "tmdb_id": s.get("tmdbId")
                })
            output.sort(key=lambda x: x["title"])
            return json.dumps({"type": "tv", "count": len(output), "items": output})
    except Exception as e:
        return f"ERROR: {e}"

# ── Groq tool-calling loop ────────────────────────────────────────────────────

TOOLS = [
    {"type": "function", "function": {
        "name": "search_movie",
        "description": "Search Radarr for a MOVIE by title. Use for movies only. Call this before add_movie if you don't have a tmdb_id yet.",
        "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}
    }},
    {"type": "function", "function": {
        "name": "add_movie",
        "description": "Add a MOVIE to Radarr. Use for movies only — never for TV shows. If tmdb_id is already known from discover or search results, call this directly without searching first.",
        "parameters": {"type": "object", "properties": {
            "tmdb_id": {"type": "integer"}, "title": {"type": "string"}
        }, "required": ["tmdb_id", "title"]}
    }},
    {"type": "function", "function": {
        "name": "search_tv",
        "description": "Search Sonarr for a TV SHOW by title. Use for TV shows only — never for movies. Returns tmdb_id and season_count for each result.",
        "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}
    }},
    {"type": "function", "function": {
        "name": "add_tv",
        "description": (
            "Add a TV SHOW to Sonarr. Use for TV shows only — never for movies. "
            "Requires tmdb_id (same ID from discover or search_tv). "
            "seasons: 'all' (default), 'first', 'latest', or comma-separated numbers e.g. '1,2,3'."
        ),
        "parameters": {"type": "object", "properties": {
            "tmdb_id": {"type": "integer"},
            "title": {"type": "string"},
            "seasons": {"type": "string", "description": "Which seasons to download: 'all', 'first', 'latest', or '1,2,3'"}
        }, "required": ["tmdb_id", "title"]}
    }},
    {"type": "function", "function": {
        "name": "check_status",
        "description": "Check what is currently downloading in Sonarr and Radarr.",
        "parameters": {"type": "object", "properties": {}}
    }},
    {"type": "function", "function": {
        "name": "discover",
        "description": (
            "TMDB-powered discovery. Use for: actor filmographies ('Jason Statham movies'), "
            "similar titles ('movies like Interstellar'), genre/mood queries ('80s sci-fi'), "
            "director searches, recommendations. Returns results with tmdb_id, overview, and in_library flag. "
            "Pass only the name or keyword — e.g. query='Jason Statham', not 'Jason Statham movies'."
        ),
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string"},
            "media_type": {"type": "string", "enum": ["movie", "tv"]}
        }, "required": ["query"]}
    }},
    {"type": "function", "function": {
        "name": "list_library",
        "description": "Browse what's on the server. Use for 'what movies do I have', genre filters.",
        "parameters": {"type": "object", "properties": {
            "media_type": {"type": "string", "enum": ["movie", "tv"]},
            "genre": {"type": "string"}
        }, "required": ["media_type"]}
    }},
]

TOOL_FN = {
    "search_movie": lambda a: search_movie(**a),
    "add_movie":    lambda a: add_movie(**a),
    "search_tv":    lambda a: search_tv(**a),
    "add_tv":       lambda a: add_tv(**a),
    "check_status": lambda a: check_status(),
    "discover":     lambda a: discover(**a),
    "list_library": lambda a: list_library(**a),
}

SYSTEM = """You are a smart media assistant for a home server. Help users discover, browse, and request movies and TV shows.

Tools available:
- discover: TMDB-powered — use for actor queries, similar titles, genre/mood searches, recommendations. Returns tmdb_id, overview, and in_library flag for each result. Pass only the name/keyword as query, not the full user sentence.
- list_library: browse what's on the server — use for "what do I have", genre filters.
- search_movie / add_movie: MOVIES ONLY. Both use tmdb_id. If you already have a tmdb_id from discover results, call add_movie directly — no need to search first.
- search_tv / add_tv: TV SHOWS ONLY. Both use tmdb_id. search_tv returns season_count so you know how many seasons exist.
- check_status: what's currently downloading.

IMPORTANT rules:
1. Never use search_tv or add_tv for a movie. Never use search_movie or add_movie for a TV show.
2. Both add_movie and add_tv use tmdb_id. If a title appeared in discover or search results and you have its tmdb_id, call add_movie or add_tv directly without searching again.
3. When showing results, organise as "On the server" and "Not on the server". Put a BLANK LINE before each of those two headings so they stand out. Each title appears in exactly ONE list, never both. Do NOT show tmdb_id or any numeric IDs in your reply — just the title and year. End with a short question asking which ones to add.
4. For TV shows with more than 3 seasons: before calling add_tv, ask the user whether they want all seasons, just the first, the latest, or specific seasons (e.g. "seasons 1 and 2"). Use their answer as the seasons parameter. If the show has 3 or fewer seasons, just add all without asking.
5. When a user asks for more details about a title that appeared in discover results (e.g. "tell me about that one", "what's it about"), use the overview already returned by discover — do not invent or guess details from training data.
6. Never include <function=...> or any code syntax in your reply — tool calls happen automatically, never in the message text.
7. ONLY present titles that came from a tool result (discover, search_tv, search_movie, list_library). Never list titles from your own training knowledge.
8. The "On the server" vs "Not on the server" split MUST come from each result's in_library flag — never decide it yourself.
9. If discover returns "Nothing found" or an ERROR, tell the user you could not find matches for that title and ask them to try another — do NOT invent a list of titles."""

ADD_TOOLS = {"add_movie", "add_tv"}

_FUNC_TAG = re.compile(r'<function=\w+[^<]*</function>', re.DOTALL)

def clean_response(text: str) -> str:
    return _FUNC_TAG.sub('', text).strip()

def format_add_result(result: str) -> str:
    if result.startswith("ALREADY_HAVE:"):
        return result[len("ALREADY_HAVE:"):].strip()
    if result.startswith("SEARCH_TRIGGERED:"):
        return result[len("SEARCH_TRIGGERED:"):].strip()
    if result.startswith("ADDED:"):
        return result[len("ADDED:"):].strip()
    if result.startswith("PENDING:"):
        return result[len("PENDING:"):].strip()
    if result.startswith("ERROR:"):
        return f"Sorry, that didn't work: {result[len('ERROR:'):].strip()}"
    return result

# ── Request approval (optional, REQUIRE_APPROVAL) ──────────────────────────────
# Non-admin "add" requests get queued to PENDING_FILE and the admin is pinged on
# Telegram with Approve/Deny buttons instead of adding immediately. The Telegram
# bot process handles the taps and calls perform_add(). PENDING_FILE lives on the
# shared /data volume so the web and telegram processes/containers both see it.

def _load_pending() -> dict:
    if PENDING_FILE.exists():
        try:
            return json.loads(PENDING_FILE.read_text())
        except Exception:
            return {}
    return {}

def _save_pending(d: dict):
    PENDING_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = PENDING_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(d))
    tmp.replace(PENDING_FILE)

def add_pending(entry: dict) -> str:
    d = _load_pending()
    pid = os.urandom(4).hex()
    entry["id"] = pid
    entry["ts"] = time.time()
    d[pid] = entry
    _save_pending(d)
    return pid

def pop_pending(pid: str):
    d = _load_pending()
    entry = d.pop(pid, None)
    if entry is not None:
        _save_pending(d)
    return entry

def list_pending() -> list:
    return sorted(_load_pending().values(), key=lambda e: e.get("ts", 0))

def notify_admin_request(pid: str, entry: dict):
    """Ping the admin on Telegram with Approve/Deny buttons. No-op if Telegram unset."""
    if not (TELEGRAM_TOKEN and ADMIN_ID):
        log.warning("REQUIRE_APPROVAL is on but Telegram isn't configured — request %s queued with no notification.", pid)
        return
    where = "Radarr" if entry.get("media_type") == "movie" else "Sonarr"
    text = (f"🎬 *{entry.get('requester', 'Someone')}* requested:\n"
            f"*{entry.get('title', '(unknown)')}*\n\n"
            f"Approve to add to {where}.")
    keyboard = {"inline_keyboard": [[
        {"text": "✅ Approve", "callback_data": f"ap:{pid}"},
        {"text": "❌ Deny", "callback_data": f"dn:{pid}"},
    ]]}
    try:
        httpx.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": ADMIN_ID, "text": text, "parse_mode": "Markdown", "reply_markup": keyboard},
            timeout=10,
        )
    except Exception as e:
        log.error("Failed to notify admin of request %s: %s", pid, e)

def perform_add(entry: dict) -> str:
    """Run the actual add for an approved request."""
    try:
        return TOOL_FN[entry["tool"]](entry["args"])
    except Exception as e:
        return f"ERROR: {e}"

def _execute_add(name: str, args: dict, is_admin: bool, requester_name: str) -> str:
    """Either perform an add immediately, or queue it for admin approval."""
    if REQUIRE_APPROVAL and not is_admin:
        entry = {
            "tool": name,
            "args": args,
            "media_type": "movie" if name == "add_movie" else "tv",
            "title": args.get("title", "(unknown)"),
            "requester": requester_name or "A web user",
        }
        pid = add_pending(entry)
        notify_admin_request(pid, entry)
        log.info("Queued for approval: '%s' (%s) by %s", entry["title"], pid, entry["requester"])
        return f"PENDING: '{entry['title']}' has been sent for approval — you'll get it once it's approved."
    try:
        return TOOL_FN[name](args)
    except Exception as e:
        return f"ERROR: {e}"

groq_client = Groq(api_key=GROQ_API_KEY)

# Per-session conversation history: {session_key: {"messages": [...], "last_active": float}}
# session_key is the Telegram user_id (int) for the bot, or a web session id (str) for the web frontend.
_user_sessions: dict = {}

def process_request(user_id, text: str, requester_name: str = None, is_admin: bool = None) -> str:
    now = time.time()
    if is_admin is None:
        is_admin = isinstance(user_id, int) and user_id == ADMIN_ID

    # Load existing session or start fresh
    session = _user_sessions.get(user_id)
    if session and (now - session["last_active"]) > SESSION_TTL:
        session = None

    history = list(session["messages"]) if session else []
    history.append({"role": "user", "content": text})
    messages = [{"role": "system", "content": SYSTEM}] + history

    for i in range(8):
        try:
            resp = groq_client.chat.completions.create(
                model=GROQ_MODEL, messages=messages, tools=TOOLS, tool_choice="auto", max_tokens=1024
            )
        except Exception as e:
            err = str(e)
            if "tool_use_failed" in err:
                # Recover from any malformed <function=name ...{args}...> variant
                func_name, args = _recover_malformed(err)
                if func_name and func_name in TOOL_FN:
                    log.info("Recovering malformed call: %s %s", func_name, args)
                    if func_name in ADD_TOOLS:
                        result = _execute_add(func_name, args or {}, is_admin, requester_name)
                        log.info("Tool %s → %s", func_name, str(result)[:120])
                        _user_sessions[user_id] = {"messages": [], "last_active": now}
                        return format_add_result(result)
                    try:
                        result = TOOL_FN[func_name](args or {})
                    except Exception as te:
                        result = f"ERROR: {te}"
                    log.info("Tool %s → %s", func_name, str(result)[:120])
                    # Search/discover tool — inject result and continue
                    fake_id = f"recover_{i}"
                    messages.append({"role": "assistant", "content": None, "tool_calls": [
                        {"id": fake_id, "type": "function",
                         "function": {"name": func_name, "arguments": json.dumps(args or {})}}
                    ]})
                    messages.append({"role": "tool", "tool_call_id": fake_id, "content": result})
                    continue
            log.error("Groq error: %s", e)
            return "Something went wrong — please try again."

        msg = resp.choices[0].message
        assistant_msg = {"role": "assistant", "content": msg.content}
        if msg.tool_calls:
            assistant_msg["tool_calls"] = [
                {"id": tc.id, "type": "function",
                 "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                for tc in msg.tool_calls
            ]
        messages.append(assistant_msg)

        if not msg.tool_calls:
            # Save conversation history (strip system message)
            _user_sessions[user_id] = {"messages": messages[1:], "last_active": now}
            return clean_response(msg.content or "Done.")

        add_result = None
        for tc in msg.tool_calls:
            args = json.loads(tc.function.arguments)
            name = tc.function.name
            if name in ADD_TOOLS:
                result = _execute_add(name, args, is_admin, requester_name)
            else:
                try:
                    result = TOOL_FN[name](args)
                except Exception as e:
                    result = f"ERROR: {e}"
            log.info("Tool %s → %s", name, result[:120])
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})
            if name in ADD_TOOLS:
                add_result = result

        if add_result is not None:
            _user_sessions[user_id] = {"messages": [], "last_active": now}
            return format_add_result(add_result)

    return "Sorry, I couldn't complete that — please try again."
