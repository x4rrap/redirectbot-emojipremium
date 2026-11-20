#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
╔══════════════════════════════════════════════════════════════════╗
║  REDIRECT BOT  –  Telegram  (aiogram 3.x)  –  file unico         ║
╠══════════════════════════════════════════════════════════════════╣
║  Funzioni:                                                       ║
║   • Redirect verso gruppi/canali con link d'invito a scadenza    ║
║   • Link personale per utente (monouso o con limite impostabile) ║
║   • Auto-eliminazione chat subito dopo l'invio del link          ║
║   • Autodistruzione del messaggio link + revoca link scaduti     ║
║   • Emoji premium (custom emoji) salvate per ID nel database     ║
║   • Foto + testi + bottoni configurabili dal pannello admin      ║
║   • Pannello admin completo (gruppi, bottoni, emoji, stats...)   ║
║   • Broadcast, ban/unban, cooldown anti-spam                     ║
║   • Autodistruzione totale del bot (wipe DB, revoca link)        ║
║   • Code di eliminazione salvate su DB (sopravvivono ai riavvii) ║
╠══════════════════════════════════════════════════════════════════╣
║  INSTALLAZIONE:   pip install -U aiogram                         ║
║  AVVIO:           python redirect_bot.py                         ║
║  Configura SOLO le 2 variabili qui sotto (BOT_TOKEN e ADMIN_ID)  ║
╚══════════════════════════════════════════════════════════════════╝
"""

# ═════════════════════════ CONFIGURAZIONE ═════════════════════════
BOT_TOKEN = "9292884821:AAHGYAxbC0OFJz934FA0U46j2G2nsGaOjy4"
ADMIN_ID = 8211955017  # il tuo ID Telegram numerico
# ═══════════════════════════════════════════════════════════════════

import asyncio
import html
import logging
import os
import re
import sqlite3
import threading
import time
from contextlib import suppress

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
)
from aiogram.filters import BaseFilter, Command, CommandObject, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    ChatJoinRequest,
    ChatMemberUpdated,
    ErrorEvent,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    KeyboardButtonRequestChat,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)

# Permette di sovrascrivere da variabili d'ambiente (opzionale)
BOT_TOKEN = os.getenv("BOT_TOKEN", BOT_TOKEN)
ADMIN_ID = int(os.getenv("ADMIN_ID", ADMIN_ID))
ADMIN_IDS = {ADMIN_ID}

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "redirect_bot.db")
DELETE_SCRIPT_ON_BOOM = False  # True = l'autodistruzione cancella anche questo file .py

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("redirect_bot")

# ═══════════════════════════ DEFAULTS ═════════════════════════════
DEFAULT_WELCOME = (
    "👋 <b>Benvenuto {nome}!</b>\n\n"
    "Scegli qui sotto il gruppo a cui vuoi accedere.\n"
    "🔒 Il link è personale e resta valido solo <b>{minuti} minuti</b>."
)
DEFAULT_LINK = (
    "✅ <b>Link pronto!</b>\n\n"
    "📌 Gruppo: <b>{gruppo}</b>\n"
    "⏳ Valido per <b>{minuti} minuti</b>\n"
    "{modo}"
    "💥 Questo messaggio si autodistrugge tra <b>{secondi} secondi</b>."
)
DEFAULT_SETTINGS = {
    "welcome_text": DEFAULT_WELCOME,
    "link_text": DEFAULT_LINK,
    "welcome_photo": "",
    "link_photo": "",
    "link_minutes": "5",      # validità del link d'invito
    "delete_delay": "45",     # secondi prima che il messaggio col link sparisca
    "member_limit": "1",      # utenti max per link (0 = illimitato)
    "cooldown": "8",          # secondi tra una richiesta link e l'altra
    "join_request": "1",      # 1 = richiesta di ingresso: NESSUNA auto-accettazione, approvi tu
    "notify_user": "1",       # 1 = avvisa l'utente quando accetti/rifiuti la richiesta
}

# Impostazioni numeriche modificabili: chiave -> (etichetta, minimo, massimo)
NUM_META = {
    "link_minutes": ("⏱ Validità link (minuti)", 1, 10080),
    "delete_delay": ("🗑 Eliminazione messaggio link (secondi)", 3, 3600),
    "member_limit": ("👤 Max utenti per link (0 = illimitato)", 0, 99999),
    "cooldown": ("🧊 Cooldown tra due link (secondi)", 0, 3600),
}
TEXT_META = {
    "welcome_text": "📝 Testo di benvenuto",
    "link_text": "📝 Testo del messaggio con link",
}
PHOTO_META = {
    "welcome_photo": "🖼 Foto di benvenuto",
    "link_photo": "🖼 Foto del messaggio con link",
}


# ═════════════════════════════ UTILS ══════════════════════════════
def now() -> int:
    return int(time.time())


def esc(s) -> str:
    return html.escape(str(s if s is not None else ""))


def strip_tags(s: str) -> str:
    return re.sub(r"<[^>]+>", "", s or "")


def fmt_ts(ts: int) -> str:
    return time.strftime("%d/%m/%Y %H:%M", time.localtime(ts))


# ═════════════════════════════ DATABASE ═══════════════════════════
class DB:
    """Wrapper SQLite thread-safe (autocommit + WAL)."""

    def __init__(self, path: str):
        self.path = path
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        with self.lock:
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA synchronous=NORMAL")
        self.create()

    # ---- base ----
    def exe(self, sql, params=()):
        with self.lock:
            cur = self.conn.execute(sql, params)
            return cur.lastrowid

    def one(self, sql, params=()):
        with self.lock:
            return self.conn.execute(sql, params).fetchone()

    def all(self, sql, params=()):
        with self.lock:
            return self.conn.execute(sql, params).fetchall()

    def create(self):
        schema = """
        CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE IF NOT EXISTS emojis(
            name TEXT PRIMARY KEY, emoji_id TEXT NOT NULL, fallback TEXT, created INTEGER);
        CREATE TABLE IF NOT EXISTS groups(
            id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER UNIQUE,
            title TEXT, label TEXT, active INTEGER DEFAULT 1,
            clicks INTEGER DEFAULT 0, created INTEGER);
        CREATE TABLE IF NOT EXISTS buttons(
            id INTEGER PRIMARY KEY AUTOINCREMENT, text TEXT, url TEXT,
            emoji_name TEXT, style TEXT, pos INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS users(
            user_id INTEGER PRIMARY KEY, first_name TEXT, username TEXT,
            first_seen INTEGER, last_seen INTEGER, banned INTEGER DEFAULT 0,
            last_link INTEGER DEFAULT 0, links INTEGER DEFAULT 0, blocked INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS links(
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, group_id INTEGER,
            chat_id INTEGER, link TEXT, expire INTEGER, created INTEGER,
            revoked INTEGER DEFAULT 0, jr INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS join_requests(
            id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER, user_id INTEGER,
            first_name TEXT, username TEXT, group_id INTEGER, link TEXT,
            created INTEGER, status TEXT DEFAULT 'pending', handled INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS deletions(
            id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER,
            message_id INTEGER, due INTEGER);
        CREATE TABLE IF NOT EXISTS session_msgs(
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER,
            chat_id INTEGER, message_id INTEGER);
        CREATE INDEX IF NOT EXISTS idx_del_due ON deletions(due);
        CREATE INDEX IF NOT EXISTS idx_links_exp ON links(revoked, expire);
        CREATE INDEX IF NOT EXISTS idx_sess_user ON session_msgs(user_id);
        CREATE INDEX IF NOT EXISTS idx_jr ON join_requests(status, created);
        """
        with self.lock:
            self.conn.executescript(schema)
            # migrazione per database creati con versioni precedenti
            cols = [r["name"] for r in self.conn.execute("PRAGMA table_info(links)")]
            if "jr" not in cols:
                self.conn.execute("ALTER TABLE links ADD COLUMN jr INTEGER DEFAULT 0")

    # ---- settings ----
    def get(self, key: str) -> str:
        r = self.one("SELECT value FROM settings WHERE key=?", (key,))
        if r is None:
            return DEFAULT_SETTINGS.get(key, "")
        return r["value"]

    def geti(self, key: str) -> int:
        try:
            return int(self.get(key))
        except (TypeError, ValueError):
            return int(DEFAULT_SETTINGS.get(key, "0") or 0)

    def set(self, key: str, value: str):
        self.exe(
            "INSERT INTO settings(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )

    # ---- emoji premium ----
    def emoji_save(self, name: str, emoji_id: str, fallback: str):
        self.exe(
            "INSERT INTO emojis(name,emoji_id,fallback,created) VALUES(?,?,?,?) "
            "ON CONFLICT(name) DO UPDATE SET emoji_id=excluded.emoji_id, fallback=excluded.fallback",
            (name, emoji_id, fallback or "⭐", now()),
        )

    def emoji_get(self, name):
        if not name:
            return None
        return self.one("SELECT * FROM emojis WHERE name=?", (name,))

    def emoji_list(self):
        return self.all("SELECT * FROM emojis ORDER BY created, name")

    def emoji_del(self, name: str):
        self.exe("DELETE FROM emojis WHERE name=?", (name,))

    def emoji_map(self) -> dict:
        return {r["name"]: (r["emoji_id"], r["fallback"]) for r in self.emoji_list()}

    def emoji_next_index(self) -> int:
        r = self.one("SELECT COUNT(*) c FROM emojis")
        n = (r["c"] if r else 0) + 1
        while self.emoji_get(f"e{n}"):
            n += 1
        return n

    # ---- gruppi ----
    def group_add(self, chat_id: int, title: str, label: str = None) -> int:
        r = self.one("SELECT id FROM groups WHERE chat_id=?", (chat_id,))
        if r:
            self.exe("UPDATE groups SET title=?, active=1 WHERE chat_id=?", (title, chat_id))
            return r["id"]
        return self.exe(
            "INSERT INTO groups(chat_id,title,label,active,created) VALUES(?,?,?,1,?)",
            (chat_id, title, label or title, now()),
        )

    def group_get(self, gid: int):
        return self.one("SELECT * FROM groups WHERE id=?", (gid,))

    def group_by_chat(self, chat_id: int):
        return self.one("SELECT * FROM groups WHERE chat_id=?", (chat_id,))

    def groups(self, only_active=False):
        sql = "SELECT * FROM groups"
        if only_active:
            sql += " WHERE active=1"
        return self.all(sql + " ORDER BY id")

    def group_set(self, gid: int, field: str, value):
        assert field in ("label", "active", "title")
        self.exe(f"UPDATE groups SET {field}=? WHERE id=?", (value, gid))

    def group_del(self, gid: int):
        self.exe("DELETE FROM groups WHERE id=?", (gid,))

    def group_click(self, gid: int):
        self.exe("UPDATE groups SET clicks=clicks+1 WHERE id=?", (gid,))

    # ---- bottoni ----
    def button_add(self, text, url, emoji_name=None, style=None):
        r = self.one("SELECT COALESCE(MAX(pos),0)+1 p FROM buttons")
        return self.exe(
            "INSERT INTO buttons(text,url,emoji_name,style,pos) VALUES(?,?,?,?,?)",
            (text, url, emoji_name, style, r["p"]),
        )

    def buttons(self):
        return self.all("SELECT * FROM buttons ORDER BY pos, id")

    def button_del(self, bid: int):
        self.exe("DELETE FROM buttons WHERE id=?", (bid,))

    # ---- utenti ----
    def user_touch(self, u):
        t = now()
        self.exe(
            "INSERT INTO users(user_id,first_name,username,first_seen,last_seen) VALUES(?,?,?,?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET first_name=excluded.first_name, "
            "username=excluded.username, last_seen=excluded.last_seen, blocked=0",
            (u.id, u.first_name or "", u.username or "", t, t),
        )

    def user_get(self, uid: int):
        return self.one("SELECT * FROM users WHERE user_id=?", (uid,))

    def user_banned(self, uid: int) -> bool:
        r = self.user_get(uid)
        return bool(r and r["banned"])

    def user_ban(self, uid: int, flag: int):
        self.exe(
            "INSERT INTO users(user_id,first_name,username,first_seen,last_seen,banned) "
            "VALUES(?,?,?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET banned=excluded.banned",
            (uid, "", "", now(), now(), flag),
        )

    def user_blocked(self, uid: int):
        self.exe("UPDATE users SET blocked=1 WHERE user_id=?", (uid,))

    def user_ids(self):
        return [r["user_id"] for r in self.all("SELECT user_id FROM users WHERE banned=0 AND blocked=0")]

    # ---- link ----
    def link_log(self, user_id, group_id, chat_id, link, expire, jr=0):
        self.exe(
            "INSERT INTO links(user_id,group_id,chat_id,link,expire,created,jr) VALUES(?,?,?,?,?,?,?)",
            (user_id, group_id, chat_id, link, expire, now(), jr),
        )
        self.exe(
            "UPDATE users SET last_link=?, links=links+1 WHERE user_id=?", (now(), user_id)
        )

    def link_known(self, link: str) -> bool:
        return bool(self.one("SELECT 1 FROM links WHERE link=?", (link,)))

    def links_expired(self, t: int):
        return self.all(
            "SELECT id,chat_id,link FROM links WHERE revoked=0 AND jr=0 AND expire<=? LIMIT 50", (t,)
        )

    def links_active(self):
        return self.all("SELECT id,chat_id,link FROM links WHERE revoked=0")

    def link_revoked(self, lid: int):
        self.exe("UPDATE links SET revoked=1 WHERE id=?", (lid,))

    def link_get(self, link: str):
        return self.one("SELECT * FROM links WHERE link=?", (link,))

    def links_jr_close(self, t: int):
        """I link in modalità richiesta NON vengono revocati: le richieste in attesa restano valide."""
        self.exe("UPDATE links SET revoked=1 WHERE revoked=0 AND jr=1 AND expire<=?", (t,))

    # ---- richieste di ingresso (approvazione MANUALE) ----
    def jr_add(self, chat_id, user_id, first_name, username, group_id, link):
        r = self.one(
            "SELECT id FROM join_requests WHERE chat_id=? AND user_id=? AND status='pending'",
            (chat_id, user_id),
        )
        if r:
            self.exe("UPDATE join_requests SET created=?, link=? WHERE id=?", (now(), link, r["id"]))
            return r["id"], False
        rid = self.exe(
            "INSERT INTO join_requests(chat_id,user_id,first_name,username,group_id,link,created) "
            "VALUES(?,?,?,?,?,?,?)",
            (chat_id, user_id, first_name, username, group_id, link, now()),
        )
        return rid, True

    def jr_get(self, rid: int):
        return self.one("SELECT * FROM join_requests WHERE id=?", (rid,))

    def jr_pending(self, limit: int = 8):
        return self.all(
            "SELECT * FROM join_requests WHERE status='pending' ORDER BY created LIMIT ?", (limit,)
        )

    def jr_pending_all(self):
        return self.jr_pending(500)

    def jr_pending_count(self) -> int:
        return self.one("SELECT COUNT(*) c FROM join_requests WHERE status='pending'")["c"]

    def jr_set(self, rid: int, status: str):
        self.exe("UPDATE join_requests SET status=?, handled=? WHERE id=?", (status, now(), rid))

    # ---- code eliminazione ----
    def del_schedule(self, chat_id: int, message_id: int, due: int):
        self.exe(
            "INSERT INTO deletions(chat_id,message_id,due) VALUES(?,?,?)",
            (chat_id, message_id, due),
        )

    def del_due(self, t: int):
        return self.all(
            "SELECT id,chat_id,message_id FROM deletions WHERE due<=? ORDER BY due LIMIT 50", (t,)
        )

    def del_remove(self, rid: int):
        self.exe("DELETE FROM deletions WHERE id=?", (rid,))

    # ---- messaggi di sessione (da ripulire) ----
    def track(self, user_id: int, chat_id: int, message_id: int):
        self.exe(
            "INSERT INTO session_msgs(user_id,chat_id,message_id) VALUES(?,?,?)",
            (user_id, chat_id, message_id),
        )

    def session_pop(self, user_id: int):
        with self.lock:
            rows = self.conn.execute(
                "SELECT chat_id,message_id FROM session_msgs WHERE user_id=?", (user_id,)
            ).fetchall()
            self.conn.execute("DELETE FROM session_msgs WHERE user_id=?", (user_id,))
        return [(r["chat_id"], r["message_id"]) for r in rows]

    def session_all(self):
        rows = self.all("SELECT chat_id,message_id FROM session_msgs")
        return [(r["chat_id"], r["message_id"]) for r in rows]

    # ---- statistiche ----
    def stats(self) -> dict:
        d = {}
        d["users"] = self.one("SELECT COUNT(*) c FROM users")["c"]
        d["banned"] = self.one("SELECT COUNT(*) c FROM users WHERE banned=1")["c"]
        d["blocked"] = self.one("SELECT COUNT(*) c FROM users WHERE blocked=1")["c"]
        d["new24"] = self.one(
            "SELECT COUNT(*) c FROM users WHERE first_seen>=?", (now() - 86400,)
        )["c"]
        d["links"] = self.one("SELECT COUNT(*) c FROM links")["c"]
        d["links24"] = self.one(
            "SELECT COUNT(*) c FROM links WHERE created>=?", (now() - 86400,)
        )["c"]
        d["active_links"] = self.one(
            "SELECT COUNT(*) c FROM links WHERE revoked=0 AND expire>?", (now(),)
        )["c"]
        d["groups"] = self.one("SELECT COUNT(*) c FROM groups WHERE active=1")["c"]
        d["emojis"] = self.one("SELECT COUNT(*) c FROM emojis")["c"]
        return d

    # ---- wipe totale ----
    def wipe(self):
        with self.lock:
            for t in (
                "settings", "emojis", "groups", "buttons", "users",
                "links", "deletions", "session_msgs", "join_requests",
            ):
                self.conn.execute(f"DELETE FROM {t}")
            with suppress(Exception):
                self.conn.execute("VACUUM")

    def close(self):
        with suppress(Exception):
            self.conn.close()


db = DB(DB_PATH)
bot: Bot = None  # type: ignore
BOT_USERNAME = ""
STOP = False
WIPED = False


# ══════════════════════ RENDER TESTI / EMOJI PREMIUM ═══════════════
def render(text: str, **variables) -> str:
    """Sostituisce {e:nome} con <tg-emoji> premium e le variabili {nome}, {minuti}..."""
    emap = db.emoji_map()

    def rep(m):
        item = emap.get(m.group(1))
        if not item:
            return ""
        return f'<tg-emoji emoji-id="{item[0]}">{esc(item[1])}</tg-emoji>'

    text = re.sub(r"\{e:([A-Za-z0-9_\-]+)\}", rep, text or "")
    for k, v in variables.items():
        text = text.replace("{" + k + "}", str(v))
    return text


def extract_custom_emojis(msg: Message):
    """Ritorna [(emoji_id, carattere_fallback)] trovati nel messaggio."""
    out = []
    text = msg.text or msg.caption or ""
    ents = msg.entities or msg.caption_entities or []
    for e in ents:
        if e.type == "custom_emoji" and getattr(e, "custom_emoji_id", None):
            fb = e.extract_from(text) if text else ""
            out.append((e.custom_emoji_id, fb or "⭐"))
    st = getattr(msg, "sticker", None)
    if st is not None and getattr(st, "custom_emoji_id", None):
        out.append((st.custom_emoji_id, st.emoji or "⭐"))
    return out


# ═══════════════════════════ BOTTONI ═══════════════════════════════
def mk_btn(text, *, url=None, cb=None, emoji_name=None, style=None, plain=False):
    """Bottone inline. In modalità 'fancy' usa icona emoji premium e stile colorato;
    in modalità 'plain' (fallback) usa l'emoji normale nel testo."""
    kw = {}
    if url:
        kw["url"] = url
    if cb:
        kw["callback_data"] = cb
    e = db.emoji_get(emoji_name) if emoji_name else None
    if plain:
        if e:
            text = f"{e['fallback']} {text}"
    else:
        if e:
            kw["icon_custom_emoji_id"] = e["emoji_id"]
        if style in ("primary", "success", "danger"):
            kw["style"] = style
    return InlineKeyboardButton(text=text[:64], **kw)


def ikb(rows) -> InlineKeyboardMarkup:
    """rows: lista di righe, ogni riga lista di (testo, callback_data)."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=t, callback_data=c) for (t, c) in row] for row in rows
        ]
    )


# ═══════════════════ INVIO MESSAGGI (ROBUSTO) ══════════════════════
async def tg(fn, *a, **kw):
    """Chiama un metodo Telegram gestendo i flood-wait."""
    for _ in range(3):
        try:
            return await fn(*a, **kw)
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after + 1)
    return await fn(*a, **kw)


async def send_rich(chat_id, text, photo=None, builder=None, track=False):
    """Invia testo (+foto) (+bottoni). Fallback automatici:
    1) bottoni con emoji premium  2) bottoni semplici  3) senza foto."""
    if photo:
        attempts = [(False, True), (True, True), (True, False)]
    else:
        attempts = [(False, False), (True, False)]
    long_caption = len(strip_tags(text)) > 1000
    last_err = None
    for plain, use_photo in attempts:
        try:
            markup = builder(plain) if builder else None
            sent = []
            if use_photo and photo and not long_caption:
                sent.append(
                    await tg(bot.send_photo, chat_id, photo, caption=text, reply_markup=markup)
                )
                if track:
                    db.track(chat_id, chat_id, sent[-1].message_id)
            else:
                if use_photo and photo:
                    m1 = await tg(bot.send_photo, chat_id, photo)
                    sent.append(m1)
                    if track:
                        db.track(chat_id, chat_id, m1.message_id)
                m2 = await tg(bot.send_message, chat_id, text, reply_markup=markup)
                sent.append(m2)
                if track:
                    db.track(chat_id, chat_id, m2.message_id)
            return sent[-1]
        except TelegramForbiddenError:
            raise
        except TelegramBadRequest as e:
            last_err = e
            log.warning("send_rich fallback (plain=%s photo=%s): %s", plain, use_photo, e)
            continue
    if last_err:
        raise last_err


async def try_delete(chat_id: int, message_id: int) -> bool:
    try:
        await tg(bot.delete_message, chat_id, message_id)
        return True
    except TelegramAPIError:
        return False


async def delete_many(pairs):
    """Elimina più messaggi (batch da 100, con fallback singolo)."""
    by_chat = {}
    for c, m in pairs:
        by_chat.setdefault(c, []).append(m)
    for chat_id, ids in by_chat.items():
        for i in range(0, len(ids), 100):
            chunk = ids[i : i + 100]
            try:
                await tg(bot.delete_messages, chat_id, chunk)
            except TelegramAPIError:
                for mid in chunk:
                    await try_delete(chat_id, mid)


async def purge(user_id: int, keep=()):
    """Elimina tutti i messaggi tracciati della chat con l'utente."""
    rows = db.session_pop(user_id)
    await delete_many([(c, m) for (c, m) in rows if m not in keep])


async def notify_admin(text: str):
    for aid in ADMIN_IDS:
        with suppress(TelegramAPIError):
            await bot.send_message(aid, text)


# ═══════════════════ WORKER IN BACKGROUND ══════════════════════════
async def worker():
    """Esegue le eliminazioni programmate e revoca i link scaduti."""
    log.info("Worker avviato")
    while True:
        try:
            t = now()
            for row in db.del_due(t):
                try:
                    await bot.delete_message(row["chat_id"], row["message_id"])
                except TelegramRetryAfter as e:
                    await asyncio.sleep(e.retry_after + 1)
                    continue  # riprova al prossimo giro (non rimuovere)
                except TelegramAPIError:
                    pass
                db.del_remove(row["id"])
            for row in db.links_expired(t):
                try:
                    await bot.revoke_chat_invite_link(row["chat_id"], row["link"])
                except TelegramRetryAfter as e:
                    await asyncio.sleep(e.retry_after + 1)
                    continue
                except TelegramAPIError:
                    pass
                db.link_revoked(row["id"])
            db.links_jr_close(t)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Errore nel worker")
        await asyncio.sleep(2)


# ═══════════════════════ FILTRI / MIDDLEWARE ═══════════════════════
class IsAdmin(BaseFilter):
    async def __call__(self, event) -> bool:
        u = getattr(event, "from_user", None)
        if u is None or u.id not in ADMIN_IDS:
            return False
        chat = event.chat if isinstance(event, Message) else (event.message.chat if event.message else None)
        return chat is not None and chat.type == "private"


class TrackMW(BaseMiddleware):
    """Traccia i messaggi dell'utente in privato per poterli cancellare."""

    async def __call__(self, handler, event: Message, data):
        try:
            if event.chat.type == "private" and event.from_user and not event.from_user.is_bot:
                db.user_touch(event.from_user)
                db.track(event.from_user.id, event.chat.id, event.message_id)
        except Exception:
            log.exception("TrackMW")
        return await handler(event, data)


admin_router = Router(name="admin")
user_router = Router(name="user")
admin_router.message.filter(IsAdmin())
admin_router.callback_query.filter(IsAdmin())
user_router.message.middleware(TrackMW())

dp = Dispatcher(storage=MemoryStorage())


class AdminSt(StatesGroup):
    text = State()
    photo = State()
    num = State()
    button = State()
    group = State()
    group_rename = State()
    emoji = State()
    broadcast = State()


# ════════════════════════ LOGICA UTENTE ════════════════════════════
def welcome_builder():
    groups = db.groups(only_active=True)
    buttons = db.buttons()

    def build(plain: bool):
        rows = []
        for g in groups:
            rows.append(
                [mk_btn(g["label"], cb=f"go:{g['id']}", emoji_name="group", style="primary", plain=plain)]
            )
        for b in buttons:
            rows.append(
                [mk_btn(b["text"], url=b["url"], emoji_name=b["emoji_name"], style=b["style"], plain=plain)]
            )
        return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None

    return build


async def show_menu(user):
    """Ripulisce la chat e mostra il menu principale."""
    await purge(user.id)
    text = render(
        db.get("welcome_text") or DEFAULT_WELCOME,
        nome=esc(user.first_name),
        minuti=db.geti("link_minutes"),
        secondi=db.geti("delete_delay"),
    )
    if not db.groups(only_active=True):
        text += "\n\n⚠️ Nessun gruppo disponibile al momento."
    await send_rich(
        user.id, text, photo=db.get("welcome_photo") or None,
        builder=welcome_builder(), track=True,
    )


async def create_invite(group, user_id: int):
    """Crea il link d'invito a scadenza. Ritorna (url, expire_ts)."""
    minutes = max(1, db.geti("link_minutes"))
    expire = now() + minutes * 60
    kwargs = dict(chat_id=group["chat_id"], name=f"u{user_id}", expire_date=expire)
    if db.geti("join_request") == 1:
        kwargs["creates_join_request"] = True
    else:
        limit = db.geti("member_limit")
        if limit > 0:
            kwargs["member_limit"] = limit
    inv = await tg(bot.create_chat_invite_link, **kwargs)
    return inv.invite_link, expire, bool(kwargs.get("creates_join_request"))


async def deliver_link(user, gid: int, cb: CallbackQuery = None):
    uid = user.id

    async def answer(txt=None, alert=False):
        if cb:
            with suppress(TelegramAPIError):
                await cb.answer(txt, show_alert=alert)

    if db.user_banned(uid):
        return await answer("⛔ Accesso negato.", True)
    group = db.group_get(gid)
    if not group or not group["active"]:
        await answer("❌ Gruppo non più disponibile.", True)
        return await show_menu(user)

    u = db.user_get(uid)
    cd = db.geti("cooldown")
    if u and cd and now() - (u["last_link"] or 0) < cd:
        wait = cd - (now() - u["last_link"])
        return await answer(f"⏳ Attendi {wait}s prima di richiedere un altro link.", True)

    try:
        url, expire, jr_mode = await create_invite(group, uid)
    except TelegramAPIError as e:
        log.error("Creazione link fallita per gruppo %s: %s", group["chat_id"], e)
        await answer("⚠️ Servizio temporaneamente non disponibile.", True)
        await notify_admin(
            f"⚠️ <b>Impossibile creare il link</b> per «{esc(group['label'])}».\n"
            f"Verifica che il bot sia admin con permesso <i>Invita utenti</i>.\n<code>{esc(e)}</code>"
        )
        return

    db.link_log(uid, group["id"], group["chat_id"], url, expire, jr=1 if jr_mode else 0)
    db.group_click(group["id"])
    await answer()

    delay = max(3, db.geti("delete_delay"))
    modo = (
        "🛂 Premi il bottone e invia la richiesta: verrai aggiunto <b>manualmente</b> dall'admin.\n"
        if jr_mode else ""
    )
    text = render(
        db.get("link_text") or DEFAULT_LINK,
        nome=esc(user.first_name),
        gruppo=esc(group["label"]),
        minuti=db.geti("link_minutes"),
        secondi=delay,
        modo=modo,
    )

    def builder(plain: bool):
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [mk_btn("Richiedi accesso" if jr_mode else "Entra nel gruppo", url=url, emoji_name="join", style="success", plain=plain)]
            ]
        )

    try:
        msg = await send_rich(uid, text, photo=db.get("link_photo") or None, builder=builder, track=True)
    except TelegramForbiddenError:
        db.user_blocked(uid)
        return
    # il messaggio col link si autodistrugge; tutto il resto sparisce subito
    db.del_schedule(msg.chat.id, msg.message_id, now() + delay)
    await purge(uid, keep={msg.message_id})
    log.info("Link inviato a %s per gruppo %s", uid, group["chat_id"])


