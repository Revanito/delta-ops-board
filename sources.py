"""Shared fetch/parse logic for the delta-ops-board website generator.

Two data sources, covering different halves of the scene:
- Liquipedia's deltaforce wiki: a site-wide match ticker plus the tournament
  portal (tier/prize-pool/results context). Delta Force's competitive scene
  runs two parallel tracks by game mode - "Warfare" (traditional team mode,
  best-of-N elimination brackets, e.g. Delta Force Invitational Warfare) and
  "Operations" (extraction-shooter mode, lobby/points standings, e.g. RISE
  Series, Pro League) - and Liquipedia covers both, tagged by a name-based
  heuristic (see `_infer_mode`) since the match ticker doesn't expose the
  distinction structurally.
- TiMi's own api-dfgw.timi-es.com backend: undocumented but public/no-auth,
  confirmed live behind playdeltaforce.com's RISE Series page. Gives the
  authoritative standings/schedule/roster data for whichever Operations
  tournaments actually run on it (confirmed: RISE Series EMEA + Americas;
  don't assume other Operations events use this backend without checking -
  a season_id has to be found via `fetch_season_config` or discovered on
  playdeltaforce.com first). This is an undocumented third-party backend,
  not a stable public API, and could change shape or disappear without
  notice - re-verify field names if these calls start failing.

Unlike r6-notifier, there's no Ubisoft-equivalent official schedule page and
no siege.gg-equivalent third-party stats site for Delta Force (checked
deltaforceesports.com - a near-empty community-qualifier SPA with no public
API - and escharts.com - a generic multi-game wrapper, not a source of
truth). So Liquipedia is the only match-ticker source: no secondary feed to
cross-check "live" status or backfill flags/logos/bracket links against.
Country flags/team logos for Warfare-track teams aren't available from
either source yet - a future improvement, not solved here.
"""
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import unquote

import requests
from bs4 import BeautifulSoup

log = logging.getLogger("delta-ops-board.sources")

LIQUIPEDIA_API = "https://liquipedia.net/deltaforce/api.php"
LIQUIPEDIA_UA = "delta-ops-board/1.0 (personal site generator; contact via github.com/Revanito/delta-ops-board)"

DFGW_API = "https://api-dfgw.timi-es.com/df"
DFGW_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"

# twitchdrops.app's URL slug is the game's full Twitch category name
# ("Delta Force: Hawk Ops"), not just "delta-force" - the plain slug 301s
# here. Same markup (drop-card/drop-name/drop-time/campaign-banner) as
# r6-notifier's page, confirmed live.
DROPS_URL = "https://twitchdrops.app/game/delta-force-hawk-ops"
DROPS_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
DROPS_CHANNEL_RE = re.compile(r"twitch\.tv/([^/?#\"]+)", re.I)

# The game's own drops microsite (playdeltaforce.com/act/twitchdrops/) is a
# JS SPA with no server-rendered content, but it just fetches this static
# JSON straight off the same host - no auth, same-origin. Unlike
# twitchdrops.app (whose `.drop-campaign` field came back blank on every
# card checked so far), this carries the real campaign name and an exact
# date range per campaign, plus the site's full drops history (49 entries
# at last check) - useful for a future archive page, not just "what's
# active now".
DROPS_CALENDAR_URL = "https://www.playdeltaforce.com/act/twitchdrops/js/activitymaps.json"

WARFARE_NAME_RE = re.compile(r"\bwarfare\b", re.I)


