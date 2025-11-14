import os, random, time, sys, pathlib
from datetime import datetime
from dateutil import tz
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

USERNAME = os.getenv("X_USERNAME")
PASSWORD = os.getenv("X_PASSWORD")

AUTH_DIR = pathlib.Path(".auth"); AUTH_DIR.mkdir(exist_ok=True)
AUTH_STATE = str(AUTH_DIR / "state.json")

# --- Réglages ---
ARTISTS_NFT = ["artistNFT1", "artistNFT2", "artistNFT3"]   # sans @
ARTISTS_NONNFT = ["artist1", "artist2", "artist3"]
PICTNART_PROMOS = [
    "Discover our partner Pictnart for creative support and artistic growth 👉 https://pictnartcompany-ux.github.io/grow_your_craft/",
    "Looking for motivation in your art journey? 🌟 Check out our partner Pictnart 👉 https://pictnartcompany-ux.github.io/grow_your_craft/",
    "Elevate your craft with the help of our partner Pictnart 🎨 👉 https://pictnartcompany-ux.github.io/grow_your_craft/",
]
P_ARTISTS = 0.70
P_PROMO   = 0.0025
RANDOM_WAIT = (0.6, 1.3)

def human_pause(a=RANDOM_WAIT[0], b=RANDOM_WAIT[1]):
    time.sleep(random.uniform(a, b))

def snap(page, label="snap"):
    try:
        page.screenshot(path=f"{label}.png", full_page=True)
        print(f"[DEBUG] screenshot saved: {label}.png")
    except Exception as e:
        print(f"[DEBUG] screenshot failed: {e}")

# --- Helpers robustes --------------------------------------------------------
def accept_cookies(page):
    # Clique sur les bannières cookies FR/EN si présentes
    selectors = [
        'button:has-text("Accept all")', 'button:has-text("Accept all cookies")',
        'button:has-text("Allow all cookies")', 'div[role="button"]:has-text("Accept")',
        'button:has-text("Tout accepter")', 'button:has-text("Accepter")',
        'div[role="button"]:has-text("Tout accepter")', 'div[role="button"]:has-text("Accepter")',
    ]
    for sel in selectors:
        try:
            if page.is_visible(sel):
                page.click(sel, delay=random.randint(40,100))
                human_pause()
                print("[INFO] Cookies accepted with:", sel)
                return
        except:
            continue

def ensure_logged_in(page):
    """
    S’assure qu’on est loggé. Si on tombe sur le flow login ou un interstitiel,
    on gère le login. Ensuite on va sur /home pour stabiliser.
    """
    print("[INFO] Ensuring session is logged in…")
    page.goto("https://x.com/home", wait_until="domcontentloaded")
    accept_cookies(page)

    # Si on est redirigé vers /login, on se logge.
    if page.url.startswith("https://x.com/i/flow/login") or "login" in page.url:
        login(page)

    # Parfois, pas de timeline visible tout de suite : on force un aller-retour léger
    try:
        page.wait_for_selector('a[href="/home"], nav', timeout=15000)
    except PWTimeout:
        page.goto("https://x.com/home", wait_until="domcontentloaded")

def go_to_composer(page):
    """
    Ouvre directement l’éditeur via l’URL officielle compose.
    C’est beaucoup plus fiable que d’attendre un bouton.
    """
    print("[INFO] Opening composer…")
    page.goto("https://x.com/compose/tweet", wait_until="domcontentloaded")
    accept_cookies(page)
    # Attendre un champ éditable
    page.wait_for_selector('div[role="textbox"], div[data-testid="tweetTextarea_0"], textarea', timeout=30000)
    print("[OK] Composer is ready.")

