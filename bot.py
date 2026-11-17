#!/usr/bin/env python3
"""
tgspotify (Telethon edition) — puts the song you're playing on Spotify into your Telegram profile music.

  Spotify (spotipy) -> yt-dlp (YouTube / SoundCloud) -> your user session -> account.saveMusic
  Bot UI and user session are both Telethon clients sharing one asyncio loop.
"""
import os, re, json, time, html, struct, asyncio, logging, traceback
from pathlib import Path

import requests
import spotipy
from spotipy.oauth2 import SpotifyOAuth
from spotipy.cache_handler import CacheFileHandler
import yt_dlp
from mutagen.id3 import ID3, ID3NoHeaderError, TIT2, TPE1, TALB, APIC
from telethon import TelegramClient, events, functions, utils as tl_utils
from telethon.errors import SessionPasswordNeededError, MessageNotModifiedError, PhoneCodeExpiredError, PhoneCodeInvalidError
from telethon.tl.tlobject import TLObject, TLRequest
from telethon import Button
from telethon.tl import types as T
from telethon.tl.types import (
    DocumentAttributeAudio, InputDocument, MessageMediaPhoto, MessageEntityCustomEmoji,
    BotCommand, BotCommandScopeDefault,
)

if not hasattr(asyncio, "to_thread"):  # Python < 3.9
    import functools
    async def _to_thread(func, *args, **kwargs):
        return await asyncio.get_running_loop().run_in_executor(None, functools.partial(func, *args, **kwargs))
    asyncio.to_thread = _to_thread

# ───────────────────────── config ─────────────────────────
BASE = Path(__file__).resolve().parent
DATA = BASE / "data"; DATA.mkdir(exist_ok=True)
CACHE = BASE / "cache"; CACHE.mkdir(exist_ok=True)

BOT_TOKEN = os.environ["BOT_TOKEN"]
OWNER_ID = int(os.environ["OWNER_ID"])
API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
SP_ID = "cfee1d81b74847769499fb100745fc75"
SP_SECRET = "e182675c8ddb41428d2fc9c9648fc8af"
SP_REDIRECT = "http://127.0.0.1:8888/callback"
POLL = max(5, int(os.getenv("POLL_INTERVAL", "15")))
CACHE_KEEP = int(os.getenv("CACHE_KEEP", "40"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("telethon").setLevel(logging.WARNING)
log = logging.getLogger("tgspotify")

# ───────────────────────── settings ─────────────────────────
class Settings:
    DEFAULTS = {
        "auto": True, "clear_on_pause": False, "delete_upload": True, "notify": False,
        "premium": True, "emoji": {},
        "last_track": None, "last_title": None, "last_sync": None, "last_doc": None,
    }

    def __init__(self, path):
        self.path = path
        self.d = dict(self.DEFAULTS)
        if path.exists():
            try: self.d.update(json.loads(path.read_text()))
            except Exception: log.exception("settings corrupted, using defaults")

    def get(self, k, default=None): return self.d.get(k, default)

    def set(self, **kw):
        self.d.update(kw)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.d, ensure_ascii=False, indent=2))
        tmp.replace(self.path)

S = Settings(DATA / "settings.json")
RUNTIME = {"premium_works": True}
STATE = {}        # chat_id -> pending input step
PHOTOS = {}       # track_id -> uploaded Photo (reused on panel edits)

# ───────────────────────── emoji ─────────────────────────
EMOJI = {
    "logo": "🎧", "music": "🎵", "disc": "💿", "artist": "👤", "album": "🎼",
    "play": "▶️", "pause": "⏸", "sync": "🔄", "download": "📥", "refresh": "🔃",
    "spotify": "💚", "yukimusic": "🎧", "telegram": "✈️", "settings": "⚙️", "on": "✅", "off": "⭕️",
    "error": "⚠️", "heart": "💖", "sparkle": "✨", "back": "⬅️", "trash": "🗑",
    "key": "🔑", "time": "⏱", "emoji": "🪄",
}

def premium_on(): return S.get("premium") and RUNTIME["premium_works"]
def eid(key): return S.get("emoji", {}).get(key) if premium_on() else None

def E(key):
    ch, i = EMOJI[key], eid(key)
    return f'<tg-emoji emoji-id="{i}">{ch}</tg-emoji>' if i else ch

esc = lambda s: html.escape(str(s or ""), quote=False)
def fmt(ms): s = int((ms or 0) / 1000); return f"{s // 60}:{s % 60:02d}"
def bar(pos, dur, n=12):
    f = 0 if not dur else max(0, min(n, round(pos / dur * n)))
    return "▰" * f + "▱" * (n - f)

