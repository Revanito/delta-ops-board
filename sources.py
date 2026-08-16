"""Shared fetch/parse logic for both the Discord notifier and the website generator.

Two public data sources, merged and de-duplicated:
- Ubisoft's official esports page (accurate, but only covers whatever event
  Ubisoft is currently spotlighting on that page)
- Liquipedia's site-wide match ticker (broader coverage, occasionally lags
  on team reveals for not-yet-started bracket slots)
"""
import json
import logging
import re
import time
from datetime import datetime
from urllib.parse import unquote

import requests
from bs4 import BeautifulSoup

log = logging.getLogger("r6-notifier.sources")

LIQUIPEDIA_API = "https://liquipedia.net/rainbowsix/api.php"
LIQUIPEDIA_UA = "r6-notifier/1.0 (personal notification bot; contact via github.com/Revanito/r6-notifier)"

UBISOFT_URL = "https://www.ubisoft.com/en-us/esports/rainbow-six/siege"
UBISOFT_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
NEXT_DATA_RE = re.compile(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.S)

DROPS_URL = "https://twitchdrops.app/game/tom-clancys-rainbow-six-siege"
DROPS_CHANNEL_RE = re.compile(r"twitch\.tv/([^/?#\"]+)", re.I)

SIEGE_GG_BASE = "https://siege.gg"
SIEGE_GG_API = "https://siege.gg/api/stats/matches"
NUXT_DATA_RE = re.compile(r'<script type="application/json"[^>]*id="__NUXT_DATA__"[^>]*>(.*?)</script>', re.S)
TEAM_NAME_SUFFIXES = ("esports", "esport", "gaming", "clan", "team", "gg")

MATCH_DEDUP_BUCKET_SECONDS = 300  # matches within this window are treated as the same match across sources


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
    """Scrapes twitchdrops.app's public R6 Siege page for real drops-campaign
    data: which channels are currently eligible for drops, and what the
    rewards actually are. This site publishes a plain-text chatbot API
    (twitchdrops.app/api/chatbot/...) built for public consumption, so
    scraping its HTML for the same data is in the same spirit.

    Returns (rewards, active_channels):
    - rewards: currently-active rewards only (the page tags expired ones
      with a "drop-expired" class we filter out) -
      [{"name", "watch_time", "campaign", "image"}, ...]
    - active_channels: lowercased set of channel logins eligible right now
      (campaign banners whose countdown hasn't ended yet)
    """
    resp = requests.get(DROPS_URL, headers={"User-Agent": UBISOFT_UA}, timeout=20)
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


def _normalize_team_name(name):
    """Loose key for matching the same team across sources that spell its
    name slightly differently ("DarkZero" on Liquipedia vs "DarkZero
    Esports" on siege.gg)."""
    key = re.sub(r"[^a-z0-9]+", "", (name or "").lower())
    for suffix in TEAM_NAME_SUFFIXES:
        if key.endswith(suffix) and len(key) > len(suffix) + 2:
            key = key[: -len(suffix)]
    return key


def _siege_gg_winner_index(rosters, winner_id, score):
    """winner_id straight from the API when present; siege.gg has been seen
    to leave it null on an otherwise-decided match (e.g. round score tied
    17-17 but maps_won 2-1), so fall back to comparing the map score."""
    if winner_id:
        for i, r in enumerate(rosters[:2]):
            if r.get("id") == winner_id:
                return i
    if score and score[0] != score[1]:
        return 0 if score[0] > score[1] else 1
    return None