# ─────────────────────────── handler utente ────────────────────────
@user_router.message(CommandStart(), F.chat.type == "private")
async def h_start(message: Message, command: CommandObject):
    if db.user_banned(message.from_user.id):
        return
    arg = (command.args or "").strip()
    m = re.fullmatch(r"g(\d+)", arg)
    if m:
        await deliver_link(message.from_user, int(m.group(1)))
        return
    await show_menu(message.from_user)


@user_router.callback_query(F.data.startswith("go:"))
async def h_go(cb: CallbackQuery):
    try:
        gid = int(cb.data.split(":", 1)[1])
    except ValueError:
        return await cb.answer()
    await deliver_link(cb.from_user, gid, cb)


def jr_keyboard(rid: int) -> InlineKeyboardMarkup:
    return ikb([[("✅ Accetta", f"jr:ok:{rid}"), ("❌ Rifiuta", f"jr:no:{rid}")]])


@user_router.chat_join_request()
async def h_join_request(req: ChatJoinRequest):
    """NON accetta mai in automatico: salva la richiesta e avvisa l'admin con Accetta/Rifiuta."""
    link = req.invite_link.invite_link if req.invite_link else None
    row = db.link_get(link) if link else None
    if not row:
        log.info("Richiesta da un link non creato dal bot (chat %s): ignorata", req.chat.id)
        return
    u = req.from_user
    group = db.group_by_chat(req.chat.id)
    label = group["label"] if group else (req.chat.title or str(req.chat.id))
    rid, is_new = db.jr_add(
        req.chat.id, u.id, u.first_name or "", u.username or "",
        group["id"] if group else 0, link,
    )
    if not is_new:
        return
    full = " ".join(filter(None, [u.first_name, u.last_name])) or str(u.id)
    text = (
        "📥 <b>Nuova richiesta di ingresso</b>\n\n"
        f'👤 <a href="tg://user?id={u.id}">{esc(full)}</a>'
        + (f" (@{esc(u.username)})" if u.username else "")
        + f"\n🆔 <code>{u.id}</code>\n📂 Gruppo: <b>{esc(label)}</b>\n"
        f"⏳ In attesa in totale: <b>{db.jr_pending_count()}</b>\n\n"
        "Decidi tu: la richiesta resta in sospeso finché non premi un bottone."
    )
    for aid in ADMIN_IDS:
        with suppress(TelegramAPIError):
            await bot.send_message(aid, text, reply_markup=jr_keyboard(rid))
    log.info("Richiesta #%s da %s per chat %s (in attesa di approvazione manuale)", rid, u.id, req.chat.id)


