# -*- coding: utf-8 -*-
import os, random, sys, time
from datetime import datetime, time as dtime
from dateutil import tz
from atproto import Client, models as at

# -------------------- Réglages --------------------
# Identifiants Bluesky (utilise un App Password !)
BSKY_HANDLE = os.getenv("BSKY_HANDLE")          # ex: "moncompte.bsky.social"
BSKY_APP_PASSWORD = os.getenv("BSKY_APP_PASSWORD")

# Handle Loufisart sur Bluesky (depuis ton lien)
LOUFIS_HANDLE = "loufisart.bsky.social"

# Liste de comptes "artistes" à promouvoir (sans @, version bsky)
ARTISTS = [
    "artist1.bsky.social",
    "artist2.bsky.social",
    "artist3.bsky.social",
]

# Matin : combien de likes ?
MORNING_LIKES_RANGE = (5, 10)

# Soir : sécurité → si on ne trouve pas d’image chez l’artiste, on tente plusieurs handles
MAX_ARTIST_IMAGE_TRIES = 5

# -------------------- Helpers temps --------------------
def now_brussels():
    brussels = tz.gettz("Europe/Brussels")
    return datetime.utcnow().replace(tzinfo=tz.UTC).astimezone(brussels)

def is_evening_brussels(dt):
    start = dtime(19, 0)
    end   = dtime(22, 0)
    t = dt.time()
    return (t >= start) and (t < end)

def is_morning_brussels(dt):
    # “matin” souple ~ 07:00–11:00
    start = dtime(7, 0)
    end   = dtime(11, 0)
    t = dt.time()
    return (t >= start) and (t < end)

# -------------------- Bluesky API wrappers --------------------
def login_client():
    if not BSKY_HANDLE or not BSKY_APP_PASSWORD:
        print("ERROR: Set BSKY_HANDLE and BSKY_APP_PASSWORD env vars.", file=sys.stderr)
        sys.exit(1)
    client = Client()
    client.login(BSKY_HANDLE, BSKY_APP_PASSWORD)
    return client

def get_timeline_posts(client, limit=50):
    feed = client.app.bsky.feed.get_timeline(limit=limit)
    # feed.feed est une liste d'items; chaque item a .post (StrongRef + record + etc.)
    return feed.feed or []

def like_post(client, uri, cid):
    # évite les doublons : si déjà liké, Bluesky renverra une erreur — on ignore
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

def post_has_image_embed(post_view):
    """
    post_view est typiquement atproto_client.app.bsky.feed.defs.FeedViewPost
    On inspecte post_view.post.embed (peut être Union: images, external, recordWithMedia, etc.)
    """
    try:
        embed = post_view.post.embed
    except Exception:
        embed = None

    # Direct: images
    if isinstance(embed, at.AppBskyEmbedImages.View):
        return True

    # RecordWithMedia: peut contenir images
    if isinstance(embed, at.AppBskyEmbedRecordWithMedia.View):
        media = embed.media
        if isinstance(media, at.AppBskyEmbedImages.View):
            return True

    # Certaines réponses/rich embeds peuvent aussi contenir des images — heuristique:
    # Si on trouve un champ 'images' avec une liste non vide
    try:
        if hasattr(embed, "images") and embed.images:
            return True
    except Exception:
        pass

    return False

def pick_first_image_post_from_feed(feed_items):
    for item in feed_items:
        try:
            if post_has_image_embed(item):
                uri = item.post.uri
                cid = item.post.cid
                return (uri, cid, item)
        except Exception:
            continue
    return (None, None, None)

def pick_latest_post_from_feed(feed_items):
    for item in feed_items:
        try:
            uri = item.post.uri
            cid = item.post.cid
            return (uri, cid, item)
        except Exception:
            continue
    return (None, None, None)