# ───────────────────────── raw TL: styled buttons + saveMusic ─────────────────────────
class StyledCallbackButton(TLObject):
    """keyboardButtonCallback#e62bc960 flags:# requires_password:flags.0?true
         style:flags.10?KeyboardButtonStyle text:string data:bytes
       keyboardButtonStyle#4fdd3430 flags:# bg_primary:flags.0?true bg_danger:flags.1?true
         bg_success:flags.2?true icon:flags.3?long"""
    CONSTRUCTOR_ID = 0xE62BC960
    SUBCLASS_OF_ID = 0xBAD74A3  # KeyboardButton
    STYLES = {"primary": 1, "danger": 2, "success": 4}

    def __init__(self, text, data, style=None, icon=None):
        self.text, self.data, self.style, self.icon = text, data, style, icon

    def to_dict(self):
        return {"_": "StyledCallbackButton", "text": self.text, "data": self.data,
                "style": self.style, "icon": self.icon}

    def _bytes(self):
        flags, style = 0, b""
        if self.style or self.icon:
            flags = 1 << 10
            sflags = self.STYLES.get(self.style, 0) | (8 if self.icon else 0)
            style = struct.pack("<II", 0x4FDD3430, sflags)
            if self.icon: style += struct.pack("<q", int(self.icon))
        return (struct.pack("<II", self.CONSTRUCTOR_ID, flags) + style
                + self.serialize_bytes(self.text) + self.serialize_bytes(self.data))

class _Row(TLObject):
    """keyboardButtonRow#77608b83 buttons:Vector<KeyboardButton>"""
    CONSTRUCTOR_ID, SUBCLASS_OF_ID = 0x77608B83, 0x847730AE
    def __init__(self, buttons): self.buttons = buttons
    def to_dict(self): return {"_": "KeyboardButtonRow", "buttons": self.buttons}
    def _bytes(self):
        return (struct.pack("<II", self.CONSTRUCTOR_ID, 0x1CB5C415) + struct.pack("<i", len(self.buttons))
                + b"".join(b._bytes() for b in self.buttons))

class _InlineMarkup(TLObject):
    """replyInlineMarkup#48a30254 rows:Vector<KeyboardButtonRow>"""
    CONSTRUCTOR_ID, SUBCLASS_OF_ID = 0x48A30254, 0xE2E10EF2
    def __init__(self, rows): self.rows = rows
    def to_dict(self): return {"_": "ReplyInlineMarkup", "rows": self.rows}
    def _bytes(self):
        return (struct.pack("<II", self.CONSTRUCTOR_ID, 0x1CB5C415) + struct.pack("<i", len(self.rows))
                + b"".join(r._bytes() for r in self.rows))

Row = getattr(T, "KeyboardButtonRow", _Row)
InlineMarkup = getattr(T, "ReplyInlineMarkup", _InlineMarkup)

class SaveMusicRequest(TLRequest):
    """account.saveMusic#b26732a9 flags:# unsave:flags.0?true id:InputDocument
       after_id:flags.1?InputDocument = Bool"""
    CONSTRUCTOR_ID = 0xB26732A9
    SUBCLASS_OF_ID = 0xF5B399AC  # Bool

    def __init__(self, id, unsave=None, after_id=None):
        self.id, self.unsave, self.after_id = id, unsave, after_id

    def to_dict(self):
        return {"_": "SaveMusicRequest", "id": self.id, "unsave": self.unsave, "after_id": self.after_id}

    def _bytes(self):
        flags = (1 if self.unsave else 0) | (2 if self.after_id else 0)
        return b"".join((struct.pack("<II", self.CONSTRUCTOR_ID, flags), self.id._bytes(),
                         self.after_id._bytes() if self.after_id else b""))

def btn(text, data=None, emoji=None, style=None, url=None):
    b = {"text": text, "data": data, "url": url, "style": None, "icon": None}
    if emoji:
        i = eid(emoji)
        if i: b["icon"] = i
        else: b["text"] = f"{EMOJI[emoji]} {text}"
    if style and premium_on(): b["style"] = style
    return b

def markup(rows):
    out = []
    for row in rows:
        bs = []
        for b in row:
            if b["url"]:
                bs.append(Button.url(b["text"], b["url"]))
            else:
                bs.append(StyledCallbackButton(b["text"], b["data"].encode(), b["style"], b["icon"]))
        out.append(Row(bs))
    return InlineMarkup(out)

def doc_to_dict(d): return {"id": d.id, "access_hash": d.access_hash, "file_reference": d.file_reference.hex()}
def dict_to_doc(d): return InputDocument(d["id"], d["access_hash"], bytes.fromhex(d["file_reference"]))