@user_router.my_chat_member()
async def h_my_chat_member(ev: ChatMemberUpdated):
    if ev.chat.type == "private":
        return
    status = ev.new_chat_member.status
    by_admin = ev.from_user.id in ADMIN_IDS
    existing = db.group_by_chat(ev.chat.id)
    if status == "administrator" and by_admin:
        gid = db.group_add(ev.chat.id, ev.chat.title or str(ev.chat.id))
        await notify_admin(
            f"✅ <b>{esc(ev.chat.title)}</b> aggiunto automaticamente (ID gruppo #{gid}).\n"
            f"Gestiscilo da /admin → Gruppi."
        )
    elif status in ("left", "kicked") and existing:
        db.group_set(existing["id"], "active", 0)
        await notify_admin(f"⚠️ Il bot è stato rimosso da <b>{esc(ev.chat.title)}</b>: gruppo disattivato.")
    elif status in ("member", "administrator") and not by_admin and not existing:
        log.warning("Aggiunto da non-admin in %s: esco.", ev.chat.id)
        with suppress(TelegramAPIError):
            await bot.leave_chat(ev.chat.id)


@user_router.message(F.chat.type == "private")
async def h_any(message: Message):
    if db.user_banned(message.from_user.id):
        return
    await show_menu(message.from_user)


