# -*- coding: utf-8 -*-
"""
ArtLift Bluesky — découverte automatique de profils artistiques (sans liste en dur)
- Matin (07–11 Europe/Brussels): likes légers de la timeline
- Soir  (19–22 Europe/Brussels): 1 repost image d'un profil artistique + 1 repost de @loufisart.bsky.social
""" 

import os, random, sys, time
from datetime import datetime, time as dtime, timedelta
from dateutil import tz, parser as dtparser
from atproto import Client, models as at

# -------------------- Secrets / env --------------------
BSKY_HANDLE = os.getenv("BSKY_HANDLE")            # ex: "toncompte.bsky.social"
BSKY_APP_PASSWORD = os.getenv("BSKY_APP_PASSWORD")  # App Password depuis Bluesky

# Handle Loufisart (depuis le lien fourni)
LOUFIS_HANDLE = "loufisart.bsky.social"

# -------------------- Réglages comportement --------------------
# Matin: combien de likes aléatoires sur la timeline
MORNING_LIKES_RANGE = (5, 10)

# Découverte: combien de comptes max à scanner
MAX_FOLLOWS_TO_SCAN   = 250   # depuis les "follows" de Loufisart
MAX_FOLLOWERS_TO_SCAN = 250   # depuis les "followers" de Loufisart

# Filtrage "profil artistique" (heuristique par mots-clés)
ART_KEYWORDS = {
    "art", "artist", "artiste", "illustrator", "illustration", "painter", "peintre",
    "dessin", "dessinateur", "dessinatrice", "comic", "bd", "manga",
    "photography", "photographer", "photo", "photographie",
    "3d", "cgi", "digital art", "concept art", "sculpt", "sculpture",
    "nft", "pixel art", "motion", "visual", "graphiste", "graphic", "designer",
}

# Fraîcheur minimale d’un post image pour considérer un compte "actif" (jours)
RECENT_IMAGE_DAYS = 21

# Combien de handles artistiques max à tester pour trouver un post image le soir
MAX_ARTIST_IMAGE_TRIES = 15

# Pauses humaines
def human_sleep(a=0.8, b=2.0):
    time.sleep(random.uniform(a, b))

# -------------------- Time helpers --------------------
def now_brussels():
    brussels = tz.gettz("Europe/Brussels")
    return datetime.utcnow().replace(tzinfo=tz.UTC).astimezone(brussels)

def is_evening_brussels(dt):
    start = dtime(19, 0)
    end   = dtime(22, 0)
    t = dt.time()
    return (t >= start) and (t < end)

def is_morning_brussels(dt):
    start = dtime(7, 0)
    end   = dtime(11, 0)
    t = dt.time()
    return (t >= start) and (t < end)

# -------------------- Bluesky API wrappers --------------------
def login_client():
    if not BSKY_HANDLE or not BSKY_APP_PASSWORD:
        print("ERROR: Set env vars BSKY_HANDLE and BSKY_APP_PASSWORD.", file=sys.stderr)
        sys.exit(1)
    client = Client()
    client.login(BSKY_HANDLE, BSKY_APP_PASSWORD)
    return client

def get_timeline_posts(client, limit=50):
    feed = client.app.bsky.feed.get_timeline(limit=limit)
    return feed.feed or []

def like_post(client, uri, cid):
    try:
        client.like(uri, cid)
        return True
    except Exception as e:
        print(f"[WARN] like failed: {e}")
        return False

def repost_post(client, uri, cid):
    try:
        client.repost(uri, cid)
        return True
    except Exception as e:
        print(f"[WARN] repost failed: {e}")
        return False

def get_author_feed(client, handle, limit=30):
    try:
        af = client.app.bsky.feed.get_author_feed(actor=handle, limit=limit)
        return af.feed or []
    except Exception as e:
        print(f"[WARN] get_author_feed({handle}) failed: {e}")
        return []

def get_profile(client, handle):
    try:
        return client.app.bsky.actor.get_profile(actor=handle)
    except Exception as e:
        print(f"[WARN] get_profile({handle}) failed: {e}")
        return None