# ───────────────────────── Spotify (sync, run in threads) ─────────────────────────
class Spot:
    def __init__(self):
        self.oauth = SpotifyOAuth(
            client_id=SP_ID, client_secret=SP_SECRET, redirect_uri=SP_REDIRECT,
            scope="user-read-currently-playing user-read-playback-state", open_browser=False,
            cache_handler=CacheFileHandler(cache_path=str(DATA / "spotify_token.json")))
        self.sp = spotipy.Spotify(auth_manager=self.oauth, requests_timeout=10, retries=2)

    def connected(self): return self.oauth.cache_handler.get_cached_token() is not None
    def auth_url(self): return self.oauth.get_authorize_url()

    def finish(self, pasted):
        code = self.oauth.parse_response_code(pasted.strip())
        self.oauth.get_access_token(code, as_dict=False, check_cache=False)

    def logout(self):
        p = DATA / "spotify_token.json"
        if p.exists(): p.unlink()

    def now(self):
        if not self.connected(): return None
        r = self.sp.currently_playing()
        if not r or not r.get("item") or r["item"].get("type") != "track": return None
        it = r["item"]
        imgs = sorted(it["album"].get("images") or [], key=lambda i: i.get("width") or 0)
        thumb = next((i["url"] for i in imgs if (i.get("width") or 0) >= 300), imgs[-1]["url"] if imgs else None)
        return {
            "id": it["id"] or re.sub(r"\W", "_", f"local-{it['name']}"), "title": it["name"],
            "artists": ", ".join(a["name"] for a in it["artists"]),
            "album": it["album"]["name"], "duration_ms": it["duration_ms"],
            "progress_ms": r.get("progress_ms") or 0, "is_playing": r.get("is_playing", False),
            "cover": imgs[-1]["url"] if imgs else None, "thumb": thumb,
            "url": it.get("external_urls", {}).get("spotify"),
        }

spot = Spot()

class YukiMusic:
    """Now playing from YukiMusic via the personal key (Settings → Telegram profile music)."""
    API = "https://singer.yukichat.lol/api/np/"
    KEY = DATA / "yukimusic_key.txt"

    def key(self): return self.KEY.read_text().strip() if self.KEY.exists() else None
    def connected(self): return bool(self.key())
    def logout(self): self.KEY.unlink(missing_ok=True)

    def link(self, key):
        key = key.strip()
        r = requests.get(self.API + key, timeout=10)
        if r.status_code == 404: raise ValueError("unknown key")
        r.raise_for_status(); self.KEY.write_text(key)

    def now(self):
        if not self.connected(): return None
        r = requests.get(self.API + self.key(), timeout=10); r.raise_for_status(); j = r.json()
        if not j.get("playing") or not j.get("title"): return None
        return {
            "id": "ym_" + re.sub(r"\W", "_", j["id"]), "title": j["title"], "artists": j["artist"],
            "album": j.get("album") or "", "duration_ms": int(j["duration"] * 1000),
            "progress_ms": int(j["progress"] * 1000), "is_playing": True,
            "cover": j.get("cover") or None, "thumb": j.get("cover") or None, "url": j.get("url"), "src": "yukimusic",
        }

ym = YukiMusic()

def any_connected(): return spot.connected() or ym.connected()

async def now():
    """Spotify if it's playing, else YukiMusic, else whatever Spotify reports (paused)."""
    sp, err = None, None
    try: sp = await asyncio.to_thread(spot.now)
    except Exception as e:
        log.warning("spotify: %s", e); err = str(e)
    if sp and sp["is_playing"]: return sp, None
    try:
        y = await asyncio.to_thread(ym.now)
        if y: return y, None
    except Exception as e:
        log.warning("yukimusic: %s", e); err = err or f"YukiMusic: {e}"
    return sp, err

# ───────────────────────── downloader (sync, run in threads) ─────────────────────────
BAD_WORDS = ("live", "cover", "remix", "slowed", "sped up", "reverb", "nightcore", "karaoke", "8d")

def _score(entry, tr):
    d = entry.get("duration")
    s = abs(d - tr["duration_ms"] / 1000) if d else 120
    t = (entry.get("title") or "").lower()
    for w in BAD_WORDS:
        if w in t and w not in tr["title"].lower(): s += 60
    return s

def fetch_image(url, path):
    if not url: return None
    if not path.exists():
        try: path.write_bytes(requests.get(url, timeout=15).content)
        except Exception: return None
    return path

def thumb_of(tr): return fetch_image(tr.get("thumb"), CACHE / f"{tr['id']}.jpg")
def cover_of(tr): return fetch_image(tr.get("cover"), CACHE / f"{tr['id']}_big.jpg")

def tag(mp3, tr, cover):
    try: t = ID3(mp3)
    except ID3NoHeaderError: t = ID3()
    t.add(TIT2(encoding=3, text=tr["title"])); t.add(TPE1(encoding=3, text=tr["artists"]))
    t.add(TALB(encoding=3, text=tr["album"]))
    if cover and cover.exists():
        t.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover", data=cover.read_bytes()))
    t.save(mp3)