# -------------------- Routines --------------------
def routine_morning_likes(client):
    """
    Like doucement quelques posts de la timeline.
    Évite de liker ses propres posts et doublons.
    """
    items = get_timeline_posts(client, limit=60)
    to_like = random.randint(*MORNING_LIKES_RANGE)
    done = 0

    # mélange et parcours
    random.shuffle(items)

    for it in items:
        try:
            post = it.post
            author = post.author.handle
            viewer = post.viewer  # peut contenir .like si déjà liké
            # skip si c'est nous
            if author == BSKY_HANDLE:
                continue
            # déjà liké ?
            if viewer and getattr(viewer, "like", None):
                continue
            # petit pourcentage de skip pour paraître “humain”
            if random.random() < 0.2:
                continue

            if like_post(client, post.uri, post.cid):
                done += 1
                # pauses humaines
                time.sleep(random.uniform(1.0, 2.5))
                if done >= to_like:
                    break
        except Exception as e:
            print(f"[WARN] morning like loop item failed: {e}")
            continue

    print(f"[OK] Morning likes done: {done}/{to_like}")

def routine_evening_posts(client):
    """
    19–22h Europe/Brussels:
      (ordre aléatoire)
        A) Repost d’un post avec image d’un autre utilisateur (idéalement artiste)
        B) Repost du dernier post de Loufisart
      + pauses/variabilité légère
    """
    steps = ["artist_image", "loufis"]
    random.shuffle(steps)

    did_artist = False
    did_loufis = False

    for step in steps:
        if step == "artist_image" and not did_artist:
            # essaie plusieurs artistes aléatoires
            pool = ARTISTS[:]
            random.shuffle(pool)
            tries = 0
            success = False
            for handle in pool:
                if tries >= MAX_ARTIST_IMAGE_TRIES:
                    break
                tries += 1
                feed = get_author_feed(client, handle, limit=25)
                uri, cid, item = pick_first_image_post_from_feed(feed)
                if uri and cid:
                    if repost_post(client, uri, cid):
                        print(f"[OK] Reposted image post from @{handle}")
                        success = True
                        did_artist = True
                        # petite pause
                        time.sleep(random.uniform(1.2, 2.8))
                        break
            if not success:
                # fallback ultime : timeline → premier post image
                print("[WARN] No artist image found; trying timeline fallback.")
                items = get_timeline_posts(client, limit=60)
                uri, cid, item = pick_first_image_post_from_feed(items)
                if uri and cid:
                    if repost_post(client, uri, cid):
                        print("[OK] Reposted an image post from timeline")
                        did_artist = True
                        time.sleep(random.uniform(1.0, 2.2))

        elif step == "loufis" and not did_loufis:
            # dernier post de Loufisart
            feed = get_author_feed(client, LOUFIS_HANDLE, limit=20)
            uri, cid, item = pick_latest_post_from_feed(feed)
            if uri and cid:
                if repost_post(client, uri, cid):
                    print(f"[OK] Reposted latest post from @{LOUFIS_HANDLE}")
                    did_loufis = True
                    time.sleep(random.uniform(1.0, 2.2))
            else:
                print(f"[WARN] No post found on @{LOUFIS_HANDLE}")

    # Assure les 2 objectifs si l’ordre aléatoire n’a pas tout couvert
    if not did_artist:
        print("[INFO] Ensuring at least one image repost…")
        items = get_timeline_posts(client, limit=60)
        uri, cid, item = pick_first_image_post_from_feed(items)
        if uri and cid:
            repost_post(client, uri, cid)

    if not did_loufis:
        print("[INFO] Ensuring loufis repost…")
        feed = get_author_feed(client, LOUFIS_HANDLE, limit=20)
        uri, cid, item = pick_latest_post_from_feed(feed)
        if uri and cid:
            repost_post(client, uri, cid)

    print("[OK] Evening reposts done.")

# -------------------- Main --------------------
def main():
    mode = os.getenv("MODE", "").strip().lower()  # optional override: morning_likes / evening_posts
    dt = now_brussels()
    print(f"[ArtLift/Bluesky] Run at {dt.isoformat()} (Europe/Brussels)")

    client = login_client()

    # Décision de mode si non forçé
    if not mode:
        if is_evening_brussels(dt):
            mode = "evening_posts"
        elif is_morning_brussels(dt):
            mode = "morning_likes"
        else:
            # Par défaut hors créneaux : petit passage de likes light
            mode = "morning_likes"

    print(f"[INFO] Mode selected: {mode}")
    if mode == "morning_likes":
        routine_morning_likes(client)
    elif mode == "evening_posts":
        routine_evening_posts(client)
    else:
        print(f"[WARN] Unknown MODE={mode}, doing morning_likes as fallback.")
        routine_morning_likes(client)

if __name__ == "__main__":
    main()