def get_twitch_token(client_id, client_secret):
    resp = requests.post(
        "https://id.twitch.tv/oauth2/token",
        data={"client_id": client_id, "client_secret": client_secret, "grant_type": "client_credentials"},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def fetch_twitch_live_info(client_id, client_secret, channels):
    """Live status for a set of channel logins. Returns {login: {title, game, viewers}}."""
    channels = sorted({c.lower() for c in channels if c})
    if not client_id or not client_secret or not channels:
        return {}

    token = get_twitch_token(client_id, client_secret)
    headers = {"Client-Id": client_id, "Authorization": f"Bearer {token}"}
    params = [("user_login", c) for c in channels]
    resp = requests.get("https://api.twitch.tv/helix/streams", headers=headers, params=params, timeout=15)
    resp.raise_for_status()

    info = {}
    for stream in resp.json().get("data", []):
        login = stream["user_login"].lower()
        title = stream.get("title", "")
        info[login] = {
            "title": title,
            "game": stream.get("game_name", ""),
            "viewers": stream.get("viewer_count", 0),
        }
    return info


def fetch_active_drops():
    """Scrapes twitchdrops.app's public Delta Force page for real drops-
    campaign data: which channels are currently eligible for drops, and
    what the rewards actually are. Ported from r6-notifier - same site,
    same markup, just a different game slug and (per a spot check) a much
    shorter campaign/reward history since Delta Force is a newer title.
    This site publishes a plain-text chatbot API (twitchdrops.app/api/
    chatbot/...) built for public consumption, so scraping its HTML for the
    same data is in the same spirit.

    Returns (rewards, active_channels):
    - rewards: currently-active rewards only (the page tags expired ones
      with a "drop-expired" class we filter out) -
      [{"name", "watch_time", "campaign", "image"}, ...]
    - active_channels: lowercased set of channel logins eligible right now
      (campaign banners whose countdown hasn't ended yet)
    """
    resp = requests.get(DROPS_URL, headers={"User-Agent": DROPS_UA}, timeout=20)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    rewards = []
    for card in soup.select(".drop-card:not(.drop-expired)"):
        name_el = card.select_one(".drop-name")
        time_el = card.select_one(".drop-time")
        campaign_el = card.select_one(".drop-campaign")
        img_el = card.select_one(".drop-img")
        rewards.append({
            "name": name_el.get_text(strip=True) if name_el else "",
            "watch_time": time_el.get_text(strip=True) if time_el else "",
            "campaign": campaign_el.get_text(strip=True) if campaign_el else "",
            "image": img_el.get("src") if img_el else None,
        })

    now_ms = time.time() * 1000
    active_channels = set()
    for banner in soup.select(".campaign-banner"):
        timer = banner.select_one(".cb-timer[data-end-ts]")
        if timer:
            try:
                if float(timer["data-end-ts"]) <= now_ms:
                    continue  # campaign already ended
            except (ValueError, TypeError):
                pass
        for link in banner.select("a.channel-link"):
            m = DROPS_CHANNEL_RE.search(link.get("href", ""))
            if m:
                active_channels.add(m.group(1).lower())

    return rewards, active_channels


def _parse_act_time_range(text):
    """Parses the subset of activitymaps.json's `act_time` formats that
    carry an explicit year into (start_ts, end_ts) UTC epoch seconds, at
    day granularity (end_ts is the start of the day *after* the listed end
    date, so a same-day check is an inclusive `start_ts <= now < end_ts`).
    Two other formats seen in the archive - "M.D H:MM - H:MM UTC+N" and
    "Mon D, H:MM - H:MM UTC+N" - never carry a year, so guessing one across
    a multi-year archive would be unreliable; those return None and are
    shown as raw text instead of being date-compared."""
    text = (text or "").strip()

    m = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{4})(?:\s+\d{1,2}:\d{2})?\s*-\s*(\d{1,2})/(\d{1,2})/(\d{4})(?:\s+\d{1,2}:\d{2})?$", text)
    if m:
        sm, sd, sy, em, ed, ey = (int(g) for g in m.groups())
        start = datetime(sy, sm, sd, tzinfo=timezone.utc)
        end = datetime(ey, em, ed, tzinfo=timezone.utc) + timedelta(days=1)
        return start.timestamp(), end.timestamp()

    m = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{4})\s+\d{1,2}:\d{2}\s*-\s*\d{1,2}:\d{2}$", text)
    if m:
        sm, sd, sy = (int(g) for g in m.groups())
        start = datetime(sy, sm, sd, tzinfo=timezone.utc)
        return start.timestamp(), (start + timedelta(days=1)).timestamp()

    return None