def cleanup_cache():
    files = sorted(CACHE.glob("*.mp3"), key=lambda p: p.stat().st_mtime, reverse=True)
    for f in files[CACHE_KEEP:]:
        f.unlink(missing_ok=True)
        (CACHE / f"{f.stem}.jpg").unlink(missing_ok=True)
        (CACHE / f"{f.stem}_big.jpg").unlink(missing_ok=True)

def fetch_audio(tr):
    out = CACHE / f"{tr['id']}.mp3"
    if out.exists(): return out
    base = {"quiet": True, "no_warnings": True, "noplaylist": True}
    if (BASE / "cookies.txt").exists(): base["cookiefile"] = str(BASE / "cookies.txt")
    q = f"{tr['artists']} - {tr['title']}"
    errors = []
    for search in (f"ytsearch6:{q} audio", f"scsearch6:{q}"):
        src = search.split(":")[0]
        try:
            with yt_dlp.YoutubeDL({**base, "extract_flat": "in_playlist"}) as y:
                info = y.extract_info(search, download=False)
            ents = [e for e in (info.get("entries") or []) if e]
            if not ents: continue
            best = min(ents, key=lambda e: _score(e, tr))
            url = best.get("webpage_url") or best.get("url")
            opts = {**base, "format": "bestaudio/best",
                    "outtmpl": str(CACHE / f"{tr['id']}.%(ext)s"),
                    "postprocessors": [{"key": "FFmpegExtractAudio",
                                        "preferredcodec": "mp3", "preferredquality": "192"}]}
            with yt_dlp.YoutubeDL(opts) as y: y.download([url])
            if out.exists():
                tag(out, tr, thumb_of(tr)); cleanup_cache()
                log.info("downloaded %s via %s (%s)", q, src, url)
                return out
        except Exception as e:
            errors.append(f"{src}: {e}"); log.warning("download via %s failed: %s", src, e)
    raise RuntimeError("couldn't download: " + (" | ".join(errors)[:300] or "no results"))

# ───────────────────────── user session ─────────────────────────
class User:
    """Your own account — the only thing allowed to change profile music."""
    def __init__(self): self.c = None; self.phone = None; self.code_hash = None

    async def connect(self):
        self.c = TelegramClient(str(DATA / "user"), API_ID, API_HASH,
                                device_model="tgspotify", system_version="Linux")
        await self.c.connect()

    async def authorized(self):
        try: return await self.c.is_user_authorized()
        except Exception: return False

    async def send_code(self, phone):
        self.phone = phone
        self.c._phone_code_hash.pop(phone, None)  # fresh SendCode, never ResendCode
        self.code_hash = (await self.c.send_code_request(phone)).phone_code_hash

    async def sign_in(self, code):
        try:
            await self.c.sign_in(self.phone, code, phone_code_hash=self.code_hash); return "ok"
        except SessionPasswordNeededError:
            return "password"

    async def password(self, pw): await self.c.sign_in(password=pw)

    async def me(self):
        try:
            me = await self.c.get_me()
            return (me.first_name or "") + (f" (@{me.username})" if me.username else "")
        except Exception: return None

    async def logout(self):
        try: await self.c.log_out()
        except Exception: pass
        await self.connect()

    async def unsave(self, d):
        try: await self.c(SaveMusicRequest(dict_to_doc(d), unsave=True))
        except Exception as e: log.warning("unsave failed: %s", e)

    async def set_song(self, mp3, tr, thumb):
        msg = await self.c.send_file(
            "me", str(mp3), silent=True, thumb=str(thumb) if thumb else None,
            attributes=[DocumentAttributeAudio(duration=int(tr["duration_ms"] / 1000),
                                               title=tr["title"], performer=tr["artists"])])
        doc = tl_utils.get_input_document(msg.media.document)
        await self.c(SaveMusicRequest(doc))          # new song → top of the profile
        prev = S.get("last_doc")
        if prev and prev["id"] != doc.id:
            await self.unsave(prev)                   # drop the previous bot-added song
        if S.get("delete_upload"):
            try: await self.c.delete_messages("me", [msg.id])
            except Exception as e: log.warning("cleanup failed: %s", e)
        return doc_to_dict(doc)

    async def clear(self):
        prev = S.get("last_doc")
        if prev: await self.unsave(prev)
        S.set(last_doc=None, last_track=None)

U = User()
bot: TelegramClient = None
SYNC_LOCK = asyncio.Lock()
LAST_NOTIFY = {"mid": None}

async def sync(tr, force=False):
    async with SYNC_LOCK:
        if not force and S.get("last_track") == tr["id"]: return False
        mp3 = await asyncio.to_thread(fetch_audio, tr)
        thumb = await asyncio.to_thread(thumb_of, tr)
        doc = await U.set_song(mp3, tr, thumb)
        S.set(last_doc=doc, last_track=tr["id"], last_title=f"{tr['artists']} — {tr['title']}",
              last_sync=time.time())
        log.info("profile music -> %s", S.get("last_title"))
        return True