def fetch_siege_gg_matches(cutoff_ts, max_pages=5):
    """siege.gg's public JSON API backing /matches. Each page response
    carries the *entire* "upcoming" list plus one page of the "results"
    list (paginated, newest first, ~9-10 days per page) - the "tab" query
    param turns out to only affect the frontend's own display, not what the
    API returns, so a single set of page fetches covers both. Paginates
    backwards through results until older than cutoff_ts. Used for: per-team
    country flags and logos (Ubisoft/Liquipedia expose neither), the
    siege.gg match-page link, the per-map score (maps_won_a/b, e.g. "1-3")
    as the authoritative result, and each match's competition_id (used to
    look up its playoff bracket)."""
    seen_ids = set()
    matches = []

    def _add(m):
        if m.get("id") in seen_ids:
            return
        seen_ids.add(m.get("id"))
        rosters = m.get("rosters") or []
        if len(rosters) < 2:
            return
        score = (m["maps_won_a"], m["maps_won_b"]) if m.get("has_results") else None
        ts = datetime.fromisoformat(m["date"].replace("Z", "+00:00")).timestamp() if m.get("date") else None
        matches.append({
            "timestamp": ts,
            "teams": [r.get("name") for r in rosters[:2]],
            "flags": [r.get("flag") for r in rosters[:2]],
            "logos": [r.get("logo_url") for r in rosters[:2]],
            "score": score,
            "winner_index": _siege_gg_winner_index(rosters, m.get("winner_id"), score),
            "result_url": (SIEGE_GG_BASE + m["web_url"]) if m.get("web_url") else None,
            "competition_id": m.get("competition_id"),
            "competition_name": m.get("competition_full_name") or m.get("competition_name"),
        })

    page = 1
    while page <= max_pages:
        resp = requests.get(
            SIEGE_GG_API,
            params={"page": page, "tab": "results"},
            headers={"User-Agent": UBISOFT_UA, "Accept": "application/json"},
            timeout=20,
        )
        resp.raise_for_status()
        payload = resp.json()

        for m in payload.get("upcoming", {}).get("data") or []:
            _add(m)

        results = payload.get("results", {})
        data = results.get("data") or []
        if not data:
            break

        reached_cutoff = False
        for m in data:
            ts = datetime.fromisoformat(m["date"].replace("Z", "+00:00")).timestamp()
            if ts < cutoff_ts:
                reached_cutoff = True
                continue
            _add(m)

        if reached_cutoff or not results.get("next_page_url"):
            break
        page += 1

    return matches


def build_siege_gg_data(lookback_days=16):
    """Returns (flag_map, logo_map, by_pair):
    - flag_map / logo_map: {normalized_team_name: image_url}
    - by_pair: {frozenset(normalized_team_names): [siege_gg_match, ...]}
    """
    siege_matches = fetch_siege_gg_matches(cutoff_ts=time.time() - lookback_days * 86400)

    flag_map = {}
    logo_map = {}
    by_pair = {}
    for sm in siege_matches:
        for team, flag, logo in zip(sm["teams"], sm["flags"], sm["logos"]):
            if not team:
                continue
            key = _normalize_team_name(team)
            if flag:
                flag_map.setdefault(key, flag)
            if logo:
                logo_map.setdefault(key, logo)
        key = frozenset(_normalize_team_name(t) for t in sm["teams"])
        by_pair.setdefault(key, []).append(sm)

    return flag_map, logo_map, by_pair


def attach_siege_gg_data(matches, flag_map, logo_map, by_pair):
    """Enriches already-merged matches in place with flags/logos for both
    teams and, where a matching siege.gg match exists (paired by team
    names, sanity-checked by timestamp proximity since siege.gg's kickoff
    time can differ from Ubisoft/Liquipedia's by a few minutes), the
    result-page link, authoritative per-map score, and competition_id."""
    for m in matches:
        m["flags"] = [flag_map.get(_normalize_team_name(t)) for t in m["teams"]]
        m["logos"] = [logo_map.get(_normalize_team_name(t)) for t in m["teams"]]

        key = frozenset(_normalize_team_name(t) for t in m["teams"])
        candidates = by_pair.get(key)
        if not candidates:
            continue
        sm = min(candidates, key=lambda c: abs((c["timestamp"] or 0) - m["timestamp"]))
        if sm["timestamp"] is None or abs(sm["timestamp"] - m["timestamp"]) > 6 * 3600:
            continue
        m["result_url"] = sm.get("result_url")
        m["competition_id"] = sm.get("competition_id")
        m["competition_name"] = sm.get("competition_name")
        if sm.get("score") and (m.get("finished") or m.get("live")):
            m["score"] = sm["score"]
            if sm.get("winner_index") is not None:
                m["winner_index"] = sm["winner_index"]
    return matches