def fetch_drops_calendar():
    """The official drops event calendar: every campaign's real name and
    exact date range (see DROPS_CALENDAR_URL). Returns a list of
    {name, date_range, start_ts, end_ts} in whatever order the source JSON
    has them - start_ts/end_ts are None for the two ambiguous no-year
    formats `_parse_act_time_range` won't guess at."""
    resp = requests.get(DROPS_CALENDAR_URL, headers={"User-Agent": DROPS_UA}, timeout=20)
    resp.raise_for_status()
    data = resp.json()

    entries = []
    for act in (data.get("activities") or {}).get("EN", {}).values():
        date_range = act.get("act_time") or ""
        parsed = _parse_act_time_range(date_range)
        entries.append({
            "name": act.get("act_name") or act.get("title1") or "",
            "date_range": date_range,
            "start_ts": parsed[0] if parsed else None,
            "end_ts": parsed[1] if parsed else None,
        })
    return entries


def current_drops_campaigns(calendar, now=None):
    """Entries from fetch_drops_calendar() whose parsed date range covers
    `now`. Entries with an unparsed (no-year) date range never match here."""
    now = now if now is not None else time.time()
    return [c for c in calendar if c["start_ts"] is not None and c["start_ts"] <= now < c["end_ts"]]


def _make_key(ts, teams):
    return f"{ts}|{teams[0]}|{teams[1]}"


WARFARE_SUFFIX_RE = re.compile(r"\s*\((?:Warfare|DF team)\)$", re.I)


def _clean_warfare_name(name):
    """Liquipedia disambiguates an org's Warfare-mode roster from its
    Operations-mode one with a literal "(Warfare)" (or "(DF team)")
    suffix in the page title/aria-label - accurate, but redundant on a
    page that's already Warfare-only, and long enough on some team names
    (e.g. "Rex Regum Qeon (Warfare)") to overflow a match ticket. Only
    call this where mode is already known to be "warfare"."""
    return WARFARE_SUFFIX_RE.sub("", name)


def _infer_mode(tournament_name):
    """Liquipedia's match ticker doesn't carry a structured game-mode field,
    only the tournament name - so this is a heuristic, not ground truth.
    Only the Warfare track consistently says "Warfare" in its tournament
    names (Delta Force Invitational Warfare, Pan-Pacific Warfare Cup, ...);
    Operations-track tournaments either say "Operations" explicitly or
    don't mention a mode at all (RISE Series, Pro League), so "Operations"
    is the default rather than "unknown"."""
    return "warfare" if WARFARE_NAME_RE.search(tournament_name or "") else "operations"


def fetch_liquipedia_page_html(page):
    resp = requests.get(
        LIQUIPEDIA_API,
        headers={"User-Agent": LIQUIPEDIA_UA, "Accept-Encoding": "gzip"},
        params={"action": "parse", "page": page, "format": "json", "prop": "text"},
        timeout=20,
    )
    resp.raise_for_status()
    return resp.json()["parse"]["text"]["*"]


def fetch_liquipedia_html():
    return fetch_liquipedia_page_html("Liquipedia:Matches")


def _extract_teams(match_div):
    """2-team head-to-head markup (`.match-info-header-opponent`) - the
    classic bracket-match template, used by the Warfare track when a
    Warfare tournament has ticker entries."""
    return [
        re.sub(r"\s*\(page does not exist\)$", "", a.get("title", a.get_text(strip=True)))
        for a in match_div.select(".match-info-header-opponent .name a")
    ]


def _extract_lobby_winner(match_div):
    """Operations-track games render with an entirely different template
    (`.match-info-headerbr*`, Liquipedia's battle-royale/lobby layout) -
    no head-to-head opponent pair at all. The site-wide ticker only ever
    surfaces one `.match-info-headerbr-positionrow` per finished game (1st
    place / the winner) - full per-lobby standings aren't available from
    this feed, only from `fetch_schedule_result` for tournaments that run
    on api-dfgw. Upcoming lobby games carry no team info here at all."""
    row = match_div.select_one(".match-info-headerbr-positionrow")
    if not row:
        return None
    a = row.select_one(".block-team .name a")
    if not a:
        return None
    return re.sub(r"\s*\(page does not exist\)$", "", a.get("title", a.get_text(strip=True)))