async def notify(tr):
    if not S.get("notify"): return
    try:
        if LAST_NOTIFY["mid"]: await bot.delete_messages(OWNER_ID, LAST_NOTIFY["mid"])
    except Exception: pass
    try:
        m = await bot.send_message(OWNER_ID, f"{E('sparkle')} Now on your profile: "
                                   f"<b>{esc(tr['title'])}</b> — {esc(tr['artists'])}", silent=True)
        LAST_NOTIFY["mid"] = m.id
    except Exception: pass

async def worker():
    failed = {"id": None, "at": 0}; paused_since = None
    while True:
        try:
            if S.get("auto") and any_connected() and await U.authorized():
                tr, _ = await now()
                if tr and tr["is_playing"]:
                    paused_since = None
                    retry_blocked = failed["id"] == tr["id"] and time.time() - failed["at"] < 600
                    if tr["id"] != S.get("last_track") and not retry_blocked:
                        try:
                            if await sync(tr): await notify(tr)
                        except Exception as e:
                            failed.update(id=tr["id"], at=time.time()); log.error("sync failed: %s", e)
                elif S.get("clear_on_pause") and S.get("last_doc"):
                    paused_since = paused_since or time.time()
                    if time.time() - paused_since > 60:
                        await U.clear(); log.info("cleared profile music (paused)")
        except Exception:
            log.error(traceback.format_exc())
        await asyncio.sleep(POLL)

# ───────────────────────── views ─────────────────────────
KEEP = object()  # keep the current media, change only text/caption

async def home_view():
    tr, err = await now()
    sp_ok, ym_ok, tg_ok = spot.connected(), ym.connected(), await U.authorized()
    head = f"{E('logo')} <b>Spotify / YukiMusic → Profile music</b>\n\n"
    photo = None
    if tr:
        st = E("play") if tr["is_playing"] else E("pause")
        body = (f"{E('disc')} <b>{esc(tr['title'])}</b>\n"
                f"{E('artist')} {esc(tr['artists'])}\n"
                f"{E('album')} <i>{esc(tr['album'])}</i>\n\n"
                f"{st} <code>{bar(tr['progress_ms'], tr['duration_ms'])}</code> "
                f"{fmt(tr['progress_ms'])} / {fmt(tr['duration_ms'])}\n")
        if S.get("last_track") == tr["id"]: body += f"{E('heart')} <i>on your profile now</i>\n"
        photo = PHOTOS.get(tr["id"]) or await asyncio.to_thread(cover_of, tr)
    elif not (sp_ok or ym_ok):
        body = f"{E('spotify')} Connect Spotify or YukiMusic to get started.\n"
    elif err:
        body = f"{E('error')} Spotify error: <code>{esc(err[:150])}</code>\n"
    else:
        body = f"{E('pause')} <i>Nothing is playing right now</i>\n"
    yes = lambda b: E("on") if b else E("off")
    foot = f"\n{yes(S.get('auto'))} Auto-sync   {yes(sp_ok)} Spotify   {yes(ym_ok)} YukiMusic   {yes(tg_ok)} Telegram"
    if S.get("last_title"):
        foot += (f"\n{E('time')} Last: {esc(S.get('last_title'))} · "
                 f"{time.strftime('%H:%M', time.localtime(S.get('last_sync') or 0))}")

    rows = []
    if not (sp_ok or ym_ok): rows.append([btn("Connect Spotify", "sp_login", "spotify", "success"),
                                          btn("Connect YukiMusic", "ym_login", "yukimusic", "success")])
    if not tg_ok: rows.append([btn("Log in to Telegram", "tg_login", "telegram", "primary")])
    if tr:
        rows.append([btn("Sync now", "sync", "sync", "primary"), btn("Download", "dl", "download")])
        if tr.get("url"):
            src = "yukimusic" if tr.get("src") == "yukimusic" else "spotify"
            rows.append([btn("Open in " + ("YukiMusic" if src == "yukimusic" else "Spotify"), url=tr["url"], emoji=src)])
    auto = S.get("auto")
    rows.append([btn("Auto-sync: " + ("ON" if auto else "OFF"), "auto", "on" if auto else "off",
                     "success" if auto else "danger"),
                 btn("Refresh", "home", "refresh")])
    rows.append([btn("Settings", "set", "settings")])
    return photo, head + body + foot, rows, (tr["id"] if tr else None)

