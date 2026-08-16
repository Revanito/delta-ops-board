import json
import logging
import os
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import requests
import schedule

import sources

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("r6-notifier")

DISCORD_WEBHOOK_URL = os.environ["DISCORD_WEBHOOK_URL"]
TWITCH_CLIENT_ID = os.environ["TWITCH_CLIENT_ID"]
TWITCH_CLIENT_SECRET = os.environ["TWITCH_CLIENT_SECRET"]

TWITCH_CHANNELS = [c.strip().lower() for c in os.environ.get("TWITCH_CHANNELS", "rainbow6").split(",") if c.strip()]
LOOKAHEAD_HOURS = float(os.environ.get("LOOKAHEAD_HOURS", "30"))
CHECK_TIMES = [t.strip() for t in os.environ.get("CHECK_TIMES", "08:00,20:00").split(",") if t.strip()]
TZ_NAME = os.environ.get("TZ_NAME", "Europe/Paris")
LOCAL_TZ = ZoneInfo(TZ_NAME)  # for formatting display timestamps
STATE_FILE = os.environ.get("STATE_FILE", "/data/state.json")
RUN_ON_START = os.environ.get("RUN_ON_START", "false").lower() == "true"
MATCH_STATE_MAX_AGE_DAYS = 4


def load_state():
    if not os.path.exists(STATE_FILE):
        return {"live_channels": {}, "notified_matches": {}}
    with open(STATE_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_state(state):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


def prune_notified_matches(state):
    cutoff = time.time() - MATCH_STATE_MAX_AGE_DAYS * 86400
    state["notified_matches"] = {
        k: v for k, v in state["notified_matches"].items() if v > cutoff
    }


def send_discord(content):
    resp = requests.post(DISCORD_WEBHOOK_URL, json={"content": content}, timeout=15)
    resp.raise_for_status()


def check_twitch_live(state, drops_channels, extra_channels=None):
    channels = list(dict.fromkeys(TWITCH_CHANNELS + (extra_channels or [])))  # de-duplicate, keep order
    live_now = sources.fetch_twitch_live_info(TWITCH_CLIENT_ID, TWITCH_CLIENT_SECRET, channels)

    previously_live = state.get("live_channels", {})

    messages = []
    for channel in channels:
        is_live = channel in live_now
        was_live = previously_live.get(channel, False)
        if is_live and not was_live:
            info = live_now[channel]
            messages.append(
                f"🔴 **twitch.tv/{channel}** just went live\n"
                f"{info['title']}" + (f" — playing *{info['game']}*" if info["game"] else "") +
                ("\n🎁 Drops enabled" if channel in drops_channels else "") +
                f"\nhttps://www.twitch.tv/{channel}"
            )
        previously_live[channel] = is_live

    state["live_channels"] = previously_live
    return messages


def check_upcoming_matches(state, matches, drops_channels):
    now = time.time()
    horizon = now + LOOKAHEAD_HOURS * 3600
    notified = state.setdefault("notified_matches", {})
    default_channel = TWITCH_CHANNELS[0] if TWITCH_CHANNELS else None

    messages = []
    for match in sorted(matches, key=lambda m: m["timestamp"]):
        if not (now <= match["timestamp"] <= horizon):
            continue
        if match["key"] in notified:
            continue

        local_dt = datetime.fromtimestamp(match["timestamp"], tz=timezone.utc).astimezone(LOCAL_TZ)
        when = local_dt.strftime("%a %d %b, %H:%M") + " (Paris time)"
        line = f"🎮 **{match['teams'][0]} vs {match['teams'][1]}** — {match['tournament']}\n🗓️ {when}"

        channel = match["twitch_channel"] or default_channel
        if channel:
            line += f"\n📺 https://www.twitch.tv/{channel}"
            if channel.lower() in drops_channels:
                line += "\n🎁 Drops enabled"

        messages.append(line)
        notified[match["key"]] = now

    prune_notified_matches(state)
    return messages


def run_check():
    log.info("running check")
    state = load_state()
    all_messages = []

    matches, ubi_live_channels, _team_links = sources.gather_all_matches()

    drops_channels = set()
    try:
        _, drops_channels = sources.fetch_active_drops()
    except Exception:
        log.exception("twitch drops fetch failed")

    try:
        all_messages.extend(check_twitch_live(state, drops_channels, extra_channels=ubi_live_channels))
    except Exception:
        log.exception("twitch live check failed")

    try:
        all_messages.extend(check_upcoming_matches(state, matches, drops_channels))
    except Exception:
        log.exception("schedule check failed")

    save_state(state)

    if all_messages:
        send_discord("**R6 esports update**\n\n" + "\n\n".join(all_messages))
        log.info("sent %d update(s) to discord", len(all_messages))
    else:
        log.info("nothing new")


def main():
    log.info("r6-notifier starting, channels=%s, check times=%s (%s)", TWITCH_CHANNELS, CHECK_TIMES, LOCAL_TZ)

    for t in CHECK_TIMES:
        schedule.every().day.at(t, TZ_NAME).do(run_check)

    if RUN_ON_START:
        run_check()

    while True:
        schedule.run_pending()
        time.sleep(30)


if __name__ == "__main__":
    main()