# ════════════════════════ PANNELLO ADMIN ═══════════════════════════
def home_text() -> str:
    s = db.stats()
    return (
        "🛠 <b>PANNELLO ADMIN</b>\n\n"
        f"👥 Utenti: <b>{s['users']}</b>  (nuovi 24h: {s['new24']})\n"
        f"🔗 Link generati: <b>{s['links']}</b>  (24h: {s['links24']})\n"
        f"📂 Gruppi attivi: <b>{s['groups']}</b>\n"
        f"📥 Richieste in attesa: <b>{db.jr_pending_count()}</b>\n"
        f"😀 Emoji premium salvate: <b>{s['emojis']}</b>\n\n"
        "Scegli una sezione:"
    )


def kb_home():
    n = db.jr_pending_count()
    return ikb([
        [(f"📥 Richieste ({n})" if n else "📥 Richieste", "adm:reqs")],
        [("👥 Gruppi", "adm:groups"), ("🔘 Bottoni", "adm:btns")],
        [("📝 Contenuti", "adm:content"), ("😀 Emoji premium", "adm:emo")],
        [("⚙️ Impostazioni", "adm:set"), ("📊 Statistiche", "adm:stats")],
        [("📢 Broadcast", "adm:bc"), ("👁 Anteprima", "adm:prev")],
        [("ℹ️ Guida", "adm:help"), ("💣 Autodistruzione", "adm:boom")],
    ])


async def safe_edit(cb: CallbackQuery, text: str, markup=None):
    try:
        await cb.message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest as e:
        if "not modified" in str(e).lower():
            return
        with suppress(TelegramAPIError):
            await cb.message.answer(text, reply_markup=markup)


@admin_router.message(Command("admin", "panel"))
async def a_cmd_admin(m: Message, state: FSMContext):
    await state.clear()
    await m.answer(home_text(), reply_markup=kb_home())


@admin_router.message(Command("cancel"))
async def a_cmd_cancel(m: Message, state: FSMContext):
    await state.clear()
    await m.answer("❎ Operazione annullata.", reply_markup=ReplyKeyboardRemove())
    await m.answer(home_text(), reply_markup=kb_home())


@admin_router.message(Command("ban"))
async def a_cmd_ban(m: Message, command: CommandObject):
    if not command.args or not command.args.strip().lstrip("-").isdigit():
        return await m.answer("Uso: <code>/ban ID_UTENTE</code>")
    db.user_ban(int(command.args.strip()), 1)
    await m.answer("⛔ Utente bannato.")


@admin_router.message(Command("unban"))
async def a_cmd_unban(m: Message, command: CommandObject):
    if not command.args or not command.args.strip().lstrip("-").isdigit():
        return await m.answer("Uso: <code>/unban ID_UTENTE</code>")
    db.user_ban(int(command.args.strip()), 0)
    await m.answer("✅ Utente sbannato.")