def fetch_competition_bracket(competition_id):
    """Playoff-only matches for one competition, from siege.gg's
    per-competition matches API (the same one its /competitions/<id> page
    uses to render the bracket)."""
    resp = requests.get(
        f"{SIEGE_GG_BASE}/api/stats/competitions/{competition_id}/matches",
        headers={"User-Agent": UBISOFT_UA, "Accept": "application/json"},
        timeout=20,
    )
    resp.raise_for_status()
    payload = resp.json()
    all_matches = (payload.get("results") or []) + (payload.get("upcoming") or [])

    bracket = []
    for m in all_matches:
        if not m.get("playoff"):
            continue
        rosters = m.get("rosters") or []
        score = (m["maps_won_a"], m["maps_won_b"]) if m.get("has_results") else None
        ts = datetime.fromisoformat(m["date"].replace("Z", "+00:00")).timestamp() if m.get("date") else None
        bracket.append({
            "teams": [r.get("name") for r in rosters[:2]],
            "flags": [r.get("flag") for r in rosters[:2]],
            "logos": [r.get("logo_url") for r in rosters[:2]],
            "score": score,
            "winner_index": _siege_gg_winner_index(rosters, m.get("winner_id"), score),
            "round": m.get("round") or "TBD",
            "sequence": m.get("sequence") or 0,
            "timestamp": ts,
            "result_url": (SIEGE_GG_BASE + m["web_url"]) if m.get("web_url") else None,
        })
    return bracket


def _devalue_resolve(raw, idx, depth=0):
    """Nuxt's SSR payload (__NUXT_DATA__) is a flat array where objects
    reference each other by index (devalue format) instead of nesting
    inline. Walks those references back into a normal dict/list."""
    if depth > 25:
        return None
    v = raw[idx]
    if isinstance(v, list):
        if len(v) == 2 and isinstance(v[0], str) and v[0] in ("ShallowReactive", "Reactive", "Ref"):
            return _devalue_resolve(raw, v[1], depth + 1)
        return [_devalue_resolve(raw, x, depth + 1) if isinstance(x, int) else x for x in v]
    if isinstance(v, dict):
        return {k: (_devalue_resolve(raw, x, depth + 1) if isinstance(x, int) else x) for k, x in v.items()}
    return v


def fetch_competition_info(competition_id):
    """Competition-level info (prize pool, region, venue, participating
    teams, ...) scraped from a competition's siege.gg page - there's no
    JSON API for this, only what's embedded in the page's Nuxt payload.
    /competitions/<id> redirects to the full slugged URL regardless of
    slug, so the numeric id alone is enough."""
    resp = requests.get(
        f"{SIEGE_GG_BASE}/competitions/{competition_id}",
        headers={"User-Agent": UBISOFT_UA},
        timeout=20,
    )
    resp.raise_for_status()
    m = NUXT_DATA_RE.search(resp.text)
    if not m:
        return None
    raw = json.loads(m.group(1))

    info = None
    for idx, item in enumerate(raw):
        if isinstance(item, dict) and "prizepool" in item and "stats" in item:
            info = _devalue_resolve(raw, idx)
            break
    if not info:
        return None

    teams = (info.get("stats") or {}).get("involved_teams") or []
    return {
        "name": info.get("name"),
        "logo_url": info.get("logo_url"),
        "date": info.get("date"),
        "type": "Online" if info.get("online") else "LAN",
        "region": info.get("region"),
        "prizepool": info.get("prizepool"),
        "location": info.get("full_location"),
        "venue": info.get("venue"),
        "flag": info.get("flag"),
        "web_url": (SIEGE_GG_BASE + info["web_url"]) if info.get("web_url") else f"{SIEGE_GG_BASE}/competitions/{competition_id}",
        "teams": [
            {
                "name": t.get("name"),
                "logo_url": t.get("logo_url"),
                "flag": t.get("flag"),
                "web_url": (SIEGE_GG_BASE + t["web_url"]) if t.get("web_url") else None,
            }
            for t in teams
        ],
    }