async def settings_view():
    tg_name = await U.me() if await U.authorized() else None
    sw = lambda k: "ON" if S.get(k) else "OFF"
    text = (f"{E('settings')} <b>Settings</b>\n\n"
            f"{E('telegram')} Telegram: <b>{esc(tg_name) if tg_name else 'not logged in'}</b>\n"
            f"{E('spotify')} Spotify: <b>{'connected' if spot.connected() else 'not connected'}</b>\n"
            f"{E('yukimusic')} YukiMusic: <b>{'connected' if ym.connected() else 'not connected'}</b>\n\n"
            "• <b>Clear on pause</b> — remove the song from profile when nothing plays\n"
            "• <b>Hide uploads</b> — delete the helper upload from Saved Messages\n"
            "• <b>Notify</b> — ping me on every change\n"
            "• <b>Premium look</b> — premium emoji + coloured buttons (bot owner needs Premium)")
    if S.get("premium") and not RUNTIME["premium_works"]:
        text += f"\n\n{E('error')} Telegram rejected premium bits — plain look until restart."
    st = lambda k: "success" if S.get(k) else None
    rows = [
        [btn(f"Clear on pause: {sw('clear_on_pause')}", "t:clear_on_pause", style=st("clear_on_pause")),
         btn(f"Hide uploads: {sw('delete_upload')}", "t:delete_upload", style=st("delete_upload"))],
        [btn(f"Notify: {sw('notify')}", "t:notify", style=st("notify")),
         btn(f"Premium look: {sw('premium')}", "t:premium", style=st("premium"))],
        [btn("Premium emoji", "emo", "emoji")],
        [btn("Remove song from profile", "clear", "trash")],
        [btn("Log out Spotify", "sp_out", style="danger") if spot.connected()
         else btn("Connect Spotify", "sp_login", "spotify", "success"),
         btn("Log out YukiMusic", "ym_out", style="danger") if ym.connected()
         else btn("Connect YukiMusic", "ym_login", "yukimusic", "success")],
        [btn("Log out Telegram", "tg_out", style="danger") if tg_name
         else btn("Log in Telegram", "tg_login", "telegram", "primary")],
        [btn("Back", "home", "back")],
    ]
    return KEEP, text, rows, None

async def emoji_view():
    have = S.get("emoji", {})
    lines = []
    for k, ch in EMOJI.items():
        mark = f'<tg-emoji emoji-id="{have[k]}">{ch}</tg-emoji>' if k in have and premium_on() else ch
        lines.append(f"{mark} <code>{k}</code>{' ✓' if k in have else ''}")
    text = (f"{E('emoji')} <b>Premium emoji</b>\n\n"
            "Tap a slot, then send me any premium emoji — I'll store its ID.\n"
            "Or send premium emoji anytime and I'll tell you their IDs.\n\n" + "\n".join(lines))
    keys = list(EMOJI)
    rows = [[btn(f"{EMOJI[k]} {k}", f"emo:{k}") for k in keys[i:i + 3]] for i in range(0, len(keys), 3)]
    rows.append([btn("Reset all", "emo_reset", "trash", "danger"), btn("Back", "set", "back")])
    return KEEP, text, rows, None

# ───────────────────────── rendering ─────────────────────────
def _remember_photo(msg, track_id):
    if track_id and msg is not None and isinstance(getattr(msg, "media", None), MessageMediaPhoto):
        PHOTOS[track_id] = msg.media.photo
        if len(PHOTOS) > 50: PHOTOS.pop(next(iter(PHOTOS)))

async def _send(chat, photo, text, mk):
    if photo: return await bot.send_file(chat, photo, caption=text, buttons=mk)
    return await bot.send_message(chat, text, buttons=mk, link_preview=False)

async def show(chat, view, msg=None):
    for attempt in (1, 2):
        photo, text, rows, track_id = await view()
        mk = markup(rows)
        try:
            if msg is None:
                out = await _send(chat, None if photo is KEEP else photo, text, mk)
            else:
                has_photo = isinstance(msg.media, MessageMediaPhoto)
                if photo is KEEP or (not photo and not has_photo):
                    out = await bot.edit_message(chat, msg.id, text, buttons=mk, link_preview=False)
                elif photo and has_photo:
                    out = await bot.edit_message(chat, msg.id, text, file=photo, buttons=mk)
                else:
                    try: await bot.delete_messages(chat, msg.id)
                    except Exception: pass
                    out = await _send(chat, photo, text, mk)
            _remember_photo(out, track_id)
            return out
        except MessageNotModifiedError:
            return msg
        except Exception as e:
            if attempt == 1 and premium_on():
                log.warning("send failed with premium look (%s) — retrying plain", e)
                RUNTIME["premium_works"] = False
                continue
            raise