@admin_router.callback_query(F.data == "adm:home")
async def a_home(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.answer()
    await safe_edit(cb, home_text(), kb_home())


# ───────────────────────────── GRUPPI ──────────────────────────────
def groups_view():
    gs = db.groups()
    rows = []
    for g in gs:
        mark = "✅" if g["active"] else "⛔"
        rows.append([(f"{mark} {g['label']}"[:60], f"adm:g:{g['id']}")])
    rows.append([("➕ Aggiungi gruppo/canale", "adm:gadd")])
    rows.append([("⬅️ Indietro", "adm:home")])
    text = "👥 <b>GRUPPI</b>\n\n"
    text += (
        "Tocca un gruppo per gestirlo."
        if gs
        else "Nessun gruppo ancora. Aggiungine uno.\n"
        "Il bot deve essere <b>admin</b> con permesso <i>Invita utenti</i>."
    )
    return text, ikb(rows)


@admin_router.callback_query(F.data == "adm:groups")
async def a_groups(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.answer()
    t, k = groups_view()
    await safe_edit(cb, t, k)


@admin_router.callback_query(F.data.startswith("adm:g:"))
async def a_group_detail(cb: CallbackQuery):
    await cb.answer()
    gid = int(cb.data.split(":")[2])
    g = db.group_get(gid)
    if not g:
        return await safe_edit(cb, "Gruppo non trovato.", ikb([[("⬅️ Indietro", "adm:groups")]]))
    deep = f"https://t.me/{BOT_USERNAME}?start=g{g['id']}"
    text = (
        f"📂 <b>{esc(g['label'])}</b>\n\n"
        f"Titolo reale: {esc(g['title'])}\n"
        f"Chat ID: <code>{g['chat_id']}</code>\n"
        f"Stato: {'✅ attivo' if g['active'] else '⛔ disattivato'}\n"
        f"Richieste link: <b>{g['clicks']}</b>\n\n"
        f"🔗 Deep-link diretto (genera subito il link):\n<code>{deep}</code>"
    )
    await safe_edit(cb, text, ikb([
        [("✏️ Rinomina", f"adm:gren:{gid}"),
         ("⛔ Disattiva" if g["active"] else "✅ Attiva", f"adm:gtog:{gid}")],
        [("🗑 Elimina", f"adm:gdel:{gid}")],
        [("⬅️ Indietro", "adm:groups")],
    ]))


@admin_router.callback_query(F.data.startswith("adm:gtog:"))
async def a_group_toggle(cb: CallbackQuery):
    gid = int(cb.data.split(":")[2])
    g = db.group_get(gid)
    if g:
        db.group_set(gid, "active", 0 if g["active"] else 1)
    await cb.answer("Fatto")
    cb.data = f"adm:g:{gid}"
    await a_group_detail(cb)


@admin_router.callback_query(F.data.startswith("adm:gdel:"))
async def a_group_del(cb: CallbackQuery):
    await cb.answer()
    gid = int(cb.data.split(":")[2])
    await safe_edit(cb, "⚠️ Eliminare definitivamente questo gruppo dal bot?", ikb([
        [("✅ Sì, elimina", f"adm:gdelok:{gid}"), ("❌ No", f"adm:g:{gid}")],
    ]))


@admin_router.callback_query(F.data.startswith("adm:gdelok:"))
async def a_group_del_ok(cb: CallbackQuery):
    db.group_del(int(cb.data.split(":")[2]))
    await cb.answer("Eliminato")
    t, k = groups_view()
    await safe_edit(cb, t, k)


@admin_router.callback_query(F.data.startswith("adm:gren:"))
async def a_group_rename(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    gid = int(cb.data.split(":")[2])
    await state.set_state(AdminSt.group_rename)
    await state.update_data(gid=gid)
    await cb.message.answer("✏️ Invia il nuovo nome del bottone (puoi scrivere anche emoji normali).\n/cancel per annullare")


@admin_router.message(StateFilter(AdminSt.group_rename), F.text)
async def a_group_rename_done(m: Message, state: FSMContext):
    data = await state.get_data()
    db.group_set(data["gid"], "label", m.text.strip()[:60])
    await state.clear()
    t, k = groups_view()
    await m.answer("✅ Rinominato.\n\n" + t, reply_markup=k)


@admin_router.callback_query(F.data == "adm:gadd")
async def a_group_add(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.set_state(AdminSt.group)
    kb = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📂 Scegli gruppo",
                            request_chat=KeyboardButtonRequestChat(request_id=1, chat_is_channel=False, bot_is_member=True)),
             KeyboardButton(text="📢 Scegli canale",
                            request_chat=KeyboardButtonRequestChat(request_id=2, chat_is_channel=True, bot_is_member=True))],
        ],
        resize_keyboard=True, one_time_keyboard=True,
    )
    await cb.message.answer(
        "➕ <b>Aggiungi gruppo/canale</b>\n\n"
        "Premi uno dei pulsanti qui sotto, oppure invia direttamente l'ID della chat "
        "(es. <code>-1001234567890</code>).\n"
        "Il bot deve essere già admin con permesso <i>Invita utenti</i>.\n/cancel per annullare",
        reply_markup=kb,
    )


async def register_group(m: Message, state: FSMContext, chat_id: int):
    try:
        chat = await bot.get_chat(chat_id)
        member = await bot.get_chat_member(chat_id, bot.id)
    except TelegramAPIError as e:
        return await m.answer(f"❌ Non riesco ad accedere alla chat:\n<code>{esc(e)}</code>")
    if member.status != "administrator":
        return await m.answer("❌ Il bot non è admin in quella chat. Promuovilo e riprova.")
    if not getattr(member, "can_invite_users", False):
        return await m.answer("❌ Al bot manca il permesso <b>Invita utenti</b> (link d'invito).")
    gid = db.group_add(chat.id, chat.title or str(chat.id))
    await state.clear()
    await m.answer(f"✅ <b>{esc(chat.title)}</b> aggiunto (#{gid}).", reply_markup=ReplyKeyboardRemove())
    t, k = groups_view()
    await m.answer(t, reply_markup=k)


@admin_router.message(StateFilter(AdminSt.group), F.chat_shared)
async def a_group_shared(m: Message, state: FSMContext):
    await register_group(m, state, m.chat_shared.chat_id)


@admin_router.message(StateFilter(AdminSt.group), F.text)
async def a_group_text(m: Message, state: FSMContext):
    t = m.text.strip()
    if not re.fullmatch(r"-?\d{5,20}", t):
        return await m.answer("❌ ID non valido. Esempio: <code>-1001234567890</code>")
    await register_group(m, state, int(t))


# ───────────────────────────── BOTTONI ─────────────────────────────
def buttons_view():
    bs = db.buttons()
    rows = []
    text = "🔘 <b>BOTTONI PERSONALIZZATI</b>\n\nQuesti bottoni appaiono sotto al messaggio di benvenuto.\n\n"
    if not bs:
        text += "<i>Nessun bottone.</i>"
    for b in bs:
        extra = []
        if b["emoji_name"]:
            extra.append(f"emoji: {esc(b['emoji_name'])}")
        if b["style"]:
            extra.append(f"stile: {esc(b['style'])}")
        text += f"• <b>{esc(b['text'])}</b> → {esc(b['url'])}" + (f"  ({', '.join(extra)})" if extra else "") + "\n"
        rows.append([(f"🗑 {b['text']}"[:60], f"adm:bdel:{b['id']}")])
    rows.append([("➕ Aggiungi bottone", "adm:badd")])
    rows.append([("⬅️ Indietro", "adm:home")])
    return text, ikb(rows)