def fetch_active_competition_info(matches, now=None):
    """Sidebar data for whichever tournament fetch_active_bracket would
    also be showing, or None if none of the given matches carry a
    competition_id."""
    comp_id, _ = pick_active_competition(matches, now=now)
    if not comp_id:
        return None
    return fetch_competition_info(comp_id)


def pick_active_competition(matches, now=None):
    """Which tournament's bracket is most relevant right now: a live
    match's competition wins outright; otherwise whichever competition has
    a match closest in time to now (soonest upcoming, or most recent
    result if nothing's scheduled)."""
    now = now if now is not None else time.time()
    candidates = [m for m in matches if m.get("competition_id") and m.get("timestamp")]
    if not candidates:
        return None, None
    live = [m for m in candidates if m.get("live")]
    pool = live or candidates
    best = min(pool, key=lambda m: abs(m["timestamp"] - now))
    return best["competition_id"], best.get("competition_name") or best.get("tournament")


def fetch_active_bracket(matches, now=None):
    """Playoff bracket for whichever tournament is currently most relevant
    among the given (already status/window-filtered) matches, or ([], None)
    if none of them carry a competition_id or that competition has no
    playoff-stage matches yet."""
    comp_id, comp_name = pick_active_competition(matches, now=now)
    if not comp_id:
        return [], None
    bracket = fetch_competition_bracket(comp_id)
    if not bracket:
        return [], None
    return bracket, comp_name


def _make_key(ts, teams):
    return f"{ts}|{teams[0]}|{teams[1]}"


def fetch_ubisoft_matches():
    """Official schedule/results for the currently-featured event. More
    authoritative than Liquipedia but limited in breadth."""
    resp = requests.get(UBISOFT_URL, headers={"User-Agent": UBISOFT_UA}, timeout=20)
    resp.raise_for_status()
    m = NEXT_DATA_RE.search(resp.text)
    if not m:
        return [], []
    data = json.loads(m.group(1))
    page_data = data["props"]["pageProps"]["pageData"]

    matches = []
    for match in page_data.get("matches", []):
        if match.get("isTimeTBD"):
            continue
        ts = match["timestamp"]
        teams = [match["team1"]["name"], match["team2"]["name"]]
        is_live = bool(match.get("live"))
        scores = match.get("scores") or {}
        score = (scores.get("team1", 0), scores.get("team2", 0))
        # NB: Ubisoft pre-populates "gameScores" with zeroed placeholder
        # entries for every possible map before a match even starts (e.g. 5
        # empty slots for a Bo5), so those can't be used as a "has a score"
        # signal - only the top-level series score and a past start time can.
        has_score = score != (0, 0)
        finished = (not is_live) and has_score and ts <= time.time()

        matches.append({
            "timestamp": ts,
            "teams": teams,
            "tournament": match.get("competition", {}).get("name", "Unknown tournament"),
            "twitch_channel": None,
            "live": is_live,
            "finished": finished,
            "score": score if (finished or is_live) else None,
            "key": _make_key(ts, teams),
        })

    live_channels = [c.lower() for c in page_data.get("liveChannels", [])]
    return matches, live_channels


def fetch_liquipedia_html():
    resp = requests.get(
        LIQUIPEDIA_API,
        headers={"User-Agent": LIQUIPEDIA_UA, "Accept-Encoding": "gzip"},
        params={"action": "parse", "page": "Liquipedia:Matches", "format": "json", "prop": "text"},
        timeout=20,
    )
    resp.raise_for_status()
    return resp.json()["parse"]["text"]["*"]


def _extract_teams(match_div):
    return [
        re.sub(r"\s*\(page does not exist\)$", "", a.get("title", a.get_text(strip=True)))
        for a in match_div.select(".match-info-header-opponent .name a")
    ]


