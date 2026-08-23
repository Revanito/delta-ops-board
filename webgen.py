import html
import logging
import os
import subprocess
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import sources

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("delta-site")

REPO_URL = os.environ["SITE_REPO_URL"]  # e.g. https://github.com/Revanito/delta-ops-board.git
GITHUB_TOKEN = os.environ["GITHUB_TOKEN"]
GIT_USER_NAME = os.environ.get("GIT_USER_NAME", "delta-ops-board-bot")
GIT_USER_EMAIL = os.environ.get("GIT_USER_EMAIL", "delta-ops-board-bot@users.noreply.github.com")

CLONE_DIR = os.environ.get("SITE_CLONE_DIR", "/repo")
DOCS_SUBDIR = os.environ.get("SITE_DOCS_SUBDIR", "docs")
BRANCH = os.environ.get("SITE_BRANCH", "main")

BUILD_INTERVAL_MINUTES = int(os.environ.get("SITE_BUILD_INTERVAL_MINUTES", "10"))
IDLE_BUILD_INTERVAL_MINUTES = int(os.environ.get("SITE_IDLE_BUILD_INTERVAL_MINUTES", str(24 * 60)))
# A match live right now always counts; otherwise "is an event going on" means
# something's scheduled to start within this many hours - keeps the site on
# the fast interval through same-day gaps between matches, not just during
# them. Based on the Warfare track's head-to-head matches only, since that's
# the only format where "live"/"upcoming" is meaningful here - the Operations
# track's lobby games don't carry a confirmed live signal from Liquipedia's
# ticker (see sources.parse_liquipedia_matches), and RISE Series' own season
# calendar isn't currently modeled as a start/end window.
ACTIVE_LOOKAHEAD_HOURS = float(os.environ.get("ACTIVE_LOOKAHEAD_HOURS", "48"))
TZ_NAME = os.environ.get("TZ_NAME", "Europe/Paris")
LOCAL_TZ = ZoneInfo(TZ_NAME)
RUN_ON_START = os.environ.get("RUN_ON_START", "false").lower() == "true"

UPCOMING_WINDOW_DAYS = float(os.environ.get("UPCOMING_WINDOW_DAYS", "30"))
RESULTS_WINDOW_DAYS = float(os.environ.get("RESULTS_WINDOW_DAYS", "14"))
RISE_LOBBIES_SHOWN = int(os.environ.get("RISE_LOBBIES_SHOWN", "6"))

# Optional Twitch app credentials. If set, the site can show non-match
# broadcasts (reveal streams, showcases, ...) as their own "live" card, and
# tag any live card with a drops badge when the stream title mentions it. If
# unset, the site just skips this - it still works fine on match data alone.
TWITCH_CLIENT_ID = os.environ.get("TWITCH_CLIENT_ID", "")
TWITCH_CLIENT_SECRET = os.environ.get("TWITCH_CLIENT_SECRET", "")
TWITCH_CHANNELS = [c.strip().lower() for c in os.environ.get("TWITCH_CHANNELS", "deltaforcegameofficial").split(",") if c.strip()]

RISE_REGIONS = [("EMEA", "EMEA"), ("AM", "Americas")]


def authed_repo_url():
    if REPO_URL.startswith("https://"):
        return REPO_URL.replace("https://", f"https://{GITHUB_TOKEN}@", 1)
    return REPO_URL


def _redact(text):
    return text.replace(GITHUB_TOKEN, "***") if GITHUB_TOKEN else text


