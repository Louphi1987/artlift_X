# -*- coding: utf-8 -*-
"""
ArtLift Bluesky bot.

Goals:
- support artists softly, without spam
- like only a small number of posts per week
- repost a few art posts per week, including Loufisart
- follow only from time to time, with strong safeguards
- keep weekly state so the bot stays light even when automation runs often
"""

import json
import os
import random
import sys
import time
from datetime import datetime, time as dtime
from pathlib import Path

from atproto import Client
from dateutil import tz
from dateutil import parser as dtparser


# -------------------- Secrets / env --------------------
BSKY_HANDLE = os.getenv("BSKY_HANDLE", "").strip()
BSKY_APP_PASSWORD = os.getenv("BSKY_APP_PASSWORD", "").strip()


# -------------------- Main target --------------------
LOUFIS_HANDLE = os.getenv("ARTLIFT_LOUFIS_HANDLE", "loufisart.bsky.social").strip().lower()


# -------------------- Runtime config --------------------
STATE_PATH = Path(os.getenv("ARTLIFT_STATE_PATH", ".artlift/state.json"))
FORCE_RUN = os.getenv("ARTLIFT_FORCE_RUN", "").strip().lower() in {"1", "true", "yes", "on"}


def env_int(name, default):
    raw = os.getenv(name)
    if raw in (None, ""):
        return default
    try:
        return int(raw)
    except ValueError:
        print(f"[WARN] Invalid int for {name}={raw!r}; using {default}.")
        return default


def env_float(name, default):
    raw = os.getenv(name)
    if raw in (None, ""):
        return default
    try:
        return float(raw)
    except ValueError:
        print(f"[WARN] Invalid float for {name}={raw!r}; using {default}.")
        return default


# Weekly caps: intentionally conservative.
WEEKLY_LIKE_QUOTA = env_int("ARTLIFT_WEEKLY_LIKE_QUOTA", 14)
WEEKLY_ARTIST_REPOST_QUOTA = env_int("ARTLIFT_WEEKLY_ARTIST_REPOST_QUOTA", 3)
WEEKLY_LOUFIS_REPOST_QUOTA = env_int("ARTLIFT_WEEKLY_LOUFIS_REPOST_QUOTA", 2)
WEEKLY_FOLLOW_QUOTA = env_int("ARTLIFT_WEEKLY_FOLLOW_QUOTA", 1)

# Per-run behavior.
MORNING_LIKES_RANGE = (
    env_int("ARTLIFT_MIN_LIKES_PER_RUN", 1),
    env_int("ARTLIFT_MAX_LIKES_PER_RUN", 3),
)
MAX_TIMELINE_SCAN = env_int("ARTLIFT_MAX_TIMELINE_SCAN", 80)
MAX_FOLLOWS_TO_SCAN = env_int("ARTLIFT_MAX_FOLLOWS_TO_SCAN", 180)
MAX_FOLLOWERS_TO_SCAN = env_int("ARTLIFT_MAX_FOLLOWERS_TO_SCAN", 180)
DISCOVERY_SAMPLE_SIZE = env_int("ARTLIFT_DISCOVERY_SAMPLE_SIZE", 120)
RECENT_IMAGE_DAYS = env_int("ARTLIFT_RECENT_IMAGE_DAYS", 21)
MAX_ARTIST_IMAGE_TRIES = env_int("ARTLIFT_MAX_ARTIST_IMAGE_TRIES", 8)
FOLLOW_ATTEMPT_PROBABILITY = env_float("ARTLIFT_FOLLOW_ATTEMPT_PROBABILITY", 0.35)
MIN_DAYS_BETWEEN_FOLLOWS = env_int("ARTLIFT_MIN_DAYS_BETWEEN_FOLLOWS", 10)

# History bounds.
MAX_LIKED_HISTORY = env_int("ARTLIFT_MAX_LIKED_HISTORY", 500)
MAX_REPOST_HISTORY = env_int("ARTLIFT_MAX_REPOST_HISTORY", 300)
MAX_FOLLOW_HISTORY = env_int("ARTLIFT_MAX_FOLLOW_HISTORY", 300)