def build_team_links(html):
    """Team name -> Liquipedia roster page URL, built from every team link
    on the page (broader than any single match, so a team seen once gets
    linked everywhere it appears). Covers both the head-to-head and lobby
    match templates (see `_extract_teams` / `_extract_lobby_winner`)."""
    soup = BeautifulSoup(html, "html.parser")
    links = {}
    for a in soup.select(".match-info-header-opponent .name a, .match-info-headerbr-opponent .name a"):
        raw_title = a.get("title", a.get_text(strip=True))
        if "(page does not exist)" in raw_title:
            continue  # red link - no real roster page to send people to
        name = re.sub(r"\s*\(page does not exist\)$", "", raw_title)
        href = a.get("href")
        if href and name not in links:
            links[name] = "https://liquipedia.net" + href
    return links


def _extract_tournament(match_div):
    el = match_div.select_one(".match-info-tournament-name a")
    return el.get_text(strip=True) if el else "Unknown tournament"


def _extract_twitch_channel(match_div):
    link = match_div.select_one('a[href*="Special:Stream/twitch/"]')
    if not link:
        return None
    m = re.search(r"Special:Stream/twitch/([^\"?#]+)", link["href"])
    return unquote(m.group(1)) if m else None


def parse_liquipedia_matches(html):
    """Returns all matches (upcoming, live, and recently completed) found
    anywhere on the page, tagged with their status and inferred game mode
    (see `_infer_mode`). "live" is a best-effort read of Liquipedia's own
    ticker state, not a confirmed feed - there's no second source to cross-
    check it against here (unlike r6-notifier, which trusted only Ubisoft's
    feed for this and used Liquipedia purely for breadth).

    Two ticker templates are handled (see `_extract_teams` /
    `_extract_lobby_winner`): head-to-head entries get a full `teams`
    pair + score; lobby (Operations) entries only ever expose a winner in
    this feed, so those come back as a single-team, scoreless "result"
    (`format: "lobby_result"`) - real standings for those need
    `fetch_schedule_result`. Upcoming lobby games carry no matchup info at
    all in this feed and are dropped rather than shown as fake "TBD vs
    TBD" cards."""
    soup = BeautifulSoup(html, "html.parser")
    matches = []

    for match_div in soup.select("div.match-info"):
        timer = match_div.select_one(".match-info-countdown .timer-object")
        if not timer or not timer.get("data-timestamp"):
            continue

        ts = int(timer["data-timestamp"])
        finished = timer.get("data-finished") == "finished"
        is_past = ts <= time.time()
        tournament = _extract_tournament(match_div)
        mode = _infer_mode(tournament)

        teams = _extract_teams(match_div)
        if mode == "warfare":
            teams = [_clean_warfare_name(t) for t in teams]
        if len(teams) >= 2:
            score = None
            winner_idx = None
            if finished or is_past:
                score_spans = match_div.select(".match-info-header-scoreholder-score")
                if len(score_spans) >= 2:
                    score = (score_spans[0].get_text(strip=True), score_spans[1].get_text(strip=True))
                opponents = match_div.select(".match-info-header-opponent")
                for i, opp in enumerate(opponents[:2]):
                    if "match-info-header-winner" in opp.get("class", []):
                        winner_idx = i

            live = False
            if not finished and is_past:
                # Past but never flagged "finished": could mean live right
                # now, or a stale ticker entry for a match that already
                # ended with no score posted. If a score exists, treat it
                # as a result; if the start time was recent, treat it as
                # tentatively live; otherwise too ambiguous to show.
                if score and any(s.strip() for s in score):
                    finished = True
                elif (time.time() - ts) < 3 * 3600:
                    live = True
                else:
                    continue

            matches.append({
                "format": "head_to_head",
                "timestamp": ts,
                "teams": teams,
                "tournament": tournament,
                "mode": mode,
                "twitch_channel": _extract_twitch_channel(match_div),
                "live": live,
                "finished": finished,
                "score": score,
                "winner_index": winner_idx,
                "key": _make_key(ts, teams),
            })
            continue

        winner = _extract_lobby_winner(match_div)
        if not winner:
            continue  # upcoming lobby game - no matchup info in this feed

        matches.append({
            "format": "lobby_result",
            "timestamp": ts,
            "teams": [winner],
            "tournament": tournament,
            "mode": "operations",
            "twitch_channel": _extract_twitch_channel(match_div),
            "live": False,
            "finished": True,
            "score": None,
            "winner_index": 0,
            "key": f"{ts}|{winner}",
        })

    return matches