# --- Login -------------------------------------------------------------------
def login(page):
    print("[INFO] Navigating to login flow…")
    page.goto("https://x.com/i/flow/login", wait_until="domcontentloaded")
    accept_cookies(page)

    print("[INFO] Filling username/email…")
    page.wait_for_selector('input[name="text"]', timeout=30000)
    page.fill('input[name="text"]', USERNAME)
    human_pause()
    page.keyboard.press("Enter")

    # Challenge possible: X redemande l’identifiant
    try:
        page.wait_for_selector('input[name="text"]', timeout=4000)
        if page.is_visible('input[name="text"]'):
            print("[INFO] Extra identifier prompt detected. Refilling…")
            page.fill('input[name="text"]', USERNAME)
            human_pause()
            page.keyboard.press("Enter")
    except PWTimeout:
        pass

    print("[INFO] Filling password…")
    page.wait_for_selector('input[name="password"]', timeout=30000)
    page.fill('input[name="password"]', PASSWORD)
    human_pause()
    page.keyboard.press("Enter")
    human_pause()

    # Fallback boutons (localisés)
    for sel in [
        'div[data-testid="LoginForm_Login_Button"]',
        'div[role="button"]:has-text("Log in")',
        'div[role="button"]:has-text("Se connecter")'
    ]:
        if page.is_visible(sel):
            page.click(sel)
            human_pause()
            break

    print("[INFO] Waiting for home UI…")
    # Au lieu d’attendre “Post”, on valide la home/nav
    try:
        page.wait_for_selector('a[href="/home"], nav', timeout=30000)
    except PWTimeout:
        # Encore un coup de pouce
        page.goto("https://x.com/home", wait_until="domcontentloaded")

    print("[OK] Logged in.")
    snap(page, "after_login")

# --- Actions de post ---------------------------------------------------------
def post_text(page, text):
    go_to_composer(page)
    # Remplir via plusieurs sélecteurs possibles
    for sel in ['div[role="textbox"]', 'div[data-testid="tweetTextarea_0"]', 'textarea']:
        try:
            if page.is_visible(sel):
                page.fill(sel, text)
                break
        except:
            continue
    human_pause()
    # Bouton envoyer (FR/EN)
    for sel in ['div[data-testid="tweetButtonInline"]',
                'div[role="button"]:has-text("Post")',
                'div[role="button"]:has-text("Tweeter")']:
        try:
            if page.is_visible(sel):
                page.click(sel)
                human_pause(1.2, 2.2)
                print("[OK] Text posted.")
                return
        except:
            continue
    raise RuntimeError("Could not find send button in composer.")

def retweet_latest(page, handle_without_at):
    h = handle_without_at.lstrip("@")
    print(f"[INFO] Visiting profile @{h}…")
    page.goto(f"https://x.com/{h}", wait_until="domcontentloaded")
    accept_cookies(page)
    page.wait_for_selector('article', timeout=30000)
    human_pause()

    print("[INFO] Trying retweet…")
    try:
        page.click('article div[data-testid="retweet"]', delay=random.randint(60, 140))
    except:
        try:
            page.click('article div[data-testid="caret"]', delay=random.randint(60, 140))
        except:
            page.click('article div[data-testid="share"]', delay=random.randint(60, 140))
    human_pause()
    page.click('div[role="menuitem"]:has-text("Retweet"), div[role="menuitem"]:has-text("Retweeter")')
    human_pause(1.2, 2.2)
    print("[OK] Retweet done.")

def quote_retweet(page, handle_without_at, comment):
    h = handle_without_at.lstrip("@")
    print(f"[INFO] Quote-RT @{h}…")
    page.goto(f"https://x.com/{h}", wait_until="domcontentloaded")
    accept_cookies(page)
    page.wait_for_selector('article', timeout=30000)
    page.click('article div[data-testid="retweet"]', delay=random.randint(60,140))
    human_pause()
    page.click('div[role="menuitem"]:has-text("Quote"), div[role="menuitem"]:has-text("Citer")')
    human_pause()
    # On est déjà dans un composer de quote → on poste avec post_text pour robustesse
    post_text(page, comment)

def post_pictnart_promo(page):
    post_text(page, random.choice(PICTNART_PROMOS))