def list_handles_from_follows(client, handle, max_count):
    """Collecte jusqu'à max_count handles des 'follows' (comptes suivis par handle)."""
    out, cursor = [], None
    while len(out) < max_count:
        res = client.app.bsky.graph.get_follows(actor=handle, limit=100, cursor=cursor)
        for u in res.follows or []:
            if u.handle and u.handle.lower() != handle.lower():
                out.append(u.handle)
                if len(out) >= max_count:
                    break
        if not getattr(res, "cursor", None) or len(out) >= max_count:
            break
        cursor = res.cursor
    return out

def list_handles_from_followers(client, handle, max_count):
    """Collecte jusqu'à max_count handles des 'followers' (qui suivent handle)."""
    out, cursor = [], None
    while len(out) < max_count:
        res = client.app.bsky.graph.get_followers(actor=handle, limit=100, cursor=cursor)
        for u in res.followers or []:
            if u.handle and u.handle.lower() != handle.lower():
                out.append(u.handle)
                if len(out) >= max_count:
                    break
        if not getattr(res, "cursor", None) or len(out) >= max_count:
            break
        cursor = res.cursor
    return out

# -------------------- Détection "profil artistique" --------------------
def is_artist_like_profile(profile):
    """
    Heuristique simple: mots-clés dans displayName / description.
    """
    if not profile:
        return False
    text = f"{getattr(profile, 'displayName', '')} || {getattr(profile, 'description', '')}"
    text_low = text.lower()
    return any(k in text_low for k in ART_KEYWORDS)

def post_view_has_image_embed(post_view):
    """Vérifie si un FeedViewPost comporte une image (directe ou via recordWithMedia)."""
    try:
        embed = post_view.post.embed
    except Exception:
        embed = None

    if isinstance(embed, at.AppBskyEmbedImages.View):
        return True
    if isinstance(embed, at.AppBskyEmbedRecordWithMedia.View):
        media = embed.media
        if isinstance(media, at.AppBskyEmbedImages.View):
            return True
    # heuristique prudente
    try:
        if hasattr(embed, "images") and embed.images:
            return True
    except Exception:
        pass
    return False

def post_is_recent(post_view, within_days=RECENT_IMAGE_DAYS):
    try:
        created = getattr(post_view.post.record, "createdAt", None)
        if not created:
            return False
        ts = dtparser.parse(created)
        return (now_brussels() - ts).days <= within_days
    except Exception:
        return False

def has_recent_image_post(client, handle, limit=30):
    feed = get_author_feed(client, handle, limit=limit)
    for item in feed:
        try:
            if post_view_has_image_embed(item) and post_is_recent(item):
                return True
        except Exception:
            continue
    return False

def discover_artist_handles(client):
    """
    Découvre des handles potentiellement artistiques, sans liste en dur :
    - Follows de Loufisart
    - Followers de Loufisart
    - Filtre par bio (keywords) + présence d’un post image récent
    """
    candidates = set()

    follows = list_handles_from_follows(client, LOUFIS_HANDLE, MAX_FOLLOWS_TO_SCAN)
    followers = list_handles_from_followers(client, LOUFIS_HANDLE, MAX_FOLLOWERS_TO_SCAN)

    # pool brute (évite doublons)
    for h in follows + followers:
        if h.lower() == LOUFIS_HANDLE.lower():
            continue
        candidates.add(h)

    print(f"[INFO] Candidate handles from network: ~{len(candidates)}")

    # shuffle pour échantillonnage aléatoire
    pool = list(candidates)
    random.shuffle(pool)

    artist_like = []
    # on échantillonne raisonnablement (pour ne pas trop charger l'API)
    sample_size = min(200, len(pool))
    for h in pool[:sample_size]:
        profile = get_profile(client, h)
        if not is_artist_like_profile(profile):
            continue
        # Vérifie qu'il y a de l'image récente
        if has_recent_image_post(client, h, limit=25):
            artist_like.append(h)
        # Petites pauses pour rester "humain" et éviter de spammer
        if random.random() < 0.15:
            human_sleep(0.6, 1.4)

    print(f"[INFO] Artist-like handles with recent images: {len(artist_like)}")
    return artist_like

# -------------------- Sélecteurs de posts --------------------
def pick_first_image_post_from_feed(feed_items):
    for item in feed_items:
        try:
            if post_view_has_image_embed(item):
                return (item.post.uri, item.post.cid, item)
        except Exception:
            continue
    return (None, None, None)

def pick_latest_post_from_feed(feed_items):
    for item in feed_items:
        try:
            return (item.post.uri, item.post.cid, item)
        except Exception:
            continue
    return (None, None, None)