# Liquipedia's global match ticker (Liquipedia:Matches) almost never carries
# Warfare-track entries - it's dominated by the higher-frequency Operations
# groups/qualifiers - so a Warfare tournament's own page has to be checked
# directly for its bracket. There's no Ubisoft-style "here's the currently
# active event" feed to derive this from automatically (see module
# docstring), so this list is maintained by hand: (Liquipedia page title,
# display name). Update it when a new Warfare LAN/qualifier bracket goes up
# - check https://liquipedia.net/deltaforce/Portal:Tournaments for the
# current S/A-tier Warfare entries and their page titles.
BRACKET_PAGES = [
    ("Delta Force Invitational/Warfare/2026", "Delta Force Invitational 2026 · Warfare"),
]


def parse_liquipedia_bracket(html, tournament_name):
    """Bracket-match popups (`.brkts-popup .match-info-header`) reuse the
    exact same markup as the global ticker's head-to-head matches (see
    `parse_liquipedia_matches` / `_extract_teams`), just nested inside the
    bracket widget - so this reads it the same way, minus round/column
    position (Liquipedia's bracket DOM nests round columns recursively by
    depth with no simple per-match round label, too fragle to reconstruct
    reliably - so these come back as a flat list of results, not a bracket
    tree). Always tagged mode="warfare": only Warfare tournaments use this
    2-team bracket template."""
    soup = BeautifulSoup(html, "html.parser")
    matches = []

    for popup in soup.select(".brkts-popup"):
        header = popup.select_one(".match-info-header")
        if not header:
            continue
        opponents = header.select(".match-info-header-opponent")
        if len(opponents) < 2:
            continue
        teams = [_clean_warfare_name(t) for t in _extract_teams(header)]
        if len(teams) < 2:
            continue

        timer = popup.select_one(".match-info-countdown .timer-object")
        ts = int(timer["data-timestamp"]) if timer and timer.get("data-timestamp") else None
        finished = bool(timer and timer.get("data-finished") == "finished")

        score = None
        winner_idx = None
        score_spans = popup.select(".match-info-header-scoreholder-score")
        if len(score_spans) >= 2:
            score = (score_spans[0].get_text(strip=True), score_spans[1].get_text(strip=True))
        for i, opp in enumerate(opponents[:2]):
            if "match-info-header-winner" in opp.get("class", []):
                winner_idx = i

        matches.append({
            "format": "head_to_head",
            "source": "bracket",
            "timestamp": ts,
            "teams": teams,
            "tournament": tournament_name,
            "mode": "warfare",
            "twitch_channel": None,
            "live": False,
            "finished": finished,
            "score": score,
            "winner_index": winner_idx,
            "key": _make_key(ts or 0, teams),
        })

    return matches


def fetch_bracket_matches():
    """Fetches every page in BRACKET_PAGES. Returns (matches, team_links).
    Each page fetched independently - one missing/renamed page logs and is
    skipped rather than failing the whole batch."""
    matches = []
    team_links = {}
    for page, tournament_name in BRACKET_PAGES:
        try:
            html = fetch_liquipedia_page_html(page)
            matches.extend(parse_liquipedia_bracket(html, tournament_name))
            team_links.update(build_team_links(html))
        except Exception:
            log.exception("bracket fetch failed for %s", page)
    return matches, team_links


def gather_all_matches():
    """Fetch + parse the Liquipedia match ticker, plus any configured
    Warfare bracket pages (see BRACKET_PAGES - the ticker rarely carries
    Warfare entries). Returns (matches, team_links)."""
    matches = []
    team_links = {}
    try:
        html = fetch_liquipedia_html()
        matches = parse_liquipedia_matches(html)
        team_links = build_team_links(html)
    except Exception:
        log.exception("liquipedia fetch failed")

    bracket_matches, bracket_team_links = fetch_bracket_matches()
    ticker_keys = {m["key"] for m in matches}
    matches.extend(m for m in bracket_matches if m["key"] not in ticker_keys)
    team_links = {**bracket_team_links, **team_links}  # ticker links win on overlap - same source, no reason to prefer either, but keep it deterministic

    return matches, team_links