# -------------------- Artist heuristics --------------------
ART_KEYWORDS = {
    "art",
    "artist",
    "artiste",
    "illustrator",
    "illustration",
    "painter",
    "painting",
    "peintre",
    "draw",
    "drawing",
    "dessin",
    "dessinateur",
    "dessinatrice",
    "comic",
    "bd",
    "manga",
    "photography",
    "photographer",
    "photo",
    "photographie",
    "3d",
    "cgi",
    "digital art",
    "concept art",
    "sculpt",
    "sculpture",
    "pixel art",
    "motion",
    "visual",
    "graphiste",
    "graphic",
    "designer",
    "watercolor",
    "aquarelle",
    "ceramic",
    "ceramics",
    "printmaker",
    "engraving",
    "tattoo artist",
}


# -------------------- Utilities --------------------
def human_sleep(a=0.8, b=2.0):
    time.sleep(random.uniform(a, b))


def clamp_range(low, high):
    return (min(low, high), max(low, high))


MORNING_LIKES_RANGE = clamp_range(*MORNING_LIKES_RANGE)


def normalize_handle(handle):
    return (handle or "").strip().lower()


def getattr_any(obj, *names, default=None):
    for name in names:
        if hasattr(obj, name):
            return getattr(obj, name)
    return default


def chunked(items, size):
    batch = []
    for item in items:
        batch.append(item)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch


def append_capped(items, value, max_size):
    if value in items:
        items.remove(value)
    items.append(value)
    if len(items) > max_size:
        del items[:-max_size]


def now_brussels():
    brussels = tz.gettz("Europe/Brussels")
    return datetime.utcnow().replace(tzinfo=tz.UTC).astimezone(brussels)


def is_evening_brussels(dt):
    start = dtime(19, 0)
    end = dtime(22, 0)
    return start <= dt.time() < end


def is_morning_brussels(dt):
    start = dtime(7, 0)
    end = dtime(11, 0)
    return start <= dt.time() < end


def current_week_key(dt):
    iso = dt.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def slot_key(mode, dt):
    return f"{dt.date().isoformat()}::{mode}"


def parse_dt(value):
    if not value:
        return None
    try:
        return dtparser.parse(value)
    except Exception:
        return None


def days_since(value, ref_dt):
    parsed = parse_dt(value)
    if not parsed:
        return None
    return (ref_dt - parsed).days


# -------------------- Persistent state --------------------
def default_state():
    return {
        "week_key": None,
        "weekly": {},
        "history": {
            "liked_posts": [],
            "reposted_posts": [],
            "followed_dids": [],
            "followed_handles": [],
            "last_follow_at": None,
        },
        "run_slots": {},
    }


def fresh_weekly_state():
    return {
        "likes": 0,
        "artist_reposts": 0,
        "loufis_reposts": 0,
        "follows": 0,
    }


def load_state():
    if not STATE_PATH.exists():
        return default_state()
    try:
        raw = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"[WARN] Could not read state file: {exc}. Starting fresh.")
        return default_state()

    state = default_state()
    if isinstance(raw, dict):
        state.update({k: v for k, v in raw.items() if k in state})
        if not isinstance(state.get("weekly"), dict):
            state["weekly"] = fresh_weekly_state()
        for key, value in fresh_weekly_state().items():
            state["weekly"].setdefault(key, value)
        if not isinstance(state.get("history"), dict):
            state["history"] = default_state()["history"]
        for key, value in default_state()["history"].items():
            state["history"].setdefault(key, value)
        if not isinstance(state.get("run_slots"), dict):
            state["run_slots"] = {}
    return state