def post_loufis(page):
    try:
        retweet_latest(page, "loufisart")
    except Exception as e:
        print(f"[WARN] RT loufisart failed: {e}")
        post_text(page, "A new piece from @loufisart — dive into the world of Loufi’s Art ✨")

def post_artists(page):
    lst = ARTISTS_NFT if random.random() < 0.5 else ARTISTS_NONNFT
    random.shuffle(lst)
    for h in lst:
        try:
            if random.random() < 0.2:
                quote_retweet(page, h, random.choice([
                    "Stunning piece ✨", "Love this style.", "Great composition!", "Texture goals."
                ]))
            else:
                retweet_latest(page, h)
            return
        except Exception as e:
            print(f"[WARN] Artist action failed for {h}: {e}")
            continue
    post_text(page, "Discover inspiring art today ✨ #art #inspiration")

# --- Main --------------------------------------------------------------------
def main():
    brussels = tz.gettz("Europe/Brussels")
    now = datetime.utcnow().replace(tzinfo=tz.UTC).astimezone(brussels)
    print(f"[ArtLift] Run at {now.isoformat()}")

    if not USERNAME or not PASSWORD:
        print("ERROR: Missing X_USERNAME or X_PASSWORD env vars.", file=sys.stderr)
        sys.exit(1)

    with sync_playwright() as p:
        # --- lancement Chromium avec auto-install si besoin ---
        def launch_chromium():
            return p.chromium.launch(headless=True)

        try:
            browser = launch_chromium()
        except Exception as e:
            print(f"[WARN] Chromium not found, installing… ({e})")
            import subprocess
            subprocess.run(
                ["python", "-m", "playwright", "install", "chromium", "--with-deps"],
                check=False
            )
            browser = launch_chromium()

        context_kwargs = dict(
            locale="fr-FR",
            timezone_id="Europe/Brussels",
            user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/118.0.0.0 Safari/537.36",
            viewport={"width": 1366, "height": 850},
        )

        context = browser.new_context(**context_kwargs)
        context.tracing.start(screenshots=True, snapshots=True)
        page = context.new_page()

        try:
            # Si on a une session sauvegardée, essaye d’abord
            if os.path.exists(AUTH_STATE):
                print("[INFO] Loading saved auth state…")
                context.close()
                context = browser.new_context(storage_state=AUTH_STATE, **context_kwargs)
                context.tracing.start(screenshots=True, snapshots=True)
                page = context.new_page()
                page.goto("https://x.com/home", wait_until="domcontentloaded")
                accept_cookies(page)
                if page.url.startswith("https://x.com/i/flow/login") or "login" in page.url:
                    print("[INFO] Saved state invalid, logging in again…")
                    login(page)
                    context.storage_state(path=AUTH_STATE)
            else:
                login(page)
                context.storage_state(path=AUTH_STATE)

            # Variabilité humaine légère
            page.wait_for_timeout(random.randint(600, 1600))
            page.keyboard.press("PageDown")
            page.wait_for_timeout(random.randint(500, 1200))

            # 3% des runs: ne rien poster (humain)
            if random.random() < 0.03:
                print("[OK] Human-like skip (no post this run).")
                snap(page, "after_login")
                context.tracing.stop(path="trace.zip")
                browser.close()
                sys.exit(0)

            roll = random.random()
            if roll < P_PROMO:
                post_pictnart_promo(page)
                print("[OK] Posted Pictnart promo.")
            elif roll < P_PROMO + P_ARTISTS:
                post_artists(page)
                print("[OK] Posted artist RT/text.")
            else:
                post_loufis(page)
                print("[OK] Posted loufis RT/text.")

            snap(page, "after_post")
            context.tracing.stop(path="trace.zip")
            browser.close()
        except Exception as e:
            print(f"[FATAL] {e}", file=sys.stderr)
            try:
                snap(page, "error")
                context.tracing.stop(path="trace.zip")
            except:
                pass
            try:
                browser.close()
            except:
                pass
            sys.exit(1)

if __name__ == "__main__":
    main()