def split_by_status(matches, now=None):
    """Returns (live, upcoming, completed) lists."""
    now = now if now is not None else time.time()
    live = [m for m in matches if m.get("live")]
    upcoming = [m for m in matches if not m.get("live") and not m.get("finished") and m["timestamp"] > now]
    completed = [m for m in matches if m.get("finished")]
    return live, upcoming, completed


def _dfgw_get(endpoint, params):
    """GET against TiMi's api-dfgw backend. Raises on transport errors and
    on the API's own {"result": <nonzero>, "msg": "..."} error convention
    (result 0 = success; anything else carries a Chinese-language `msg`,
    e.g. 20010001 for an invalid `region`)."""
    resp = requests.get(
        f"{DFGW_API}/{endpoint}",
        params=params,
        headers={"User-Agent": DFGW_UA, "Accept": "application/json"},
        timeout=20,
    )
    resp.raise_for_status()
    payload = resp.json()
    if payload.get("result") != 0:
        raise RuntimeError(f"api-dfgw {endpoint} error {payload.get('result')}: {payload.get('msg')}")
    return payload["data"]


def fetch_season_config(region):
    """region is "EMEA" or "AM". Returns {season_id, season_name, utc_offset,
    region, season_list, live_current} for RISE Series' current season."""
    return _dfgw_get("getSeasonConfig", {"region": region})


def fetch_standings(season_id):
    """Overall season standings: [{rank, team_id, team_name, logo_url, points}, ...]."""
    return _dfgw_get("getStandings", {"season_id": season_id})["list"]


def fetch_team_list(season_id):
    """Rosters: [{team_id, team_name, logo_url, players: [{player_id, nickname, avatar_url}, ...]}, ...]."""
    return _dfgw_get("getTeamList", {"season_id": season_id})["list"]


def fetch_schedule_list(season_id):
    """Upcoming/past lobbies: [{schedule_id, stage, title, match_time (unix
    string), status, teams: [{team_id, team_name, logo_url, rank}, ...]}, ...].
    Each entry is a multi-team lobby (RISE Series runs 3v3v3 groups of up to
    6 teams), not a head-to-head match - there is no 2-team bracket model
    here, unlike the Warfare track."""
    return _dfgw_get("getScheduleList", {"season_id": season_id})["list"]


def fetch_schedule_result(schedule_id):
    """Final per-team placement for one lobby: {schedule_id, results:
    [{rank, team_id, team_name, logo_url, decode_count, kill_score,
    asset_score}, ...]}. No single combined "points" field is exposed here -
    `fetch_standings` carries the season-cumulative points instead."""
    return _dfgw_get("getScheduleResult", {"schedule_id": schedule_id})


def fetch_mvp_ranking(season_id):
    """[{rank, player_id, player_name, avatar_url, team_name, mvp_count}, ...]."""
    return _dfgw_get("getMVPRanking", {"season_id": season_id})["list"]


def fetch_news_list(region):
    """region is "EMEA" or "AM" (not season_id, unlike the other endpoints).
    Returns {carousel: [{news_id, title, carousel_url, link_url}, ...],
    list: [{news_id, title, link_url, updated_at}, ...]}."""
    return _dfgw_get("getNewsList", {"region": region})


def gather_rise_series_data(region):
    """Full snapshot for one RISE Series region: season config, standings,
    rosters, and the schedule (lobby) list. Raises on any failure - unlike
    `gather_all_matches`, there's no partial-source fallback to fall back to
    for this data, so let the caller decide how to handle it."""
    config = fetch_season_config(region)
    season_id = config["season_id"]
    return {
        "season_id": season_id,
        "region": config["region"],
        "standings": fetch_standings(season_id),
        "teams": fetch_team_list(season_id),
        "schedule": fetch_schedule_list(season_id),
    }