# ───────────────────────── actions ─────────────────────────
async def do_sync(chat, ev=None):
    tr, err = await now()
    if not tr:
        t = "Spotify error" if err else "Nothing is playing"
        return await (ev.answer(t) if ev else bot.send_message(chat, f"{E('pause')} {t}"))
    if not await U.authorized():
        t = "Log in to Telegram first"
        return await (ev.answer(t, alert=True) if ev else bot.send_message(chat, t))
    if ev: await ev.answer("Syncing… 🎵")
    status = await bot.send_message(chat, f"{E('sync')} Fetching <b>{esc(tr['title'])}</b>…", silent=True)
    try:
        await sync(tr, force=True)
        await status.edit(f"{E('heart')} <b>{esc(tr['title'])}</b> is on your profile now")
    except Exception as e:
        await status.edit(f"{E('error')} Sync failed:\n<code>{esc(str(e)[:400])}</code>")
    if ev:
        try: await show(chat, home_view, await ev.get_message())
        except Exception: pass

async def do_download(chat, ev):
    tr, _ = await now()
    if not tr: return await ev.answer("Nothing is playing")
    await ev.answer("Downloading… 📥")
    try:
        async with bot.action(chat, "audio"):
            mp3 = await asyncio.to_thread(fetch_audio, tr)
            thumb = await asyncio.to_thread(thumb_of, tr)
            await bot.send_file(
                chat, str(mp3), thumb=str(thumb) if thumb else None,
                caption=f"{E('music')} <b>{esc(tr['title'])}</b> — {esc(tr['artists'])}",
                attributes=[DocumentAttributeAudio(duration=int(tr["duration_ms"] / 1000),
                                                   title=tr["title"], performer=tr["artists"])])
    except Exception as e:
        await bot.send_message(chat, f"{E('error')} Download failed:\n<code>{esc(str(e)[:400])}</code>")

# ───────────────────────── handlers ─────────────────────────
async def on_callback(ev):
    if ev.sender_id != OWNER_ID: return await ev.answer("Private bot")
    d, chat = ev.data.decode(), ev.chat_id
    try:
        msg = await ev.get_message()
        if d == "home": await ev.answer(); await show(chat, home_view, msg)
        elif d == "set": await ev.answer(); await show(chat, settings_view, msg)
        elif d == "emo": await ev.answer(); await show(chat, emoji_view, msg)
        elif d == "sync": await do_sync(chat, ev)
        elif d == "dl": await do_download(chat, ev)
        elif d == "auto":
            S.set(auto=not S.get("auto"))
            await ev.answer("Auto-sync " + ("on ✨" if S.get("auto") else "off"))
            await show(chat, home_view, msg)
        elif d.startswith("t:"):
            k = d[2:]; S.set(**{k: not S.get(k)})
            if k == "premium": RUNTIME["premium_works"] = True
            await ev.answer("Saved"); await show(chat, settings_view, msg)
        elif d.startswith("emo:"):
            k = d[4:]; STATE[chat] = f"emoji:{k}"
            await ev.answer()
            await bot.send_message(chat, f"{EMOJI[k]} Send me a premium emoji for <code>{k}</code> (or /cancel)")
        elif d == "emo_reset":
            S.set(emoji={}); await ev.answer("Reset"); await show(chat, emoji_view, msg)
        elif d == "clear":
            if await U.authorized(): await U.clear()
            await ev.answer("Removed from profile"); await show(chat, settings_view, msg)
        elif d == "sp_login":
            await ev.answer(); STATE[chat] = "spotify"
            await bot.send_message(chat,
                f"{E('spotify')} <b>Connect Spotify</b>\n\n"
                "1. Tap the button and allow access\n"
                "2. You'll land on a page that won't load — that's fine\n"
                f"3. Copy the <b>whole address</b> (starts with <code>{esc(SP_REDIRECT)}</code>) and send it here",
                buttons=markup([[btn("Authorize Spotify", url=spot.auth_url(), emoji="key")]]))
        elif d == "ym_login":
            await ev.answer(); STATE[chat] = "yukimusic"
            await bot.send_message(chat, f"{E('yukimusic')} <b>Connect YukiMusic</b>\n\n"
                                   "In YukiMusic open <b>Settings → Telegram profile music</b>, tap <b>Copy key</b> "
                                   "and send the key here.")
        elif d == "ym_out":
            ym.logout(); await ev.answer("YukiMusic disconnected"); await show(chat, settings_view, msg)
        elif d == "sp_out":
            spot.logout(); await ev.answer("Spotify disconnected"); await show(chat, settings_view, msg)
        elif d == "tg_login":
            await ev.answer(); STATE[chat] = "phone"
            await bot.send_message(chat, f"{E('telegram')} <b>Telegram login</b>\n\n"
                                   "This logs in <b>your account</b> (needed to change profile music — bots can't).\n"
                                   "Send your phone number, e.g. <code>+31612345678</code>")
        elif d == "tg_out":
            await U.logout(); S.set(last_doc=None, last_track=None)
            await ev.answer("Logged out"); await show(chat, settings_view, msg)
        else:
            await ev.answer()
    except Exception as e:
        log.error(traceback.format_exc())
        try: await ev.answer(f"Error: {str(e)[:150]}", alert=True)
        except Exception: pass