@admin_router.callback_query(F.data == "adm:btns")
async def a_btns(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.answer()
    t, k = buttons_view()
    await safe_edit(cb, t, k)


@admin_router.callback_query(F.data.startswith("adm:bdel:"))
async def a_btn_del(cb: CallbackQuery):
    db.button_del(int(cb.data.split(":")[2]))
    await cb.answer("Eliminato")
    t, k = buttons_view()
    await safe_edit(cb, t, k)


@admin_router.callback_query(F.data == "adm:badd")
async def a_btn_add(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.set_state(AdminSt.button)
    await cb.message.answer(
        "➕ <b>Nuovo bottone</b>\n\nInvia nel formato:\n"
        "<code>Testo | https://link | nome_emoji | stile</code>\n\n"
        "• <b>nome_emoji</b> (facoltativo): nome di un'emoji premium salvata\n"
        "• <b>stile</b> (facoltativo): <code>primary</code> (blu), <code>success</code> (verde), <code>danger</code> (rosso)\n\n"
        "Esempi:\n<code>Canale ufficiale | https://t.me/miocanale</code>\n"
        "<code>Supporto | https://t.me/utente | fire | success</code>\n\n/cancel per annullare"
    )


@admin_router.message(StateFilter(AdminSt.button), F.text)
async def a_btn_add_done(m: Message, state: FSMContext):
    parts = [p.strip() for p in m.text.split("|")]
    if len(parts) < 2 or not parts[0] or not re.match(r"^(https?://|tg://|t\.me/)", parts[1]):
        return await m.answer("❌ Formato non valido. Servono almeno <code>Testo | https://link</code>")
    url = parts[1]
    if url.startswith("t.me/"):
        url = "https://" + url
    emoji_name = parts[2] if len(parts) > 2 and parts[2] else None
    style = parts[3].lower() if len(parts) > 3 and parts[3] else None
    warn = ""
    if emoji_name and not db.emoji_get(emoji_name):
        warn += f"\n⚠️ Emoji «{esc(emoji_name)}» non trovata: verrà ignorata finché non la salvi."
    if style and style not in ("primary", "success", "danger"):
        warn += "\n⚠️ Stile non valido: ignorato."
        style = None
    db.button_add(parts[0][:60], url, emoji_name, style)
    await state.clear()
    t, k = buttons_view()
    await m.answer("✅ Bottone aggiunto." + warn + "\n\n" + t, reply_markup=k)


# ─────────────────────── CONTENUTI (testi e foto) ──────────────────
def content_view():
    wp = "✅ impostata" if db.get("welcome_photo") else "— nessuna"
    lp = "✅ impostata" if db.get("link_photo") else "— nessuna"
    text = (
        "📝 <b>CONTENUTI</b>\n\n"
        f"🖼 Foto benvenuto: {wp}\n🖼 Foto messaggio link: {lp}\n\n"
        "Nei testi puoi usare: <code>{nome}</code> <code>{gruppo}</code> <code>{minuti}</code> "
        "<code>{secondi}</code> <code>{modo}</code> e le emoji premium (incollale direttamente o usa <code>{e:nome}</code>).\n"
        "La formattazione (grassetto, corsivo, ecc.) viene mantenuta."
    )
    rows = [
        [("✏️ Testo benvenuto", "adm:txt:welcome_text")],
        [("✏️ Testo messaggio link", "adm:txt:link_text")],
        [("🖼 Foto benvenuto", "adm:ph:welcome_photo"), ("🗑", "adm:phdel:welcome_photo")],
        [("🖼 Foto messaggio link", "adm:ph:link_photo"), ("🗑", "adm:phdel:link_photo")],
        [("♻️ Ripristina testi default", "adm:txtreset")],
        [("⬅️ Indietro", "adm:home")],
    ]
    return text, ikb(rows)


@admin_router.callback_query(F.data == "adm:content")
async def a_content(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.answer()
    t, k = content_view()
    await safe_edit(cb, t, k)


@admin_router.callback_query(F.data == "adm:txtreset")
async def a_txt_reset(cb: CallbackQuery):
    db.set("welcome_text", DEFAULT_WELCOME)
    db.set("link_text", DEFAULT_LINK)
    await cb.answer("Testi ripristinati")


@admin_router.callback_query(F.data.startswith("adm:txt:"))
async def a_txt(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    key = cb.data.split(":")[2]
    if key not in TEXT_META:
        return
    await state.set_state(AdminSt.text)
    await state.update_data(key=key)
    await cb.message.answer(
        f"✏️ <b>{TEXT_META[key]}</b>\n\nInvia il nuovo testo. Puoi usare grassetto, corsivo, "
        "link e <b>emoji premium</b> direttamente: il loro ID viene salvato in automatico.\n"
        "Variabili: <code>{nome} {gruppo} {minuti} {secondi} {modo}</code>\n\n/cancel per annullare"
    )


@admin_router.message(StateFilter(AdminSt.text), F.text)
async def a_txt_done(m: Message, state: FSMContext):
    data = await state.get_data()
    key = data["key"]
    # salva anche gli emoji premium trovati (per riuso con {e:nome})
    for eid, fb in extract_custom_emojis(m):
        if not any(r["emoji_id"] == eid for r in db.emoji_list()):
            db.emoji_save(f"e{db.emoji_next_index()}", eid, fb)
    db.set(key, m.html_text)
    await state.clear()
    t, k = content_view()
    await m.answer("✅ Testo salvato.\n\n" + t, reply_markup=k)


@admin_router.callback_query(F.data.startswith("adm:ph:"))
async def a_photo(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    key = cb.data.split(":")[2]
    if key not in PHOTO_META:
        return
    await state.set_state(AdminSt.photo)
    await state.update_data(key=key)
    await cb.message.answer(f"🖼 Invia la foto per: <b>{PHOTO_META[key]}</b>\n\n/cancel per annullare")


@admin_router.message(StateFilter(AdminSt.photo), F.photo)
async def a_photo_done(m: Message, state: FSMContext):
    data = await state.get_data()
    db.set(data["key"], m.photo[-1].file_id)
    await state.clear()
    t, k = content_view()
    await m.answer("✅ Foto salvata.\n\n" + t, reply_markup=k)


@admin_router.message(StateFilter(AdminSt.photo))
async def a_photo_wrong(m: Message):
    await m.answer("❌ Devi inviare una <b>foto</b> (non file). /cancel per annullare")


@admin_router.callback_query(F.data.startswith("adm:phdel:"))
async def a_photo_del(cb: CallbackQuery):
    key = cb.data.split(":")[2]
    if key in PHOTO_META:
        db.set(key, "")
    await cb.answer("Foto rimossa")
    t, k = content_view()
    await safe_edit(cb, t, k)


# ─────────────────────────── EMOJI PREMIUM ─────────────────────────
def emoji_view():
    es = db.emoji_list()
    text = (
        "😀 <b>EMOJI PREMIUM</b>\n\n"
        "Gli ID vengono salvati nel database. Usali nei testi con <code>{e:nome}</code>, "
        "nei bottoni (campo <i>nome_emoji</i>) o incollandole direttamente.\n\n"
        "Nomi speciali usati dal bot come icona:\n"
        "• <code>group</code> → bottoni dei gruppi\n• <code>join</code> → bottone «Entra nel gruppo»\n\n"
    )
    rows = []
    if not es:
        text += "<i>Nessuna emoji salvata.</i>"
    for e in es[:40]:
        text += (
            f'<tg-emoji emoji-id="{e["emoji_id"]}">{esc(e["fallback"])}</tg-emoji> '
            f'<code>{esc(e["name"])}</code> — <code>{e["emoji_id"]}</code>\n'
        )
        rows.append([(f"🗑 {e['name']}", f"adm:edel:{e['name']}")])
    rows.append([("➕ Salva emoji premium", "adm:eadd")])
    rows.append([("⬅️ Indietro", "adm:home")])
    return text, ikb(rows)


@admin_router.callback_query(F.data == "adm:emo")
async def a_emo(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.answer()
    t, k = emoji_view()
    await safe_edit(cb, t, k)


@admin_router.callback_query(F.data.startswith("adm:edel:"))
async def a_emo_del(cb: CallbackQuery):
    db.emoji_del(cb.data.split(":", 2)[2])
    await cb.answer("Eliminata")
    t, k = emoji_view()
    await safe_edit(cb, t, k)


@admin_router.callback_query(F.data == "adm:eadd")
async def a_emo_add(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.set_state(AdminSt.emoji)
    await cb.message.answer(
        "😀 <b>Salva emoji premium</b>\n\n"
        "Invia un messaggio con una o più emoji premium.\n"
        "Per scegliere il nome, scrivi una parola prima (solo lettere/numeri):\n"
        "<code>fire 🔥</code> → salvata come <code>fire</code>\n"
        "Con più emoji: <code>stars ⭐ ✨</code> → <code>stars1</code>, <code>stars2</code>\n"
        "Senza parola i nomi sono automatici (<code>e1</code>, <code>e2</code>…).\n"
        "Puoi anche inoltrare uno sticker emoji.\n\n/cancel per terminare"
    )


@admin_router.message(StateFilter(AdminSt.emoji))
async def a_emo_done(m: Message, state: FSMContext):
    found = extract_custom_emojis(m)
    if not found:
        return await m.answer("❌ Nessuna emoji premium trovata nel messaggio. Riprova o /cancel.")
    text = m.text or m.caption or ""
    mm = re.search(r"[A-Za-z][A-Za-z0-9_\-]{0,30}", text)
    base = mm.group(0).lower() if mm else None
    saved = []
    for i, (eid, fb) in enumerate(found):
        if base:
            name = base if len(found) == 1 else f"{base}{i + 1}"
        else:
            name = f"e{db.emoji_next_index()}"
        db.emoji_save(name, eid, fb)
        saved.append((name, eid, fb))
    out = "✅ <b>Salvate:</b>\n" + "\n".join(
        f'<tg-emoji emoji-id="{eid}">{esc(fb)}</tg-emoji> <code>{esc(n)}</code> — <code>{eid}</code>'
        for n, eid, fb in saved
    )
    await m.answer(out + "\n\nPuoi inviarne altre oppure /cancel per terminare.")


# ───────────────────────── IMPOSTAZIONI ────────────────────────────
def settings_view():
    jr = db.geti("join_request") == 1
    text = (
        "⚙️ <b>IMPOSTAZIONI</b>\n\n"
        f"⏱ Validità link: <b>{db.geti('link_minutes')}</b> min\n"
        f"🗑 Auto-eliminazione messaggio link: <b>{db.geti('delete_delay')}</b> s\n"
        f"👤 Max utenti per link: <b>{db.geti('member_limit') or 'illimitato'}</b>\n"
        f"🧊 Cooldown: <b>{db.geti('cooldown')}</b> s\n"
        f"🛂 Richiesta di ingresso: <b>{'ON · approvi tu a mano' if jr else 'OFF · ingresso diretto'}</b>\n"
        f"📨 Avviso all'utente sull'esito: <b>{'ON' if db.geti('notify_user') == 1 else 'OFF'}</b>\n\n"
        "Il resto della chat con l'utente viene cancellato <b>subito</b> dopo l'invio del link."
    )
    rows = [[(lbl, f"adm:num:{k}")] for k, (lbl, _, _) in NUM_META.items()]
    rows.append([(f"🛂 Approvazione manuale: {'ON' if jr else 'OFF'}", "adm:jr")])
    rows.append([(f"📨 Avvisa l'utente dell'esito: {'ON' if db.geti('notify_user') == 1 else 'OFF'}", "adm:notif")])
    rows.append([("⬅️ Indietro", "adm:home")])
    return text, ikb(rows)


@admin_router.callback_query(F.data == "adm:set")
async def a_set(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.answer()
    t, k = settings_view()
    await safe_edit(cb, t, k)


@admin_router.callback_query(F.data == "adm:jr")
async def a_jr(cb: CallbackQuery):
    db.set("join_request", "0" if db.geti("join_request") == 1 else "1")
    await cb.answer("Aggiornato")
    t, k = settings_view()
    await safe_edit(cb, t, k)


@admin_router.callback_query(F.data.startswith("adm:num:"))
async def a_num(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    key = cb.data.split(":")[2]
    if key not in NUM_META:
        return
    lbl, lo, hi = NUM_META[key]
    await state.set_state(AdminSt.num)
    await state.update_data(key=key)
    await cb.message.answer(
        f"{lbl}\nValore attuale: <b>{db.geti(key)}</b>\n\n"
        f"Invia un numero tra <b>{lo}</b> e <b>{hi}</b>.\n/cancel per annullare"
    )


@admin_router.message(StateFilter(AdminSt.num), F.text)
async def a_num_done(m: Message, state: FSMContext):
    data = await state.get_data()
    key = data["key"]
    _, lo, hi = NUM_META[key]
    if not m.text.strip().isdigit() or not (lo <= int(m.text.strip()) <= hi):
        return await m.answer(f"❌ Valore non valido (da {lo} a {hi}).")
    db.set(key, m.text.strip())
    await state.clear()
    t, k = settings_view()
    await m.answer("✅ Salvato.\n\n" + t, reply_markup=k)


# ──────────────────────── STATISTICHE / ANTEPRIMA ──────────────────
# ─────────────────── RICHIESTE DI INGRESSO (manuali) ────────────────
async def process_request(rid: int, approve: bool) -> str:
    """Accetta/rifiuta UNA richiesta. Ritorna il testo dell'esito (HTML)."""
    r = db.jr_get(rid)
    if not r:
        return "❌ Richiesta non trovata."
    if r["status"] != "pending":
        return f"ℹ️ Già gestita ({esc(r['status'])})."
    try:
        if approve:
            await tg(bot.approve_chat_join_request, r["chat_id"], r["user_id"])
        else:
            await tg(bot.decline_chat_join_request, r["chat_id"], r["user_id"])
    except TelegramBadRequest as e:
        m = str(e).lower()
        if "already" in m and "participant" in m:
            db.jr_set(rid, "approved")
            return "ℹ️ L'utente era già nel gruppo."
        if "hide_requester_missing" in m or "not found" in m:
            db.jr_set(rid, "expired")
            return "⚠️ Richiesta non più valida (ritirata o già gestita altrove)."
        return f"❌ Errore Telegram: <code>{esc(e)}</code>"
    except TelegramAPIError as e:
        return f"❌ Errore: <code>{esc(e)}</code>"
    db.jr_set(rid, "approved" if approve else "declined")
    if db.geti("notify_user") == 1:
        g = db.group_get(r["group_id"]) if r["group_id"] else None
        label = esc(g["label"]) if g else "il gruppo"
        txt = (
            f"✅ La tua richiesta per <b>{label}</b> è stata <b>accettata</b>! Ora sei dentro."
            if approve else f"❌ La tua richiesta per <b>{label}</b> è stata <b>rifiutata</b>."
        )
        with suppress(TelegramAPIError):
            await bot.send_message(r["user_id"], txt)
    who = esc(r["first_name"] or r["user_id"])
    return f"{'✅ Accettato' if approve else '❌ Rifiutato'}: <b>{who}</b>"


def reqs_view():
    total = db.jr_pending_count()
    items = db.jr_pending(8)
    text = (
        "📥 <b>RICHIESTE DI INGRESSO</b>\n\n"
        f"In attesa: <b>{total}</b>\n"
        "Nessuna accettazione automatica: decidi tu.\n\n"
    )
    rows = []
    if not items:
        text += "<i>Nessuna richiesta in sospeso.</i>"
    for r in items:
        g = db.group_get(r["group_id"]) if r["group_id"] else None
        name = r["first_name"] or str(r["user_id"])
        uname = f" (@{esc(r['username'])})" if r["username"] else ""
        text += (
            f'• <a href="tg://user?id={r["user_id"]}">{esc(name)}</a>{uname} → '
            f'{esc(g["label"] if g else "?")} · {fmt_ts(r["created"])}\n'
        )
        rows.append([(f"✅ {name}"[:28], f"jrl:ok:{r['id']}"), (f"❌ {name}"[:28], f"jrl:no:{r['id']}")])
    if total > len(items):
        text += f"\n… e altre <b>{total - len(items)}</b> richieste."
    if total:
        rows.append([("✅ Accetta tutte", "adm:reqbulk:ok"), ("❌ Rifiuta tutte", "adm:reqbulk:no")])
    rows.append([("🔄 Aggiorna", "adm:reqs")])
    rows.append([("⬅️ Indietro", "adm:home")])
    return text, ikb(rows)


@admin_router.callback_query(F.data == "adm:reqs")
async def a_reqs(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.answer()
    t, k = reqs_view()
    await safe_edit(cb, t, k)


@admin_router.callback_query(F.data.startswith("jr:"))
async def a_jr_action(cb: CallbackQuery):
    """Bottoni Accetta/Rifiuta sul messaggio di avviso."""
    try:
        _, act, rid = cb.data.split(":")
        rid = int(rid)
    except ValueError:
        return await cb.answer()
    res = await process_request(rid, act == "ok")
    await cb.answer(strip_tags(res)[:190])
    base = getattr(cb.message, "html_text", "") or ""
    await safe_edit(cb, f"{base}\n\n{res}", None)


@admin_router.callback_query(F.data.startswith("jrl:"))
async def a_jrl_action(cb: CallbackQuery):
    """Bottoni Accetta/Rifiuta nella lista 📥 Richieste."""
    try:
        _, act, rid = cb.data.split(":")
        rid = int(rid)
    except ValueError:
        return await cb.answer()
    res = await process_request(rid, act == "ok")
    await cb.answer(strip_tags(res)[:190])
    t, k = reqs_view()
    await safe_edit(cb, t, k)


@admin_router.callback_query(F.data.startswith("adm:reqbulk:"))
async def a_req_bulk(cb: CallbackQuery):
    act = cb.data.split(":")[2]
    n = db.jr_pending_count()
    await cb.answer()
    verb = "ACCETTARE" if act == "ok" else "RIFIUTARE"
    await safe_edit(
        cb, f"⚠️ Vuoi davvero <b>{verb} tutte</b> le <b>{n}</b> richieste in attesa?",
        ikb([[("✅ Sì, conferma", f"adm:reqbulkgo:{act}"), ("❌ Annulla", "adm:reqs")]]),
    )


@admin_router.callback_query(F.data.startswith("adm:reqbulkgo:"))
async def a_req_bulk_go(cb: CallbackQuery):
    act = cb.data.split(":")[2]
    await cb.answer("Elaboro…")
    ok = bad = 0
    for r in db.jr_pending_all():
        res = await process_request(r["id"], act == "ok")
        if res.startswith(("✅", "❌ Rifiutato", "ℹ️")):
            ok += 1
        else:
            bad += 1
        await asyncio.sleep(0.35)
    t, k = reqs_view()
    await safe_edit(cb, f"Completato: ✅ {ok} · ⚠️ {bad}\n\n" + t, k)


@admin_router.callback_query(F.data == "adm:notif")
async def a_notif(cb: CallbackQuery):
    db.set("notify_user", "0" if db.geti("notify_user") == 1 else "1")
    await cb.answer("Aggiornato")
    t, k = settings_view()
    await safe_edit(cb, t, k)


@admin_router.callback_query(F.data == "adm:stats")
async def a_stats(cb: CallbackQuery):
    await cb.answer()
    s = db.stats()
    text = (
        "📊 <b>STATISTICHE</b>\n\n"
        f"👥 Utenti totali: <b>{s['users']}</b>\n"
        f"🆕 Nuovi (24h): <b>{s['new24']}</b>\n"
        f"⛔ Bannati: <b>{s['banned']}</b>\n"
        f"🚫 Hanno bloccato il bot: <b>{s['blocked']}</b>\n\n"
        f"🔗 Link generati: <b>{s['links']}</b>\n"
        f"🔗 Link (24h): <b>{s['links24']}</b>\n"
        f"🟢 Link attivi ora: <b>{s['active_links']}</b>\n"
        f"📥 Richieste in attesa: <b>{db.jr_pending_count()}</b>\n\n"
        f"📂 Gruppi attivi: <b>{s['groups']}</b>\n"
        f"😀 Emoji premium: <b>{s['emojis']}</b>\n\n"
    )
    for g in db.groups():
        text += f"• {esc(g['label'])}: <b>{g['clicks']}</b> richieste\n"
    await safe_edit(cb, text, ikb([[("🔄 Aggiorna", "adm:stats")], [("⬅️ Indietro", "adm:home")]]))


@admin_router.callback_query(F.data == "adm:prev")
async def a_prev(cb: CallbackQuery):
    await cb.answer("Anteprima inviata")
    text = render(
        db.get("welcome_text") or DEFAULT_WELCOME,
        nome=esc(cb.from_user.first_name),
        minuti=db.geti("link_minutes"),
        secondi=db.geti("delete_delay"),
    )
    try:
        await send_rich(cb.from_user.id, "👁 <b>ANTEPRIMA BENVENUTO</b>\n\n" + text,
                        photo=db.get("welcome_photo") or None, builder=welcome_builder())
    except TelegramAPIError as e:
        await cb.message.answer(f"❌ Errore anteprima:\n<code>{esc(e)}</code>")


@admin_router.callback_query(F.data == "adm:help")
async def a_help(cb: CallbackQuery):
    await cb.answer()
    text = (
        "ℹ️ <b>GUIDA RAPIDA</b>\n\n"
        "<b>1.</b> Aggiungi il bot come <b>admin</b> nei gruppi/canali (permesso <i>Invita utenti</i>).\n"
        "<b>2.</b> Registra il gruppo da <i>Gruppi → Aggiungi</i> (o promuovi il bot mentre sei tu a farlo: "
        "viene aggiunto da solo).\n"
        "<b>3.</b> Imposta foto, testi e bottoni da <i>Contenuti</i> e <i>Bottoni</i>.\n"
        "<b>4.</b> Salva le emoji premium da <i>Emoji premium</i>.\n\n"
        "<b>Funzionamento:</b> l'utente fa /start, sceglie il gruppo, riceve un link personale "
        "valido X minuti. Subito dopo l'invio, la chat viene ripulita e il messaggio col link "
        "si autodistrugge dopo Y secondi. I link scaduti vengono revocati.\n\n"
        "<b>Emoji premium:</b> le emoji custom nei messaggi del bot richiedono che l'account "
        "proprietario del bot abbia Telegram Premium o username acquistati su Fragment "
        "(regola di Telegram). Se non disponibile, il bot mostra l'emoji normale di ripiego.\n\n"
        "<b>Richieste di ingresso:</b> con «Approvazione manuale» ON il bot <u>non accetta mai da solo</u>: "
        "ti manda un avviso con ✅ Accetta / ❌ Rifiuta e le trovi tutte in 📥 Richieste. "
        "Puoi anche gestirle dalla lista «Richieste di iscrizione» del gruppo in Telegram.\n\n"
        "<b>Comandi:</b> /admin · /cancel · /ban ID · /unban ID\n"
        "<b>Deep-link per gruppo:</b> dentro la scheda del gruppo."
    )
    await safe_edit(cb, text, ikb([[("⬅️ Indietro", "adm:home")]]))


# ───────────────────────────── BROADCAST ───────────────────────────
@admin_router.callback_query(F.data == "adm:bc")
async def a_bc(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.set_state(AdminSt.broadcast)
    await cb.message.answer(
        "📢 <b>Broadcast</b>\n\nInvia il messaggio da spedire a tutti gli utenti "
        "(testo, foto, emoji premium... verrà copiato identico).\n/cancel per annullare"
    )


@admin_router.message(StateFilter(AdminSt.broadcast))
async def a_bc_msg(m: Message, state: FSMContext):
    await state.update_data(mid=m.message_id, chat=m.chat.id)
    await m.answer(
        f"Inviare questo messaggio a <b>{len(db.user_ids())}</b> utenti?",
        reply_markup=ikb([[("✅ Invia", "adm:bcgo"), ("❌ Annulla", "adm:home")]]),
    )


@admin_router.callback_query(F.data == "adm:bcgo")
async def a_bc_go(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    await state.clear()
    if "mid" not in data:
        return await cb.answer("Nessun messaggio", show_alert=True)
    await cb.answer("Invio in corso…")
    status = await cb.message.answer("📤 Invio in corso…")
    ok = fail = 0
    for uid in db.user_ids():
        try:
            await tg(bot.copy_message, uid, data["chat"], data["mid"])
            ok += 1
        except TelegramForbiddenError:
            db.user_blocked(uid)
            fail += 1
        except TelegramAPIError:
            fail += 1
        await asyncio.sleep(0.05)
    await status.edit_text(f"✅ Broadcast completato.\nInviati: <b>{ok}</b>\nFalliti: <b>{fail}</b>")


# ──────────────────────── AUTODISTRUZIONE ──────────────────────────
@admin_router.callback_query(F.data == "adm:boom")
async def a_boom(cb: CallbackQuery):
    await cb.answer()
    await safe_edit(
        cb,
        "💣 <b>AUTODISTRUZIONE</b>\n\nVerranno:\n"
        "• revocati <b>tutti</b> i link d'invito attivi\n"
        "• eliminati i messaggi tracciati nelle chat utente\n"
        "• cancellati gruppi, utenti, bottoni, emoji, impostazioni e database\n"
        "• fermato il bot\n\n⚠️ <b>Operazione irreversibile.</b>",
        ikb([[("💥 CONFERMA", "adm:boomok"), ("❌ Annulla", "adm:home")]]),
    )


@admin_router.callback_query(F.data == "adm:boomok")
async def a_boom_ok(cb: CallbackQuery):
    global STOP, WIPED
    await cb.answer("Autodistruzione…")
    st = await cb.message.answer("💣 Revoca dei link in corso…")
    for row in db.links_active():
        with suppress(TelegramAPIError):
            await bot.revoke_chat_invite_link(row["chat_id"], row["link"])
        await asyncio.sleep(0.03)
    await st.edit_text("💣 Eliminazione messaggi in corso…")
    await delete_many(db.session_all())
    db.wipe()
    STOP = True
    WIPED = True
    await st.edit_text("💥 <b>Autodistruzione completata.</b> Il bot si sta spegnendo.")
    log.warning("AUTODISTRUZIONE eseguita dall'admin")
    with suppress(Exception):
        await dp.stop_polling()


# ═══════════════════════ GESTIONE ERRORI GLOBALE ═══════════════════
@dp.errors()
async def on_error(event: ErrorEvent):
    exc = event.exception
    if isinstance(exc, (TelegramForbiddenError,)):
        return True
    if isinstance(exc, TelegramBadRequest) and any(
        s in str(exc).lower()
        for s in ("message is not modified", "message to delete not found",
                  "query is too old", "message can't be deleted")
    ):
        return True
    log.exception("Errore non gestito: %s", exc)
    with suppress(Exception):
        await notify_admin(f"⚠️ <b>Errore nel bot</b>\n<code>{esc(type(exc).__name__)}: {esc(str(exc)[:300])}</code>")
    return True


# ═════════════════════════════ MAIN ════════════════════════════════
async def main():
    global bot, BOT_USERNAME
    if not BOT_TOKEN or "INSERISCI" in BOT_TOKEN:
        raise SystemExit("❌ Imposta BOT_TOKEN in cima allo script.")
    if not ADMIN_ID or ADMIN_ID == 123456789:
        log.warning("ADMIN_ID sembra quello di esempio: impostalo con il tuo ID!")

    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp.include_router(admin_router)
    dp.include_router(user_router)

    me = await bot.get_me()
    BOT_USERNAME = me.username or ""
    log.info("Bot avviato come @%s (admin: %s)", BOT_USERNAME, ADMIN_ID)
    with suppress(TelegramAPIError):
        await bot.delete_webhook(drop_pending_updates=False)

    wtask = asyncio.create_task(worker())
    try:
        while not STOP:
            try:
                await dp.start_polling(
                    bot,
                    allowed_updates=dp.resolve_used_update_types(),
                    close_bot_session=False,
                    handle_signals=False,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Polling interrotto, riavvio tra 5s")
                await asyncio.sleep(5)
                continue
            if not STOP:
                await asyncio.sleep(3)
    finally:
        wtask.cancel()
        with suppress(Exception):
            await wtask
        with suppress(Exception):
            await bot.session.close()
        db.close()
        if WIPED:
            for ext in ("", "-wal", "-shm"):
                with suppress(Exception):
                    os.remove(DB_PATH + ext)
            if DELETE_SCRIPT_ON_BOOM:
                with suppress(Exception):
                    os.remove(os.path.abspath(__file__))
        log.info("Bot arrestato.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