def save_state(state):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(
        json.dumps(state, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def ensure_current_week(state, dt):
    wk = current_week_key(dt)
    if state.get("week_key") != wk:
        state["week_key"] = wk
        state["weekly"] = fresh_weekly_state()
        state["run_slots"] = {}
    return state


def weekly_count(state, key):
    return int(state.get("weekly", {}).get(key, 0) or 0)


def weekly_remaining(state, key, quota):
    return max(0, quota - weekly_count(state, key))


def mark_weekly_action(state, key):
    state.setdefault("weekly", {})
    state["weekly"][key] = weekly_count(state, key) + 1


def mark_slot_done(state, mode, dt):
    state.setdefault("run_slots", {})
    state["run_slots"][slot_key(mode, dt)] = dt.isoformat()


def slot_already_done(state, mode, dt):
    return slot_key(mode, dt) in state.get("run_slots", {})


def remember_like(state, uri):
    items = state.setdefault("history", {}).setdefault("liked_posts", [])
    append_capped(items, uri, MAX_LIKED_HISTORY)
    mark_weekly_action(state, "likes")


def remember_repost(state, uri, bucket):
    items = state.setdefault("history", {}).setdefault("reposted_posts", [])
    append_capped(items, uri, MAX_REPOST_HISTORY)
    mark_weekly_action(state, bucket)


def remember_follow(state, did, handle, dt):
    history = state.setdefault("history", {})
    if did:
        append_capped(history.setdefault("followed_dids", []), did, MAX_FOLLOW_HISTORY)
    if handle:
        append_capped(
            history.setdefault("followed_handles", []),
            normalize_handle(handle),
            MAX_FOLLOW_HISTORY,
        )
    history["last_follow_at"] = dt.isoformat()
    mark_weekly_action(state, "follows")


def history_contains_uri(state, uri):
    return uri in state.get("history", {}).get("liked_posts", []) or uri in state.get("history", {}).get(
        "reposted_posts", []
    )


def already_followed_before(state, did, handle):
    history = state.get("history", {})
    if did and did in history.get("followed_dids", []):
        return True
    if normalize_handle(handle) in history.get("followed_handles", []):
        return True
    return False


# -------------------- Bluesky API wrappers --------------------
def login_client():
    if not BSKY_HANDLE or not BSKY_APP_PASSWORD:
        print("ERROR: Set env vars BSKY_HANDLE and BSKY_APP_PASSWORD.", file=sys.stderr)
        sys.exit(1)
    client = Client()
    client.login(BSKY_HANDLE, BSKY_APP_PASSWORD)
    return client


def get_timeline_posts(client, limit=50):
    try:
        feed = client.get_timeline(limit=limit)
    except AttributeError:
        feed = client.app.bsky.feed.get_timeline(limit=limit)
    return getattr(feed, "feed", []) or []


def like_post(client, uri, cid):
    try:
        client.like(uri, cid)
        return True
    except Exception as exc:
        print(f"[WARN] like failed: {exc}")
        return False


def repost_post(client, uri, cid):
    try:
        client.repost(uri, cid)
        return True
    except Exception as exc:
        print(f"[WARN] repost failed: {exc}")
        return False


def follow_actor(client, actor):
    try:
        client.follow(actor)
        return True
    except Exception as exc:
        print(f"[WARN] follow failed: {exc}")
        return False


def get_author_feed(client, handle, limit=30, feed_filter=None):
    try:
        kwargs = {"actor": handle, "limit": limit}
        if feed_filter:
            kwargs["filter"] = feed_filter
        try:
            feed = client.get_author_feed(**kwargs)
        except AttributeError:
            feed = client.app.bsky.feed.get_author_feed(**kwargs)
        return getattr(feed, "feed", []) or []
    except Exception as exc:
        print(f"[WARN] get_author_feed({handle}) failed: {exc}")
        return []


def get_profile(client, handle):
    try:
        try:
            return client.get_profile(handle)
        except AttributeError:
            return client.app.bsky.actor.get_profile(actor=handle)
    except Exception as exc:
        print(f"[WARN] get_profile({handle}) failed: {exc}")
        return None


def get_profiles(client, handles):
    if not handles:
        return []
    out = []
    for batch in chunked(handles, 25):
        try:
            try:
                res = client.get_profiles(actors=batch)
            except AttributeError:
                res = client.app.bsky.actor.get_profiles(actors=batch)
            profiles = getattr(res, "profiles", []) or []
            out.extend(profiles)
        except Exception as exc:
            print(f"[WARN] get_profiles batch failed: {exc}")
            for handle in batch:
                profile = get_profile(client, handle)
                if profile:
                    out.append(profile)
        if random.random() < 0.15:
            human_sleep(0.4, 0.9)
    return out


def list_handles_from_follows(client, handle, max_count):
    out = []
    cursor = None
    while len(out) < max_count:
        try:
            try:
                res = client.get_follows(actor=handle, limit=100, cursor=cursor)
            except AttributeError:
                res = client.app.bsky.graph.get_follows(actor=handle, limit=100, cursor=cursor)
        except Exception as exc:
            print(f"[WARN] get_follows({handle}) failed: {exc}")
            break
        for user in getattr(res, "follows", []) or []:
            candidate = normalize_handle(getattr_any(user, "handle"))
            if candidate and candidate != normalize_handle(handle):
                out.append(candidate)
                if len(out) >= max_count:
                    break
        cursor = getattr_any(res, "cursor")
        if not cursor or len(out) >= max_count:
            break
    return out


def list_handles_from_followers(client, handle, max_count):
    out = []
    cursor = None
    while len(out) < max_count:
        try:
            try:
                res = client.get_followers(actor=handle, limit=100, cursor=cursor)
            except AttributeError:
                res = client.app.bsky.graph.get_followers(actor=handle, limit=100, cursor=cursor)
        except Exception as exc:
            print(f"[WARN] get_followers({handle}) failed: {exc}")
            break
        for user in getattr(res, "followers", []) or []:
            candidate = normalize_handle(getattr_any(user, "handle"))
            if candidate and candidate != normalize_handle(handle):
                out.append(candidate)
                if len(out) >= max_count:
                    break
        cursor = getattr_any(res, "cursor")
        if not cursor or len(out) >= max_count:
            break
    return out


# -------------------- Post / profile helpers --------------------
def profile_handle(profile):
    return normalize_handle(getattr_any(profile, "handle"))


def profile_did(profile):
    return getattr_any(profile, "did")


def profile_viewer(profile):
    return getattr_any(profile, "viewer")


def profile_display_name(profile):
    return getattr_any(profile, "display_name", "displayName", default="")


def profile_description(profile):
    return getattr_any(profile, "description", default="")


def post_record(post_view):
    return getattr_any(post_view, "record")


def post_created_at(post_view):
    record = post_record(post_view)
    return getattr_any(record, "created_at", "createdAt")


def post_is_reply(feed_item):
    record = post_record(feed_item.post)
    return bool(getattr_any(record, "reply"))


def feed_item_is_repost(feed_item):
    return getattr_any(feed_item, "reason") is not None


def viewer_like_uri(post_view):
    viewer = getattr_any(post_view, "viewer")
    return getattr_any(viewer, "like")


def viewer_repost_uri(post_view):
    viewer = getattr_any(post_view, "viewer")
    return getattr_any(viewer, "repost")


def viewer_following_uri(profile):
    return getattr_any(profile_viewer(profile), "following")


def viewer_blocking_uri(profile):
    return getattr_any(profile_viewer(profile), "blocking")


def viewer_muted(profile):
    return bool(getattr_any(profile_viewer(profile), "muted", default=False))


def post_view_has_image_embed(feed_item):
    embed = getattr_any(feed_item.post, "embed")
    if not embed:
        return False
    if getattr_any(embed, "images"):
        return True
    media = getattr_any(embed, "media")
    if media and getattr_any(media, "images"):
        return True
    return False


def post_is_recent(feed_item, within_days=RECENT_IMAGE_DAYS):
    created = post_created_at(feed_item.post)
    ts = parse_dt(created)
    if not ts:
        return False
    return (now_brussels() - ts).days <= within_days


def is_artist_like_profile(profile):
    if not profile:
        return False
    text = f"{profile_display_name(profile)} || {profile_description(profile)}".lower()
    if not text.strip():
        return False
    return any(keyword in text for keyword in ART_KEYWORDS)


def is_safe_profile_candidate(profile):
    if not profile:
        return False
    handle = profile_handle(profile)
    if not handle or handle == normalize_handle(BSKY_HANDLE):
        return False
    if viewer_blocking_uri(profile) or viewer_muted(profile):
        return False
    return True


def is_original_art_post(feed_item):
    try:
        if not post_view_has_image_embed(feed_item):
            return False
        if feed_item_is_repost(feed_item):
            return False
        if post_is_reply(feed_item):
            return False
        return True
    except Exception:
        return False


def eligible_like_item(feed_item, artist_handles, state):
    post = feed_item.post
    author = normalize_handle(getattr_any(getattr_any(post, "author"), "handle"))
    if not author or author == normalize_handle(BSKY_HANDLE):
        return False
    if artist_handles and author not in artist_handles and author != LOUFIS_HANDLE:
        return False
    if viewer_like_uri(post):
        return False
    if history_contains_uri(state, getattr_any(post, "uri")):
        return False
    return is_original_art_post(feed_item)


def eligible_repost_item(feed_item, state):
    post = feed_item.post
    if viewer_repost_uri(post):
        return False
    if history_contains_uri(state, getattr_any(post, "uri")):
        return False
    return is_original_art_post(feed_item)


def has_recent_image_post(client, handle, limit=12):
    feed = get_author_feed(client, handle, limit=limit, feed_filter="posts_with_media")
    for item in feed:
        if eligible_repost_item(item, default_state()) and post_is_recent(item):
            return True
    return False


def pick_best_original_post(feed_items, state, prefer_image=True):
    for item in feed_items:
        post = item.post
        author = normalize_handle(getattr_any(getattr_any(post, "author"), "handle"))
        if author == normalize_handle(BSKY_HANDLE):
            continue
        if viewer_repost_uri(post):
            continue
        if history_contains_uri(state, getattr_any(post, "uri")):
            continue
        if feed_item_is_repost(item) or post_is_reply(item):
            continue
        if prefer_image and not post_view_has_image_embed(item):
            continue
        return item
    return None


def fetch_artist_handles_from_timeline(client, timeline_items):
    authors = []
    seen = set()
    for item in timeline_items:
        post = getattr_any(item, "post")
        author = normalize_handle(getattr_any(getattr_any(post, "author"), "handle"))
        if not author or author in seen or author == normalize_handle(BSKY_HANDLE):
            continue
        seen.add(author)
        authors.append(author)

    profiles = get_profiles(client, authors)
    return {profile_handle(profile) for profile in profiles if is_safe_profile_candidate(profile) and is_artist_like_profile(profile)}


def discover_artist_profiles(client):
    candidates = set()

    for handle in list_handles_from_follows(client, LOUFIS_HANDLE, MAX_FOLLOWS_TO_SCAN):
        candidates.add(handle)
    for handle in list_handles_from_followers(client, LOUFIS_HANDLE, MAX_FOLLOWERS_TO_SCAN):
        candidates.add(handle)

    timeline_items = get_timeline_posts(client, limit=MAX_TIMELINE_SCAN)
    for item in timeline_items:
        if not post_view_has_image_embed(item):
            continue
        handle = normalize_handle(getattr_any(getattr_any(item.post, "author"), "handle"))
        if handle and handle != normalize_handle(BSKY_HANDLE):
            candidates.add(handle)

    pool = list(candidates)
    random.shuffle(pool)
    sample = pool[: min(DISCOVERY_SAMPLE_SIZE, len(pool))]
    print(f"[INFO] Candidate handles from Loufis + timeline: {len(candidates)} (sample {len(sample)})")

    artist_profiles = []
    for profile in get_profiles(client, sample):
        handle = profile_handle(profile)
        if not is_safe_profile_candidate(profile):
            continue
        if not is_artist_like_profile(profile):
            continue
        if has_recent_image_post(client, handle, limit=10):
            artist_profiles.append(profile)
        if random.random() < 0.15:
            human_sleep(0.5, 1.1)

    print(f"[INFO] Artist-like profiles with recent image posts: {len(artist_profiles)}")
    random.shuffle(artist_profiles)
    return artist_profiles


def maybe_follow_artist(client, state, profile, dt):
    remaining = weekly_remaining(state, "follows", WEEKLY_FOLLOW_QUOTA)
    if remaining <= 0:
        print("[INFO] Weekly follow quota exhausted.")
        return False

    days = days_since(state.get("history", {}).get("last_follow_at"), dt)
    if days is not None and days < MIN_DAYS_BETWEEN_FOLLOWS:
        print(f"[INFO] Last follow was only {days} day(s) ago; skipping new follow.")
        return False

    if random.random() > FOLLOW_ATTEMPT_PROBABILITY:
        print("[INFO] Skipping follow this time to stay very light.")
        return False

    handle = profile_handle(profile)
    did = profile_did(profile)
    if not is_safe_profile_candidate(profile):
        return False
    if viewer_following_uri(profile):
        print(f"[INFO] Already following @{handle}.")
        return False
    if already_followed_before(state, did, handle):
        print(f"[INFO] @{handle} was already followed before; skipping.")
        return False

    if follow_actor(client, did or handle):
        remember_follow(state, did, handle, dt)
        save_state(state)
        print(f"[OK] Followed @{handle}")
        human_sleep(1.0, 2.0)
        return True

    return False


# -------------------- Routines --------------------
def routine_morning_likes(client, state, dt):
    remaining = weekly_remaining(state, "likes", WEEKLY_LIKE_QUOTA)
    if remaining <= 0:
        print("[INFO] Weekly like quota exhausted.")
        return 0

    items = get_timeline_posts(client, limit=MAX_TIMELINE_SCAN)
    artist_handles = fetch_artist_handles_from_timeline(client, items)
    random.shuffle(items)

    target = min(random.randint(*MORNING_LIKES_RANGE), remaining)
    done = 0

    for item in items:
        if done >= target:
            break
        try:
            if not eligible_like_item(item, artist_handles, state):
                continue
            post = item.post
            if like_post(client, post.uri, post.cid):
                remember_like(state, post.uri)
                save_state(state)
                done += 1
                human_sleep(1.0, 2.4)
        except Exception as exc:
            print(f"[WARN] morning like loop failed: {exc}")

    print(f"[OK] Morning likes: {done}/{target} (remaining this week: {weekly_remaining(state, 'likes', WEEKLY_LIKE_QUOTA)})")
    return done


def repost_artist_post(client, state, artist_profiles, dt):
    remaining = weekly_remaining(state, "artist_reposts", WEEKLY_ARTIST_REPOST_QUOTA)
    if remaining <= 0:
        print("[INFO] Weekly artist repost quota exhausted.")
        return False

    tries = 0
    for profile in artist_profiles:
        if tries >= MAX_ARTIST_IMAGE_TRIES:
            break
        tries += 1
        handle = profile_handle(profile)
        feed = get_author_feed(client, handle, limit=12, feed_filter="posts_with_media")
        picked = pick_best_original_post(feed, state, prefer_image=True)
        if not picked:
            continue
        post = picked.post
        if repost_post(client, post.uri, post.cid):
            remember_repost(state, post.uri, "artist_reposts")
            save_state(state)
            print(f"[OK] Reposted artist image from @{handle}")
            human_sleep(1.0, 2.2)
            maybe_follow_artist(client, state, profile, dt)
            return True

    print("[WARN] No eligible artist image found for repost.")
    return False


def repost_loufis_post(client, state):
    remaining = weekly_remaining(state, "loufis_reposts", WEEKLY_LOUFIS_REPOST_QUOTA)
    if remaining <= 0:
        print("[INFO] Weekly Loufis repost quota exhausted.")
        return False

    feed = get_author_feed(client, LOUFIS_HANDLE, limit=12, feed_filter="posts_with_media")
    picked = pick_best_original_post(feed, state, prefer_image=True)
    if not picked:
        feed = get_author_feed(client, LOUFIS_HANDLE, limit=12, feed_filter="posts_no_replies")
        picked = pick_best_original_post(feed, state, prefer_image=False)
    if not picked:
        print(f"[WARN] No eligible post to repost on @{LOUFIS_HANDLE}.")
        return False

    post = picked.post
    if repost_post(client, post.uri, post.cid):
        remember_repost(state, post.uri, "loufis_reposts")
        save_state(state)
        print(f"[OK] Reposted Loufisart from @{LOUFIS_HANDLE}")
        human_sleep(0.9, 1.8)
        return True

    return False


def routine_evening_posts(client, state, dt):
    artist_profiles = discover_artist_profiles(client)
    steps = ["artist", "loufis"]
    random.shuffle(steps)
    did_artist = False
    did_loufis = False

    for step in steps:
        if step == "artist" and not did_artist:
            did_artist = repost_artist_post(client, state, artist_profiles, dt)
        elif step == "loufis" and not did_loufis:
            did_loufis = repost_loufis_post(client, state)

    if not did_artist:
        did_artist = repost_artist_post(client, state, artist_profiles, dt)
    if not did_loufis:
        did_loufis = repost_loufis_post(client, state)

    print(
        "[OK] Evening support done. "
        f"artist_reposts={weekly_count(state, 'artist_reposts')}/{WEEKLY_ARTIST_REPOST_QUOTA}, "
        f"loufis_reposts={weekly_count(state, 'loufis_reposts')}/{WEEKLY_LOUFIS_REPOST_QUOTA}, "
        f"follows={weekly_count(state, 'follows')}/{WEEKLY_FOLLOW_QUOTA}"
    )
    return int(did_artist) + int(did_loufis)


# -------------------- Main --------------------
def detect_mode(dt, explicit_mode):
    if explicit_mode:
        return explicit_mode
    if is_evening_brussels(dt):
        return "evening_posts"
    if is_morning_brussels(dt):
        return "morning_likes"
    return ""


def main():
    explicit_mode = os.getenv("MODE", "").strip().lower()
    dt = now_brussels()
    print(f"[ArtLift/Bluesky] Run at {dt.isoformat()} (Europe/Brussels)")

    mode = detect_mode(dt, explicit_mode)
    if not mode:
        print("[INFO] Outside configured morning/evening windows; exiting quietly.")
        return

    state = ensure_current_week(load_state(), dt)
    if slot_already_done(state, mode, dt) and not FORCE_RUN:
        print(f"[INFO] Slot already completed for {slot_key(mode, dt)}; exiting.")
        return

    print(
        "[INFO] Quotas this week: "
        f"likes={weekly_count(state, 'likes')}/{WEEKLY_LIKE_QUOTA}, "
        f"artist_reposts={weekly_count(state, 'artist_reposts')}/{WEEKLY_ARTIST_REPOST_QUOTA}, "
        f"loufis_reposts={weekly_count(state, 'loufis_reposts')}/{WEEKLY_LOUFIS_REPOST_QUOTA}, "
        f"follows={weekly_count(state, 'follows')}/{WEEKLY_FOLLOW_QUOTA}"
    )
    print(f"[INFO] Mode: {mode}")

    client = login_client()

    if mode == "morning_likes":
        routine_morning_likes(client, state, dt)
    elif mode == "evening_posts":
        routine_evening_posts(client, state, dt)
    else:
        print(f"[WARN] Unknown MODE={mode}; exiting.")
        return

    mark_slot_done(state, mode, dt)
    save_state(state)


if __name__ == "__main__":
    main()