async def on_message(ev):
    if not ev.is_private or ev.sender_id != OWNER_ID: return
    chat, text, st = ev.chat_id, (ev.raw_text or "").strip(), STATE.get(ev.chat_id)
    ids = [e.document_id for e in (ev.message.entities or []) if isinstance(e, MessageEntityCustomEmoji)]
    cmd = text.split()[0].split("@")[0].lower() if text.startswith("/") else None
    try:
        if cmd in ("/start", "/panel"):
            STATE.pop(chat, None); return await show(chat, home_view)
        if cmd == "/cancel":
            STATE.pop(chat, None); return await ev.reply(f"{E('back')} Cancelled.")
        if cmd == "/sync":
            return await do_sync(chat)

        if st and st.startswith("emoji:"):
            if not ids: return await ev.reply("That's not a premium emoji — try again or /cancel")
            k = st[6:]; em = dict(S.get("emoji", {})); em[k] = str(ids[0]); S.set(emoji=em)
            RUNTIME["premium_works"] = True; STATE.pop(chat, None)
            return await show(chat, emoji_view)
        if ids:
            lines = "\n".join(f'<tg-emoji emoji-id="{i}">⭐</tg-emoji> <code>{i}</code>' for i in dict.fromkeys(ids))
            return await ev.reply(f"{E('emoji')} Custom emoji IDs:\n{lines}")
        if st == "yukimusic":
            try: await ev.delete()
            except Exception: pass
            try: await asyncio.to_thread(ym.link, text)
            except Exception as e:
                return await bot.send_message(chat, f"{E('error')} That key didn't work ({esc(str(e)[:80])}). Copy it again and resend, or /cancel.")
            STATE.pop(chat, None)
            await bot.send_message(chat, f"{E('on')} YukiMusic connected!")
            return await show(chat, home_view)
        if st == "spotify":
            await asyncio.to_thread(spot.finish, text); STATE.pop(chat, None)
            await bot.send_message(chat, f"{E('on')} Spotify connected!")
            return await show(chat, home_view)
        if st == "phone":
            await U.send_code(re.sub(r"[^\d+]", "", text)); STATE[chat] = "code"
            return await bot.send_message(chat, f"{E('key')} Code sent to your Telegram app.\n"
                                          "Type it <b>with letters between digits</b>, e.g. <code>1a2b3c4d5</code> — "
                                          "Telegram kills codes sent as plain digits.")
        if st == "code":
            try: await ev.delete()
            except Exception: pass
            try: res = await U.sign_in(re.sub(r"\D", "", text))
            except (PhoneCodeExpiredError, PhoneCodeInvalidError) as e:
                if isinstance(e, PhoneCodeExpiredError): await U.send_code(U.phone)
                return await bot.send_message(chat, f"{E('key')} " + ("Code expired — I sent a new one." if isinstance(e, PhoneCodeExpiredError) else "Wrong code, try again.") +
                                              " Type it with letters between digits, e.g. <code>1a2b3c4d5</code>.")
            if res == "password":
                STATE[chat] = "password"
                return await bot.send_message(chat, f"{E('key')} 2FA is on — send your cloud password (I'll delete it).")
            STATE.pop(chat, None)
            await bot.send_message(chat, f"{E('on')} Logged in as <b>{esc(await U.me())}</b>")
            return await show(chat, home_view)
        if st == "password":
            try: await ev.delete()
            except Exception: pass
            await U.password(text); STATE.pop(chat, None)
            await bot.send_message(chat, f"{E('on')} Logged in as <b>{esc(await U.me())}</b>")
            return await show(chat, home_view)
        await show(chat, home_view)
    except Exception as e:
        log.error(traceback.format_exc())
        await bot.send_message(chat, f"{E('error')} <code>{esc(str(e)[:400])}</code>\nTry again or /cancel")

# ───────────────────────── main ─────────────────────────
async def main():
    global bot
    bot = TelegramClient(str(DATA / "bot"), API_ID, API_HASH)
    bot.parse_mode = "html"
    await bot.start(bot_token=BOT_TOKEN)
    await U.connect()
    bot.add_event_handler(on_message, events.NewMessage(incoming=True))
    bot.add_event_handler(on_callback, events.CallbackQuery())
    try:
        await bot(functions.bots.SetBotCommandsRequest(
            scope=BotCommandScopeDefault(), lang_code="",
            commands=[BotCommand("start", "Open the panel"),
                      BotCommand("sync", "Put the current song on my profile"),
                      BotCommand("cancel", "Cancel current input")]))
    except Exception as e: log.warning("set commands: %s", e)
    asyncio.create_task(worker())
    log.info("tgspotify started")
    try: await bot.send_message(OWNER_ID, f"{E('sparkle')} tgspotify is up — /start", silent=True)
    except Exception: pass
    await bot.run_until_disconnected()

if __name__ == "__main__":
    asyncio.run(main())