# -------------------- Routines --------------------
def routine_morning_likes(client):
    """
    Like quelques posts de la timeline, évite soi-même & doublons.
    """
    items = get_timeline_posts(client, limit=80)
    target = random.randint(*MORNING_LIKES_RANGE)
    done = 0
    random.shuffle(items)

    for it in items:
        try:
            post = it.post
            author = post.author.handle
            viewer = post.viewer  # contient possiblement .like si déjà liké
            if author and BSKY_HANDLE and author.lower() == BSKY_HANDLE.lower():
                continue
            if viewer and getattr(viewer, "like", None):
                continue
            # léger skip aléatoire
            if random.random() < 0.25:
                continue
            if like_post(client, post.uri, post.cid):
                done += 1
                human_sleep(1.0, 2.5)
                if done >= target:
                    break
        except Exception as e:
            print(f"[WARN] morning like loop failed: {e}")
            continue

    print(f"[OK] Morning likes: {done}/{target}")

def routine_evening_posts(client):
    """
    19–22h Europe/Brussels :
      - Repost d’un post image d’un profil artistique découvert automatiquement
      - Repost d’un post récent de Loufisart
      Ordre aléatoire pour “humaniser”.
    """
    steps = ["artist_image", "loufis"]
    random.shuffle(steps)

    did_artist = False
    did_loufis = False

    # Découverte dynamique (sans liste en dur)
    artist_pool = discover_artist_handles(client)
    random.shuffle(artist_pool)

    for step in steps:
        if step == "artist_image" and not did_artist:
            tries = 0
            success = False
            for h in artist_pool:
                if tries >= MAX_ARTIST_IMAGE_TRIES:
                    break
                tries += 1
                feed = get_author_feed(client, h, limit=30)
                uri, cid, _ = pick_first_image_post_from_feed(feed)
                if uri and cid:
                    if repost_post(client, uri, cid):
                        print(f"[OK] Reposted image from @{h}")
                        did_artist = True
                        success = True
                        human_sleep(1.0, 2.2)
                        break
            if not success:
                print("[WARN] No image found among artist-like pool; trying timeline fallback.")
                items = get_timeline_posts(client, limit=80)
                uri, cid, _ = pick_first_image_post_from_feed(items)
                if uri and cid and repost_post(client, uri, cid):
                    did_artist = True
                    human_sleep(0.9, 1.8)

        elif step == "loufis" and not did_loufis:
            feed = get_author_feed(client, LOUFIS_HANDLE, limit=25)
            uri, cid, _ = pick_latest_post_from_feed(feed)
            if uri and cid and repost_post(client, uri, cid):
                print(f"[OK] Reposted latest from @{LOUFIS_HANDLE}")
                did_loufis = True
                human_sleep(0.9, 1.8)
            else:
                print(f"[WARN] No post to repost on @{LOUFIS_HANDLE}")

    # Garanties si l’ordre aléatoire n’a pas suffi
    if not did_artist:
        items = get_timeline_posts(client, limit=80)
        uri, cid, _ = pick_first_image_post_from_feed(items)
        if uri and cid:
            repost_post(client, uri, cid)

    if not did_loufis:
        feed = get_author_feed(client, LOUFIS_HANDLE, limit=25)
        uri, cid, _ = pick_latest_post_from_feed(feed)
        if uri and cid:
            repost_post(client, uri, cid)

    print("[OK] Evening reposts done.")

# -------------------- Main --------------------
def main():
    mode = os.getenv("MODE", "").strip().lower()  # optionnel: morning_likes / evening_posts
    dt = now_brussels()
    print(f"[ArtLift/Bluesky] Run at {dt.isoformat()} (Europe/Brussels)")

    client = login_client()

    if not mode:
        if is_evening_brussels(dt):
            mode = "evening_posts"
        elif is_morning_brussels(dt):
            mode = "morning_likes"
        else:
            # hors créneaux, on fait un petit passage like discret
            mode = "morning_likes"

    print(f"[INFO] Mode: {mode}")
    if mode == "morning_likes":
        routine_morning_likes(client)
    elif mode == "evening_posts":
        routine_evening_posts(client)
    else:
        print(f"[WARN] Unknown MODE={mode}; defaulting to morning_likes.")
        routine_morning_likes(client)

if __name__ == "__main__":
    main()