def build_team_links(html):
    """Team name -> Liquipedia roster page URL, built from every team link
    on the page (broader than any single match, so a team seen once gets
    linked everywhere it appears - Ubisoft-sourced matches included, since
    they carry no such link of their own)."""
    soup = BeautifulSoup(html, "html.parser")
    links = {}
    for a in soup.select(".match-info-header-opponent .name a"):
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
    anywhere on the page, tagged with their live/finished status."""
    soup = BeautifulSoup(html, "html.parser")
    matches = []

    for match_div in soup.select("div.match-info"):
        timer = match_div.select_one(".match-info-countdown .timer-object")
        if not timer or not timer.get("data-timestamp"):
            continue

        ts = int(timer["data-timestamp"])
        finished = timer.get("data-finished") == "finished"
        is_past = ts <= time.time()
        teams = _extract_teams(match_div)
        if len(teams) < 2:
            teams = teams + ["TBD"] * (2 - len(teams))

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

        if not finished and is_past:
            # Past but never flagged "finished": Liquipedia's live/ticker state
            # can't be trusted to mean "live right now" (it also shows stale
            # entries from matches that already ended). If a score got
            # posted, treat it as a result; otherwise it's too ambiguous to
            # show at all rather than risk a false "LIVE" claim.
            if score and any(s.strip() for s in score):
                finished = True
            else:
                continue

        matches.append({
            "timestamp": ts,
            "teams": teams,
            "tournament": _extract_tournament(match_div),
            "twitch_channel": _extract_twitch_channel(match_div),
            "live": False,  # only Ubisoft's feed is trusted for "live right now"
            "finished": finished,
            "score": score,
            "winner_index": winner_idx,
            "key": _make_key(ts, teams),
        })

    return matches


def _bucket(ts):
    return round(ts / MATCH_DEDUP_BUCKET_SECONDS)


def _merge(primary, secondary, drop_tbd=True):
    """Primary source wins on overlap, but backfills fields it lacks (e.g.
    Ubisoft never exposes a twitch_channel) from the matching secondary
    entry instead of just discarding it. Secondary entries with no bucket
    match in primary are appended as-is."""
    primary_by_bucket = {_bucket(m["timestamp"]): m for m in primary}
    combined = list(primary)
    for match in secondary:
        if drop_tbd and "TBD" in match["teams"]:
            continue
        bucket = _bucket(match["timestamp"])
        existing = primary_by_bucket.get(bucket)
        if existing is not None:
            if not existing.get("twitch_channel") and match.get("twitch_channel"):
                existing["twitch_channel"] = match["twitch_channel"]
            continue
        combined.append(match)
    return combined


def gather_all_matches():
    """Fetch + merge both sources. Returns (all_matches, ubisoft_live_channels, team_links)."""
    ubi_matches, ubi_live_channels = [], []
    try:
        ubi_matches, ubi_live_channels = fetch_ubisoft_matches()
    except Exception:
        log.exception("ubisoft fetch failed")

    lp_matches = []
    team_links = {}
    try:
        html = fetch_liquipedia_html()
        lp_matches = parse_liquipedia_matches(html)
        team_links = build_team_links(html)
    except Exception:
        log.exception("liquipedia fetch failed")

    combined = _merge(ubi_matches, lp_matches)

    try:
        flag_map, logo_map, results_by_pair = build_siege_gg_data()
        combined = attach_siege_gg_data(combined, flag_map, logo_map, results_by_pair)
    except Exception:
        log.exception("siege.gg fetch failed")

    return combined, ubi_live_channels, team_links


def split_by_status(matches, now=None):
    """Returns (live, upcoming, completed) lists."""
    now = now if now is not None else time.time()
    live = [m for m in matches if m.get("live")]
    upcoming = [m for m in matches if not m.get("live") and not m.get("finished") and m["timestamp"] > now]
    completed = [m for m in matches if m.get("finished")]
    return live, upcoming, completed