def _run(args, cwd=CLONE_DIR):
    """Like subprocess.run(check=True), but never lets the token reach logs
    or exception messages - git argv (and CalledProcessError's repr of it)
    would otherwise leak it verbatim."""
    result = subprocess.run(args, cwd=cwd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(
            f"command failed ({result.returncode}): {_redact(' '.join(args))}\n{_redact(result.stderr)}"
        )
    return result


def run_git(args, cwd=CLONE_DIR):
    return _run(["git"] + args, cwd=cwd)


def ensure_repo():
    if not os.path.isdir(os.path.join(CLONE_DIR, ".git")):
        log.info("cloning repo into %s", CLONE_DIR)
        os.makedirs(CLONE_DIR, exist_ok=True)
        _run(["git", "clone", "--branch", BRANCH, authed_repo_url(), CLONE_DIR], cwd=None)
        run_git(["config", "user.name", GIT_USER_NAME])
        run_git(["config", "user.email", GIT_USER_EMAIL])
    else:
        run_git(["fetch", "origin", BRANCH])
        run_git(["checkout", BRANCH])
        run_git(["reset", "--hard", f"origin/{BRANCH}"])


def fmt_dt(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(LOCAL_TZ).strftime("%a %d %b, %H:%M")


def fmt_date_utc(ts):
    # sources._parse_act_time_range's timestamps carry no real time-of-day
    # (day granularity only) - converting through LOCAL_TZ would risk
    # shifting a UTC-midnight boundary across a day, so this stays in UTC.
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%d %b %Y")


def current_campaign_label(calendar_entries):
    """Human-readable "name · date - date" for whatever's running right
    now, per sources.current_drops_campaigns(). None if nothing's running
    or the calendar fetch failed."""
    if not calendar_entries:
        return None
    parts = []
    for c in calendar_entries:
        start_str = fmt_date_utc(c["start_ts"])
        end_str = fmt_date_utc(c["end_ts"] - 1)  # end_ts is exclusive (start of the next day)
        parts.append(f'{c["name"]} · {start_str} – {end_str}')
    return " / ".join(parts)


def e(text):
    return html.escape(str(text))


# ---------------------------------------------------------------------------
# Head-to-head match tickets (Warfare track)
# ---------------------------------------------------------------------------

def render_team(name, side_class, team_links, flag_url=None, logo_url=None):
    url = team_links.get(name)
    flag_html = f'<img class="flag" src="{e(flag_url)}" alt="" loading="lazy">' if flag_url else ""
    logo_html = f'<img class="team-logo" src="{e(logo_url)}" alt="" loading="lazy">' if logo_url else ""
    name_html = (
        f'<a class="team" href="{e(url)}" target="_blank" rel="noopener">{e(name)}</a>'
        if url else f'<span class="team">{e(name)}</span>'
    )
    # Logo sits at the outer edge (away from the score), flag+name stay
    # innermost next to it - matches how broadcast scoreboards lay these out.
    parts = [logo_html, flag_html, name_html] if side_class == "team-a" else [flag_html, name_html, logo_html]
    return f'<span class="team-line {side_class}">{"".join(parts)}</span>'


def render_match_row(match, kind, drops=False, team_links=None):
    team_links = team_links or {}
    teams = match["teams"]
    flags = match.get("flags") or [None, None]
    logos = match.get("logos") or [None, None]
    when = fmt_dt(match["timestamp"])
    if kind in ("live", "completed") and match.get("score"):
        s0, s1 = match["score"]
        w = match.get("winner_index")
        t0_cls = " winner" if w == 0 else ""
        t1_cls = " winner" if w == 1 else ""
        score_html = (
            f'<span class="score"><span class="digit{t0_cls}">{e(s0)}</span>'
            f'<span class="dash">–</span><span class="digit{t1_cls}">{e(s1)}</span></span>'
        )
    else:
        score_html = '<span class="vs">vs</span>'

    twitch_html = ""
    if match.get("twitch_channel"):
        twitch_html = f'<a class="twitch-link" href="https://www.twitch.tv/{e(match["twitch_channel"])}" target="_blank" rel="noopener">Watch on Twitch ↗</a>'

    badge = {
        "live": '<span class="badge badge-live"><span class="dot"></span>Live</span>',
        "upcoming": "",
        "completed": '<span class="badge badge-done">Final</span>',
    }[kind]
    drops_html = '<span class="badge badge-drops">Drops enabled</span>' if drops else ""

    return f"""
    <article class="ticket ticket-{kind}">
      <div class="ticket-row">
        {render_team(teams[0], "team-a", team_links, flags[0], logos[0])}
        {score_html}
        {render_team(teams[1], "team-b", team_links, flags[1], logos[1])}
      </div>
      <div class="ticket-meta">
        <span class="tournament">{e(match["tournament"])}</span>
        <span class="dot-sep">·</span>
        <span class="when">{e(when)} Paris</span>
        {twitch_html}
        {badge}
        {drops_html}
      </div>
    </article>"""


def render_broadcast_row(b):
    """A live Twitch stream not tied to a tracked match - e.g. a reveal
    show, dev stream, or anything else airing on a watched channel."""
    game_html = f' · {e(b["game"])}' if b.get("game") else ""
    drops_html = '<span class="badge badge-drops">Drops enabled</span>' if b.get("has_drops") else ""

    return f"""
    <article class="ticket ticket-live ticket-broadcast">
      <div class="ticket-row ticket-row-broadcast">
        <span class="broadcast-title">{e(b["title"] or b["channel"])}</span>
      </div>
      <div class="ticket-meta">
        <span class="tournament">twitch.tv/{e(b["channel"])}{game_html}</span>
        <a class="twitch-link" href="https://www.twitch.tv/{e(b["channel"])}" target="_blank" rel="noopener">Watch on Twitch ↗</a>
        <span class="badge badge-live"><span class="dot"></span>Live</span>
        {drops_html}
      </div>
    </article>"""


def render_reward_card(r):
    img_html = f'<img class="reward-img" src="{e(r["image"])}" alt="{e(r["name"])}" loading="lazy">' if r.get("image") else ""
    return f"""
    <article class="reward-card">
      {img_html}
      <div class="reward-name">{e(r["name"])}</div>
      <div class="reward-time">{e(r["watch_time"])}</div>
      <div class="reward-campaign">{e(r["campaign"])}</div>
    </article>"""


def render_rewards_section(rewards, campaign_label=None):
    if not rewards:
        return ""
    cards = "".join(render_reward_card(r) for r in rewards)
    label_html = f'<p class="rewards-campaign">{e(campaign_label)}</p>' if campaign_label else ""
    return f"""
    <section class="section-rewards">
      <h2>Active drops</h2>
      {label_html}
      <div class="rewards-grid">{cards}</div>
    </section>"""


def render_section(title, row_htmls, kind, empty_text):
    if not row_htmls:
        body = f'<p class="empty">{e(empty_text)}</p>'
    else:
        body = "".join(row_htmls)
    return f"""
    <section class="section-{kind}">
      <h2>{e(title)}</h2>
      <div class="ticket-list">{body}</div>
    </section>"""


def render_day_grouped_section(title, matches, row_renderer, kind, empty_text):
    """Like render_section, but splits already-(desc-)sorted matches into
    one ticket-list grid per local calendar day, each under its own labeled
    divider - a long flat wall of same-shaped result cards otherwise reads
    as undifferentiated, and grouping by day also keeps grid row-stretch
    alignment (see .ticket-list align-items: stretch) from spanning across
    unrelated days."""
    if not matches:
        return f"""
    <section class="section-{kind}">
      <h2>{e(title)}</h2>
      <p class="empty">{e(empty_text)}</p>
    </section>"""

    groups = []
    current_date, current_group = None, None
    for m in matches:
        d = datetime.fromtimestamp(m["timestamp"], tz=timezone.utc).astimezone(LOCAL_TZ).date()
        if d != current_date:
            current_date, current_group = d, []
            groups.append((d, current_group))
        current_group.append(m)

    day_blocks = "".join(
        f"""
      <div class="day-group">
        <h3 class="day-divider">{e(d.strftime("%A %d %b"))}</h3>
        <div class="ticket-list">{"".join(row_renderer(m) for m in group)}</div>
      </div>"""
        for d, group in groups
    )

    return f"""
    <section class="section-{kind}">
      <h2>{e(title)}</h2>
      {day_blocks}
    </section>"""


# ---------------------------------------------------------------------------
# Operations track: scene-wide lobby results (from Liquipedia) + RISE Series
# standings/schedule (from api-dfgw, RISE Series only)
# ---------------------------------------------------------------------------

def render_lobby_result_row(match):
    """A scene-wide Operations result from Liquipedia's ticker - winner
    only, no full standings (see sources.parse_liquipedia_matches)."""
    when = fmt_dt(match["timestamp"])
    return f"""
    <article class="ticket ticket-completed">
      <div class="lobby-title">Winner: {e(match["teams"][0])}</div>
      <div class="ticket-meta">
        <span class="tournament">{e(match["tournament"])}</span>
        <span class="dot-sep">·</span>
        <span class="when">{e(when)} Paris</span>
        <span class="badge badge-done">Final</span>
      </div>
    </article>"""


def render_standings_row(t):
    logo_html = f'<img class="team-logo-sm" src="{e(t["logo_url"])}" alt="" loading="lazy">' if t.get("logo_url") else ""
    return f'<tr><td class="standings-rank">{e(t["rank"])}</td><td class="standings-team">{logo_html}<span>{e(t["team_name"])}</span></td><td class="standings-points">{e(t["points"])}</td></tr>'


def render_standings_table(standings):
    if not standings:
        return '<p class="empty">Standings unavailable.</p>'
    rows = "".join(render_standings_row(t) for t in sorted(standings, key=lambda t: t["rank"]))
    return f"""
    <table class="standings-table">
      <thead><tr><th>#</th><th>Team</th><th>Pts</th></tr></thead>
      <tbody>{rows}</tbody>
    </table>"""


def render_mvp_row(p):
    avatar_html = f'<img class="mvp-avatar" src="{e(p["avatar_url"])}" alt="" loading="lazy">' if p.get("avatar_url") else ""
    return f'<li class="mvp-row">{avatar_html}<span class="mvp-name">{e(p["player_name"])}</span><span class="mvp-team">{e(p["team_name"])}</span><span class="mvp-count">{e(p["mvp_count"])} MVP</span></li>'


def render_mvp_panel(mvp_list):
    if not mvp_list:
        return ""
    top = sorted(mvp_list, key=lambda p: p["rank"])[:5]
    rows = "".join(render_mvp_row(p) for p in top)
    return f'<div class="side-panel"><h3>Top MVPs</h3><ul class="mvp-list">{rows}</ul></div>'


def render_lobby_card(entry):
    """One RISE Series lobby (getScheduleList entry). Finished lobbies
    carry each team's final placement directly on `teams[].rank` - no need
    to also call fetch_schedule_result for a basic placement display."""
    teams = entry.get("teams") or []
    finished = bool(entry.get("status"))
    when = fmt_dt(int(entry["match_time"]))
    replay_html = (
        f'<a class="result-link" href="{e(entry["replay_url"])}" target="_blank" rel="noopener">Watch VOD ↗</a>'
        if entry.get("replay_url") else ""
    )

    if finished and teams:
        ranked = sorted(teams, key=lambda t: t.get("rank") or 99)
        top = ranked[:3]
        rest = len(ranked) - len(top)
        places = " · ".join(f'<span class="lobby-place">#{e(t.get("rank"))} {e(t["team_name"])}</span>' for t in top)
        if rest > 0:
            places += f' <span class="lobby-more">+{rest} more</span>'
        places_html = f'<div class="lobby-places">{places}</div>'
        badge = '<span class="badge badge-done">Final</span>'
    else:
        places_html = '<div class="lobby-places"><span class="empty-inline">Result pending</span></div>'
        badge = ""

    return f"""
    <article class="ticket ticket-lobby">
      <div class="lobby-title">{e(entry.get("title") or entry.get("stage") or "Lobby")}</div>
      {places_html}
      <div class="ticket-meta">
        <span class="when">{e(when)} Paris</span>
        {replay_html}
        {badge}
      </div>
    </article>"""


def render_rise_series_section(region_label, data):
    if not data:
        return f"""
    <section class="section-rise">
      <h2>RISE Series · {e(region_label)}</h2>
      <p class="empty">Data unavailable.</p>
    </section>"""

    schedule_sorted = sorted(data["schedule"], key=lambda s: int(s["match_time"]), reverse=True)
    lobby_cards = "".join(render_lobby_card(s) for s in schedule_sorted[:RISE_LOBBIES_SHOWN])

    return f"""
    <section class="section-rise">
      <h2>RISE Series · {e(region_label)}</h2>
      <div class="rise-grid">
        <div class="rise-standings">{render_standings_table(data["standings"])}</div>
        <div class="rise-side">{render_mvp_panel(data.get("mvp") or [])}</div>
      </div>
      <h3 class="rise-subhead">Recent lobbies</h3>
      <div class="ticket-list">{lobby_cards}</div>
    </section>"""


# ---------------------------------------------------------------------------
# DFPL (Delta Force Pro League, China) - see sources.py's DFPL section
# ---------------------------------------------------------------------------

def render_dfpl_standings_row(rank, name, logo_url, wins):
    logo_html = f'<img class="team-logo-sm" src="{e(logo_url)}" alt="" loading="lazy">' if logo_url else ""
    return f'<tr><td class="standings-rank">{e(rank)}</td><td class="standings-team">{logo_html}<span>{e(name)}</span></td><td class="standings-points">{e(wins)}</td></tr>'


def render_dfpl_standings_table(team_ranks, team_map):
    """DFPL exposes no points/rank field (see sources.fetch_dfpl_team_rank_list) -
    ranked here by win_times, the closest analog to a competitive result."""
    if not team_ranks:
        return '<p class="empty">Standings unavailable.</p>'
    ranked = sorted(team_ranks, key=lambda t: t["win_times"], reverse=True)
    rows = "".join(
        render_dfpl_standings_row(i + 1, sources.dfpl_team_name(t["team_id"], team_map), t.get("team_logo"), t["win_times"])
        for i, t in enumerate(ranked)
    )
    return f"""
    <table class="standings-table">
      <thead><tr><th>#</th><th>Team</th><th>Wins</th></tr></thead>
      <tbody>{rows}</tbody>
    </table>"""


def render_dfpl_player_row(handle, team_name, kd):
    return f'<li class="mvp-row"><span class="mvp-name">{e(handle)}</span><span class="mvp-team">{e(team_name)}</span><span class="mvp-count">{e(kd)} KD</span></li>'


def render_dfpl_player_panel(player_ranks, team_map):
    if not player_ranks:
        return ""
    top = sorted(player_ranks, key=lambda p: p["kd"], reverse=True)[:5]
    rows = "".join(
        render_dfpl_player_row(sources.dfpl_player_handle(p["player_name"]), sources.dfpl_team_name(p["team_id"], team_map), p["kd"])
        for p in top
    )
    return f'<div class="side-panel"><h3>Top players (K/D)</h3><ul class="mvp-list">{rows}</ul></div>'


def render_dfpl_schedule_card(entry, team_map):
    """One DFPL lobby (getDfScheduleList entry). schedule_status 4 = finished
    (verified against a full season); team_list/schedule_result are
    ";"-joined team_ids, empty until set."""
    finished = entry.get("schedule_status") == 4
    when = fmt_dt(int(entry["start_timestamp"]))
    title = sources.dfpl_schedule_title(entry["scheduleid"])

    replay_html = ""
    reply_list = entry.get("reply_list") or []
    if reply_list and reply_list[0].get("vid"):
        replay_html = f'<a class="result-link" href="https://v.qq.com/x/page/{e(reply_list[0]["vid"])}.html" target="_blank" rel="noopener">Watch VOD ↗</a>'

    result_ids = [t for t in (entry.get("schedule_result") or "").split(";") if t]
    if finished and result_ids:
        ranked_names = [sources.dfpl_team_name(t, team_map) for t in result_ids]
        top = ranked_names[:3]
        rest = len(ranked_names) - len(top)
        places = " · ".join(f'<span class="lobby-place">#{i + 1} {e(name)}</span>' for i, name in enumerate(top))
        if rest > 0:
            places += f' <span class="lobby-more">+{rest} more</span>'
        places_html = f'<div class="lobby-places">{places}</div>'
        badge = '<span class="badge badge-done">Final</span>'
    else:
        places_html = '<div class="lobby-places"><span class="empty-inline">Result pending</span></div>'
        badge = ""

    return f"""
    <article class="ticket ticket-lobby">
      <div class="lobby-title">{e(title)}</div>
      {places_html}
      <div class="ticket-meta">
        <span class="when">{e(when)} Paris</span>
        {replay_html}
        {badge}
      </div>
    </article>"""


# ---------------------------------------------------------------------------
# Shared page shell
# ---------------------------------------------------------------------------

PAGE_STYLE = """
:root {
  --bg: #eef0f2;
  --bg-wash: #e4e7ea;
  --card: #ffffff;
  --text: #12151a;
  --text-dim: #5b6470;
  --border: #d8dce1;
  --accent: #d9600a;
  --accent-ink: #ffffff;
  --live: #d0271f;
  --lose: #a7adb6;
  --win-box: #1a9c53;
  --win-ink: #ffffff;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --bg: #0d0f12; --bg-wash: #15181c; --card: #15181c; --text: #eceef0;
    --text-dim: #838d97; --border: #262b31; --accent: #ff8c3a; --accent-ink: #16110a;
    --live: #ff453a; --lose: #5c636b; --win-box: #2fd673; --win-ink: #0a1f12;
  }
}
:root[data-theme="dark"] {
  --bg: #0d0f12; --bg-wash: #15181c; --card: #15181c; --text: #eceef0;
  --text-dim: #838d97; --border: #262b31; --accent: #ff8c3a; --accent-ink: #16110a;
  --live: #ff453a; --lose: #5c636b; --win-box: #2fd673; --win-ink: #0a1f12;
}
* { box-sizing: border-box; }
html { background: var(--bg); }
body {
  margin: 0; padding: 0 1rem 4rem; background: var(--bg); color: var(--text);
  font-family: -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
}
.layout {
  margin: 0 auto; padding: 0 0.5rem;
  display: grid; gap: 1.75rem; align-items: start;
  /* align-items:start matters here: the grid ROW is still sized to the
     tallest item regardless, but start keeps `.sidebar`'s own box at its
     natural (shorter) height within that tall row instead of stretching
     it to match - that gap is exactly the "room" position:sticky needs to
     actually stick as `main` scrolls past it. */
}
.layout.no-sidebar { grid-template-columns: 1fr; max-width: 980px; }
/* sidebar-double (Operations): two same-shape panels (RISE EMEA/Americas)
   each get their own full column - .sidebar itself becomes `display:
   contents` so its children (not the wrapper) are the grid items. */
.layout.sidebar-double { grid-template-columns: 3fr 1fr 1fr; max-width: 1800px; }
.layout.sidebar-double .sidebar { display: contents; }
@media (max-width: 1250px) { .layout.sidebar-double { grid-template-columns: 1fr; } }
/* sidebar-single (DFPL, and any future single-panel page): one real boxed
   sidebar stacking whatever it's given (player panel, upcoming/recent
   lobby sections, ...) in a single wide column next to the main content. */
.layout.sidebar-single { grid-template-columns: 1fr 480px; max-width: 1500px; }
@media (max-width: 1000px) { .layout.sidebar-single { grid-template-columns: 1fr; } }
main { min-width: 0; }
.sidebar { position: sticky; top: 1rem; margin-top: 15px; display: flex; flex-direction: column; gap: 1.75rem; min-width: 0; }
/* single-sidebar pages (DFPL) have no per-panel heading of their own at
   the very top the way sidebar-double's RISE panels do, so nudging it
   down further aligns it with where the main column's content (not just
   its header) actually starts. */
.layout.sidebar-single .sidebar { margin-top: 95px; }
.site-nav {
  max-width: 980px; margin: 0 auto; padding: 1.1rem 0.5rem 0;
  display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 0.75rem;
}
.site-nav-title {
  font: 800 1rem/1 "Segoe UI", -apple-system, "Arial Narrow", sans-serif;
  font-stretch: condensed; text-transform: uppercase; letter-spacing: 0.03em;
}
.site-nav-links { display: flex; gap: 0.4rem; }
.nav-link {
  text-decoration: none; color: var(--text-dim); font-weight: 700; font-size: 0.82rem;
  padding: 0.4rem 0.85rem; border-radius: 999px; border: 1px solid var(--border);
}
.nav-link.active { color: var(--accent-ink); background: var(--accent); border-color: var(--accent); }
.nav-link:not(.active):hover { border-color: var(--accent); color: var(--accent); }
header {
  padding: 1.75rem 0 1.75rem;
  border-bottom: 1px solid var(--border);
  margin-bottom: 2rem;
}
.eyebrow {
  font: 700 0.72rem/1 -apple-system, "Segoe UI", Roboto, sans-serif;
  text-transform: uppercase; letter-spacing: 0.14em; color: var(--accent); margin: 0 0 0.6rem;
}
h1 {
  font: 800 1.9rem/1.1 "Segoe UI", -apple-system, Roboto, "Arial Narrow", sans-serif;
  font-stretch: condensed; text-transform: uppercase; letter-spacing: 0.01em;
  margin: 0 0 0.4rem; text-wrap: balance;
}
.subtitle { color: var(--text-dim); font-size: 0.92rem; margin: 0; }
h2 {
  font: 700 0.78rem/1 -apple-system, "Segoe UI", sans-serif;
  text-transform: uppercase; letter-spacing: 0.1em; color: var(--text-dim);
  margin: 0 0 0.85rem; display: flex; align-items: center; gap: 0.5rem;
}
h3.rise-subhead {
  font: 700 0.72rem/1 -apple-system, "Segoe UI", sans-serif;
  text-transform: uppercase; letter-spacing: 0.1em; color: var(--text-dim);
  margin: 1.5rem 0 0.85rem;
}
section { margin-bottom: 2.25rem; }
.section-live h2 { color: var(--live); }
.ticket-list { display: grid; grid-template-columns: repeat(auto-fit, minmax(310px, 1fr)); gap: 0.6rem; align-items: start; }
/* stretch (not start): row-neighbors otherwise render at their own content
   height - a 1-line title next to a 2-line one looks visibly misaligned
   even though both sit in the same grid row. .ticket's flex column +
   .ticket-meta's auto top-margin (below) is what pins the meta/badge row
   to a shared bottom edge once the card is stretched taller than its
   own content. */
.section-completed .ticket-list, .section-rise .ticket-list {
  grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); align-items: stretch;
}
.day-group { margin-bottom: 1.1rem; }
.day-group:last-child { margin-bottom: 0; }
.day-divider {
  font: 700 0.7rem/1 -apple-system, "Segoe UI", sans-serif; text-transform: uppercase;
  letter-spacing: 0.08em; color: var(--text-dim); margin: 0 0 0.6rem;
  padding-bottom: 0.45rem; border-bottom: 1px solid var(--border);
}
.ticket {
  background: var(--card); border: 1px solid var(--border); border-left: 3px solid var(--border);
  border-radius: 4px; padding: 0.7rem 0.85rem; position: relative;
  display: flex; flex-direction: column; gap: 0.4rem;
}
.ticket-live { border-left-color: var(--live); padding-top: 2.1rem; }
.ticket-completed, .ticket-lobby { opacity: 0.92; }
.ticket-row { display: grid; grid-template-columns: 1fr auto 1fr; align-items: center; gap: 0.6rem; }
.team-line { display: inline-flex; align-items: center; gap: 0.45rem; min-width: 0; }
.team-line.team-a { justify-content: flex-end; }
.team-line.team-b { justify-content: flex-start; }
.flag { width: 20px; height: 14px; object-fit: cover; border-radius: 2px; flex: none; box-shadow: 0 0 0 1px var(--border); }
.team-logo { width: 34px; height: 34px; object-fit: contain; border-radius: 6px; flex: none; background: var(--bg-wash); padding: 4px; }
.team {
  font: 700 1.15rem/1.25 "Segoe UI", -apple-system, "Arial Narrow", sans-serif;
  font-stretch: condensed; letter-spacing: 0.01em; min-width: 0; overflow-wrap: break-word;
}
a.team { color: inherit; text-decoration: none; }
a.team:hover { color: var(--accent); text-decoration: underline; }
.vs { color: var(--text-dim); font-size: 0.78rem; text-transform: uppercase; letter-spacing: 0.08em; }
.score {
  display: flex; align-items: center; gap: 0.3rem; justify-content: center;
  font: 700 1.2rem/1 ui-monospace, "Cascadia Mono", Consolas, "SFMono-Regular", monospace;
  font-variant-numeric: tabular-nums;
}
.score .dash { color: var(--text-dim); font-weight: 400; }
.score .digit { color: var(--lose); min-width: 1.5ch; text-align: center; padding: 0.08rem 0; }
.score .digit.winner { color: var(--win-ink); background: var(--win-box); border-radius: 4px; padding: 0.08rem 0.4rem; }
.ticket-meta {
  display: flex; align-items: center; flex-wrap: wrap; gap: 0.5rem;
  margin-top: auto; font-size: 0.78rem; color: var(--text-dim);
}
.dot-sep { opacity: 0.6; }
.when { font-variant-numeric: tabular-nums; }
.twitch-link { color: var(--accent); text-decoration: none; font-weight: 600; }
.twitch-link:hover { text-decoration: underline; }
.result-link { color: var(--text-dim); text-decoration: none; font-weight: 600; }
.result-link:hover { color: var(--accent); text-decoration: underline; }
.badge {
  margin-left: auto; display: inline-flex; align-items: center; gap: 0.35rem;
  font: 700 0.68rem/1 -apple-system, sans-serif; text-transform: uppercase; letter-spacing: 0.06em;
  padding: 0.25rem 0.55rem; border-radius: 999px;
}
.badge-live { background: var(--live); color: #fff; position: absolute; top: 0.7rem; right: 0.85rem; margin-left: 0; }
.badge-live .dot { width: 6px; height: 6px; border-radius: 50%; background: #fff; animation: pulse 1.6s ease-in-out infinite; }
@media (prefers-reduced-motion: reduce) { .badge-live .dot { animation: none; } }
@keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.35; } }
.badge-done { background: var(--bg-wash); color: var(--text-dim); border: 1px solid var(--border); }
.badge-drops { background: var(--accent); color: var(--accent-ink); }
.ticket-row-broadcast { display: block; }
.broadcast-title {
  font: 700 0.98rem/1.3 "Segoe UI", -apple-system, "Arial Narrow", sans-serif;
  font-stretch: condensed; letter-spacing: 0.01em;
}
.lobby-title {
  font: 700 0.95rem/1.3 "Segoe UI", -apple-system, "Arial Narrow", sans-serif;
  font-stretch: condensed; letter-spacing: 0.01em;
}
.lobby-places { margin-top: 0.4rem; font-size: 0.82rem; display: flex; flex-wrap: wrap; gap: 0.4rem; align-items: baseline; }
.lobby-place { font-weight: 600; }
.lobby-more, .empty-inline { color: var(--text-dim); }
.dfpl-intro { background: var(--card); border: 1px solid var(--border); border-radius: 8px; padding: 1rem 1.2rem; margin-bottom: 1.25rem; }
.dfpl-intro h2 { margin-bottom: 0.5rem; }
.dfpl-note { margin: 0; max-width: 200ch; }
.section-rise { position: sticky; top: 1rem; margin-top: 15px; min-width: 0; margin-bottom: 0; }
.section-rise h2 { color: var(--accent); }
.section-dfpl h2 { color: var(--accent); }
.rise-grid { display: flex; flex-direction: column; gap: 1rem; }
.standings-table { width: 100%; border-collapse: collapse; background: var(--card); border: 1px solid var(--border); border-radius: 8px; overflow: hidden; }
.standings-table th, .standings-table td { padding: 0.5rem 0.7rem; text-align: left; font-size: 0.85rem; }
.standings-table thead th {
  font: 700 0.68rem/1 -apple-system, sans-serif; text-transform: uppercase; letter-spacing: 0.06em;
  color: var(--text-dim); background: var(--bg-wash); border-bottom: 1px solid var(--border);
}
.standings-table tbody tr + tr { border-top: 1px solid var(--border); }
.standings-rank { font-variant-numeric: tabular-nums; color: var(--text-dim); width: 2ch; }
.standings-team { display: flex; align-items: center; gap: 0.5rem; font-weight: 700; }
.team-logo-sm { width: 22px; height: 22px; object-fit: contain; border-radius: 5px; background: var(--bg-wash); padding: 2px; flex: none; }
.standings-points { font-variant-numeric: tabular-nums; font-weight: 700; text-align: right; }
.side-panel { background: var(--card); border: 1px solid var(--border); border-radius: 8px; padding: 1rem 1.1rem; }
.side-panel h3 {
  font: 700 0.72rem/1 -apple-system, "Segoe UI", sans-serif; text-transform: uppercase;
  letter-spacing: 0.08em; color: var(--text-dim); margin: 0 0 0.75rem;
}
.mvp-list { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 0.55rem; }
.mvp-row { display: flex; align-items: center; gap: 0.5rem; font-size: 0.82rem; }
.mvp-avatar { width: 26px; height: 26px; object-fit: cover; border-radius: 50%; background: var(--bg-wash); flex: none; }
.mvp-name { font-weight: 700; }
.mvp-team { color: var(--text-dim); }
.mvp-count { margin-left: auto; font-variant-numeric: tabular-nums; color: var(--accent); font-weight: 700; }
.section-rewards h2 { color: var(--accent); }
.rewards-campaign { text-align: center; color: var(--text-dim); font-size: 0.82rem; margin: -0.4rem 0 0.85rem; }
.rewards-grid { display: flex; flex-wrap: wrap; justify-content: center; gap: 0.6rem; }
.reward-card {
  background: var(--card); border: 1px solid var(--border); border-radius: 8px;
  padding: 0.65rem; display: flex; flex-direction: column; align-items: center;
  text-align: center; gap: 0.3rem; width: 130px;
}
.reward-img { width: 56px; height: 56px; object-fit: contain; border-radius: 6px; background: var(--bg-wash); padding: 0.25rem; }
.reward-name { font: 700 0.82rem/1.25 "Segoe UI", -apple-system, sans-serif; }
.reward-time { font: 700 0.7rem/1 -apple-system, sans-serif; color: var(--accent); text-transform: uppercase; letter-spacing: 0.03em; }
.reward-campaign { font-size: 0.7rem; color: var(--text-dim); }
.empty { color: var(--text-dim); font-size: 0.88rem; padding: 1rem; border: 1px dashed var(--border); border-radius: 4px; background: var(--bg-wash); }
footer {
  margin-top: 3rem; padding-top: 1.5rem; border-top: 1px solid var(--border);
  color: var(--text-dim); font-size: 0.76rem; display: flex; justify-content: space-between; flex-wrap: wrap; gap: 0.5rem;
}
a { color: inherit; }
"""


def render_nav(active):
    def link(href, label, key):
        cls = "nav-link active" if key == active else "nav-link"
        return f'<a class="{cls}" href="{href}">{label}</a>'
    return f"""
<nav class="site-nav">
  <span class="site-nav-title">Delta Ops Board</span>
  <div class="site-nav-links">
    {link("index.html", "Warfare", "warfare")}
    {link("operations.html", "Operations", "operations")}
    {link("dfpl.html", "DFPL", "dfpl")}
  </div>
</nav>"""


def build_page(active, title, eyebrow, subtitle, sections, sources_line, generated_at, sidebar_html=None, sidebar_mode="double"):
    generated_str = generated_at.astimezone(LOCAL_TZ).strftime("%a %d %b %Y, %H:%M (Paris time)")
    if sidebar_html:
        layout_class = "layout sidebar-double" if sidebar_mode == "double" else "layout sidebar-single"
    else:
        layout_class = "layout no-sidebar"
    sidebar = f'<aside class="sidebar">{sidebar_html}</aside>' if sidebar_html else ""
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{e(title)}</title>
<link rel="icon" type="image/png" href="favicon.png">
<style>{PAGE_STYLE}</style>
</head>
<body>
{render_nav(active)}
<div class="{layout_class}">
<main>
  <header>
    <p class="eyebrow">{e(eyebrow)}</p>
    <h1>{e(title)}</h1>
    <p class="subtitle">{e(subtitle)}</p>
  </header>
  {sections}
  <footer>
    <span>Updated {e(generated_str)}</span>
    <span>Sources: {e(sources_line)}</span>
  </footer>
</main>
{sidebar}
</div>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Page builders
# ---------------------------------------------------------------------------

def build_warfare_html(matches, generated_at, twitch_info=None, broadcasts=None, drops_channels=None, team_links=None, rewards=None, campaign_label=None):
    twitch_info = twitch_info or {}
    broadcasts = broadcasts or []
    drops_channels = drops_channels or set()
    team_links = team_links or {}
    rewards = rewards or []

    warfare_matches = [m for m in matches if m.get("mode") == "warfare" and m.get("format") == "head_to_head"]
    now = time.time()
    live = [m for m in warfare_matches if m.get("live")]
    upcoming = [m for m in warfare_matches if not m.get("live") and not m.get("finished") and m["timestamp"] > now]
    completed = [m for m in warfare_matches if m.get("finished")]
    upcoming = [m for m in upcoming if m["timestamp"] <= now + UPCOMING_WINDOW_DAYS * 86400]
    # BRACKET_PAGES is a small, hand-curated list (see sources.py), not the
    # noisy broad ticker - so bracket-sourced results stay visible however
    # old they are (until the page is removed from BRACKET_PAGES) rather
    # than falling out of a rolling results window.
    completed = [
        m for m in completed
        if m.get("source") == "bracket" or m["timestamp"] >= now - RESULTS_WINDOW_DAYS * 86400
    ]

    live_sorted = sorted(live, key=lambda m: m["timestamp"])
    upcoming_sorted = sorted(upcoming, key=lambda m: m["timestamp"])
    completed_sorted = sorted(completed, key=lambda m: m["timestamp"], reverse=True)

    def has_drops(match):
        return (match.get("twitch_channel") or "").lower() in drops_channels

    default_channel = TWITCH_CHANNELS[0] if TWITCH_CHANNELS else None

    def with_fallback_channel(match):
        if match.get("twitch_channel"):
            return match
        return {**match, "twitch_channel": default_channel}

    live_sorted = [with_fallback_channel(m) for m in live_sorted]

    live_rows = [render_match_row(m, "live", drops=has_drops(m), team_links=team_links) for m in live_sorted]
    live_rows += [render_broadcast_row(b) for b in broadcasts]
    upcoming_rows = [render_match_row(m, "upcoming", drops=has_drops(m), team_links=team_links) for m in upcoming_sorted]
    completed_rows = [render_match_row(m, "completed", team_links=team_links) for m in completed_sorted]

    sections = ""
    if live_rows:
        sections += render_section("Live now", live_rows, "live", "")
    sections += render_rewards_section(rewards, campaign_label=campaign_label)
    sections += render_section(f"Upcoming (next {int(UPCOMING_WINDOW_DAYS)} days)", upcoming_rows, "upcoming", "No further matches scheduled in this window.")
    # Not labeled with a day count like the other windowed sections - this
    # list mixes rolling-window ticker results with indefinitely-shown
    # bracket results (see the `source == "bracket"` filter above).
    sections += render_section("Results", completed_rows, "completed", "No results yet.")

    return build_page(
        "warfare", "Delta Force · Warfare", "Ops Board",
        "Live status, upcoming matches and recent results for the Warfare track (traditional team-vs-team format), pulled from Liquipedia.",
        sections, "Liquipedia, twitchdrops.app, playdeltaforce.com (drops calendar)", generated_at,
    )


def build_operations_html(matches, rise_data_by_region, generated_at, rewards=None, campaign_label=None):
    rewards = rewards or []
    now = time.time()

    scene_results = [m for m in matches if m.get("mode") == "operations" and m.get("format") == "lobby_result"]
    scene_results = [m for m in scene_results if m["timestamp"] >= now - RESULTS_WINDOW_DAYS * 86400]
    scene_results_sorted = sorted(scene_results, key=lambda m: m["timestamp"], reverse=True)
    sections = render_rewards_section(rewards, campaign_label=campaign_label)
    sections += render_day_grouped_section(
        f"Recent results across the scene (last {int(RESULTS_WINDOW_DAYS)} days)",
        scene_results_sorted, render_lobby_result_row, "completed",
        "No recent results.",
    )

    sidebar_html = "".join(
        render_rise_series_section(region_label, rise_data_by_region.get(region_key))
        for region_key, region_label in RISE_REGIONS
    )

    return build_page(
        "operations", "Delta Force · Operations", "Ops Board",
        "RISE Series standings and scene-wide results for the Operations track (extraction-shooter, lobby/points format), pulled from Liquipedia and TiMi's own RISE Series backend.",
        sections, "Liquipedia, playdeltaforce.com (RISE Series + drops calendar), twitchdrops.app", generated_at,
        sidebar_html=sidebar_html, sidebar_mode="double",
    )


def build_dfpl_html(data, generated_at):
    sidebar_html = None
    if not data:
        sections = '<p class="empty">DFPL data unavailable.</p>'
    else:
        team_map = data["team_map"]
        now = time.time()

        schedule_sorted = sorted(data["schedule"], key=lambda s: int(s["start_timestamp"]), reverse=True)
        finished = [s for s in schedule_sorted if s.get("schedule_status") == 4]
        upcoming = sorted(
            (s for s in data["schedule"] if s.get("schedule_status") == 1 and s.get("team_list") and int(s["start_timestamp"]) > now),
            key=lambda s: int(s["start_timestamp"]),
        )

        sections = f"""
    <section class="section-dfpl">
      <div class="dfpl-intro">
        <h2>{e(sources.dfpl_season_title(data["season_id"]))}</h2>
        <p class="empty-inline dfpl-note">Ranked by win count - DFPL's own API doesn't expose a points/standings field (see README). Team names shown as their short code; player names are their Latin handle where one exists in the roster data, otherwise the original Chinese nickname.</p>
      </div>
      {render_dfpl_standings_table(data["team_ranks"], team_map)}
    </section>"""

        sidebar_html = render_dfpl_player_panel(data["player_ranks"], team_map)
        if upcoming:
            sidebar_html += render_section(
                "Upcoming lobbies", [render_dfpl_schedule_card(s, team_map) for s in upcoming[:RISE_LOBBIES_SHOWN]],
                "upcoming", "",
            )
        sidebar_html += render_section(
            "Recent lobbies", [render_dfpl_schedule_card(s, team_map) for s in finished[:RISE_LOBBIES_SHOWN]],
            "completed", "No results yet.",
        )

    return build_page(
        "dfpl", "Delta Force · DFPL", "Ops Board",
        "DFPL (烽火职业联赛) team and player stats for China's domestic Operations-mode league, pulled from Tencent's own DFPL backend.",
        sections, "df.qq.com (DFPL)", generated_at,
        sidebar_html=sidebar_html, sidebar_mode="single",
    )


def build_and_commit():
    log.info("building site")
    ensure_repo()

    matches, team_links = sources.gather_all_matches()
    now = time.time()

    warfare_matches = [m for m in matches if m.get("mode") == "warfare" and m.get("format") == "head_to_head"]
    warfare_live = [m for m in warfare_matches if m.get("live")]
    warfare_upcoming = [m for m in warfare_matches if not m.get("live") and not m.get("finished") and m["timestamp"] > now]
    event_active = bool(warfare_live) or any(m["timestamp"] <= now + ACTIVE_LOOKAHEAD_HOURS * 3600 for m in warfare_upcoming)

    rewards, drops_channels = [], set()
    try:
        rewards, drops_channels = sources.fetch_active_drops()
    except Exception:
        log.exception("twitch drops fetch failed")

    campaign_label = None
    try:
        current_campaigns = sources.current_drops_campaigns(sources.fetch_drops_calendar(), now=now)
        campaign_label = current_campaign_label(current_campaigns)
    except Exception:
        log.exception("drops calendar fetch failed")

    rise_data_by_region = {}
    for region_key, region_label in RISE_REGIONS:
        try:
            data = sources.gather_rise_series_data(region_key)
            data["mvp"] = sources.fetch_mvp_ranking(data["season_id"])
            rise_data_by_region[region_key] = data
        except Exception:
            log.exception("RISE Series %s fetch failed", region_label)
            rise_data_by_region[region_key] = None

    dfpl_data = None
    try:
        dfpl_data = sources.gather_dfpl_data()
    except Exception:
        log.exception("DFPL fetch failed")

    twitch_info, broadcasts = {}, []
    if TWITCH_CLIENT_ID and TWITCH_CLIENT_SECRET:
        channels_to_check = set(TWITCH_CHANNELS)
        channels_to_check.update(m["twitch_channel"].lower() for m in matches if m.get("twitch_channel"))
        try:
            twitch_info = sources.fetch_twitch_live_info(TWITCH_CLIENT_ID, TWITCH_CLIENT_SECRET, channels_to_check)
        except Exception:
            log.exception("twitch live info fetch failed")

        matched_channels = {m["twitch_channel"].lower() for m in warfare_live if m.get("twitch_channel")}
        broadcasts = [
            {"channel": channel, "has_drops": channel in drops_channels, **info}
            for channel, info in twitch_info.items()
            if channel not in matched_channels
        ]

    generated_at = datetime.now(tz=timezone.utc)
    warfare_page = build_warfare_html(
        matches, generated_at, twitch_info=twitch_info, broadcasts=broadcasts,
        drops_channels=drops_channels, team_links=team_links, rewards=rewards,
        campaign_label=campaign_label,
    )
    operations_page = build_operations_html(matches, rise_data_by_region, generated_at, rewards=rewards, campaign_label=campaign_label)
    dfpl_page = build_dfpl_html(dfpl_data, generated_at)

    docs_dir = os.path.join(CLONE_DIR, DOCS_SUBDIR)
    os.makedirs(docs_dir, exist_ok=True)
    with open(os.path.join(docs_dir, "index.html"), "w", encoding="utf-8") as f:
        f.write(warfare_page)
    with open(os.path.join(docs_dir, "operations.html"), "w", encoding="utf-8") as f:
        f.write(operations_page)
    with open(os.path.join(docs_dir, "dfpl.html"), "w", encoding="utf-8") as f:
        f.write(dfpl_page)
    nojekyll = os.path.join(docs_dir, ".nojekyll")
    if not os.path.exists(nojekyll):
        open(nojekyll, "w").close()

    run_git(["add", DOCS_SUBDIR])
    status = run_git(["status", "--porcelain", "--", DOCS_SUBDIR])
    if not status.stdout.strip():
        log.info("no changes, skipping commit")
        return event_active

    run_git(["commit", "-m", f"Update site {generated_at.isoformat()}"])
    run_git(["push", "origin", BRANCH])
    log.info("pushed site update")
    return event_active


def safe_build_and_commit():
    try:
        return build_and_commit()
    except Exception:
        log.exception("site build failed, will retry sooner")
        return True  # assume the worst so we retry at the short interval, not the 24h one


def _seconds_until_next_local_midnight():
    now = datetime.now(tz=LOCAL_TZ)
    next_midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return (next_midnight - now).total_seconds()


def main():
    log.info(
        "delta-site starting, active interval=%d min, idle: daily at local midnight (capped at %d min) (event window: next %g h)",
        BUILD_INTERVAL_MINUTES, IDLE_BUILD_INTERVAL_MINUTES, ACTIVE_LOOKAHEAD_HOURS,
    )

    event_active = True
    if RUN_ON_START:
        event_active = safe_build_and_commit()

    while True:
        if event_active:
            sleep_seconds = BUILD_INTERVAL_MINUTES * 60
            log.info("next build in %d min (event window)", BUILD_INTERVAL_MINUTES)
        else:
            # A rolling "sleep N hours" idle interval drifts and never lands
            # on a predictable time - anchoring to local midnight instead
            # gives a real daily refresh, same spirit as r6-notifier's fixed
            # CHECK_TIMES. IDLE_BUILD_INTERVAL_MINUTES still caps the wait
            # (relevant right after midnight, when it's otherwise ~24h away).
            sleep_seconds = min(_seconds_until_next_local_midnight(), IDLE_BUILD_INTERVAL_MINUTES * 60)
            log.info("next build in %.0f min (idle, next local midnight or cap)", sleep_seconds / 60)
        time.sleep(sleep_seconds)
        event_active = safe_build_and_commit()


if __name__ == "__main__":
    main()
