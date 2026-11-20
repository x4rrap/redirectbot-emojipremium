# -*- coding: utf-8 -*-
"""
CONVOCATORIA BOT — versione autonoma multi-admin (v2)
Migliorie:
  • Search parallela (veloce) + cache dialoghi
  • Report con bottoni: messaggio / profilo mittente / gruppo
  • Join link in background con report finale
  • Fix funzione mancante search_content_records
"""

import asyncio
import json
import os
import random
import re
import logging
import time
from datetime import datetime

from telethon import TelegramClient, events, Button
from telethon.tl.functions.channels import JoinChannelRequest
from telethon.tl.functions.messages import ImportChatInviteRequest
from telethon.tl.types import Channel, Chat, User
from telethon.errors import (
    FloodWaitError, UserAlreadyParticipantError,
    InviteHashExpiredError, InviteHashInvalidError,
    ChannelPrivateError, UsernameNotOccupiedError,
    UsernameInvalidError, SessionPasswordNeededError,
    PhoneCodeInvalidError, PhoneCodeExpiredError,
    PasswordHashInvalidError, ChatWriteForbiddenError,
)

# ======================================================================
# CONFIGURAZIONE
# ======================================================================
API_ID    = 32839277
API_HASH  = "06f13b57e8a464c4af4f8360e1ef2814"
BOT_TOKEN = "8733520607:AAHgvhsO1jDKz5wW66ZQX17bOROOt6L3rM0"

ADMIN_IDS = [8211955017, 8669550030]

# ======================================================================
# FILE / SESSIONI
# ======================================================================
BOT_SESSION   = "panel_bot"
USER_SESSION  = "user_session"

LINKS_FILE      = "newgeneration_links.txt"
KEYWORDS_FILE   = "keywords.txt"
PROGRESS_FILE   = "progress.json"
RUNTIME_FILE    = "runtime_state.json"
VOIP_VIEW_FILE  = "voip_view.txt"
VOIP_REPLY_FILE = "voip_reply.txt"

DEFAULT_REPLY = "Ciao! Ho visto il tuo messaggio e ti rispondo volentieri."

JOIN_DELAY_SECONDS      = 6
ACTION_COOLDOWN_SECONDS = 2
AUTO_DELETE_AFTER_SECONDS = 1800
MAX_LINKS_PER_ACTION    = 50

# Ricerca
SEARCH_SCAN_LIMIT       = 80     # messaggi per dialogo
SEARCH_MAX_CONCURRENT   = 6      # parallelismo
SEARCH_DIALOG_CACHE_TTL = 300    # secondi

# ======================================================================
# LOGGING
# ======================================================================
logging.basicConfig(
    level=logging.INFO,
    format="[%(levelname)s] %(asctime)s | %(message)s",
    handlers=[logging.FileHandler("convocatoria_bot.log", encoding="utf-8"),
              logging.StreamHandler()],
)
log = logging.getLogger("convocatoria")


# ======================================================================
# AUTO-CREAZIONE FILE
# ======================================================================
def ensure_files():
    for path in (LINKS_FILE, KEYWORDS_FILE, VOIP_VIEW_FILE, VOIP_REPLY_FILE):
        if not os.path.exists(path):
            open(path, "a", encoding="utf-8").close()
            log.info(f"📄 Creato file: {path}")
    if not os.path.exists(PROGRESS_FILE):
        with open(PROGRESS_FILE, "w", encoding="utf-8") as f:
            json.dump({"current_index": 0, "total_links": 0,
                       "processed": 0, "skipped": 0}, f)
    if not os.path.exists(RUNTIME_FILE):
        with open(RUNTIME_FILE, "w", encoding="utf-8") as f:
            json.dump({"queue_running": False, "queue_paused": False}, f)


# ======================================================================
# STATO
# ======================================================================
class State:
    def __init__(self):
        self.queue_running = False
        self.queue_paused  = False
        self.queue_task = None
        self.join_task  = None

        self.current_index = 0
        self.total_links   = 0
        self.processed     = 0
        self.skipped       = 0
        self.last_msg      = ""

        self.user = None
        self.bot  = None

        self.login_phone = None
        self.login_hash  = None
        self.login_awaiting_code = False
        self.login_awaiting_2fa  = False

        self.waiting_input = None
        self.admin_bot_messages = {}
        self.last_action_at = {}

state = State()


# ======================================================================
# FILE HELPERS
# ======================================================================
def _read_lines(path):
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        return [l.strip() for l in f if l.strip()]

def _write_lines(path, items):
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(items))

def load_progress():
    try:
        with open(PROGRESS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"current_index": 0, "total_links": 0, "processed": 0, "skipped": 0}

def save_progress():
    with open(PROGRESS_FILE, "w", encoding="utf-8") as f:
        json.dump({
            "current_index": state.current_index,
            "total_links":   state.total_links,
            "processed":     state.processed,
            "skipped":       state.skipped,
        }, f, indent=2, ensure_ascii=False)

def save_runtime():
    with open(RUNTIME_FILE, "w", encoding="utf-8") as f:
        json.dump({
            "queue_running": state.queue_running,
            "queue_paused":  state.queue_paused,
        }, f, ensure_ascii=False)

def load_runtime():
    try:
        with open(RUNTIME_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
            state.queue_paused = bool(d.get("queue_paused", False))
    except Exception:
        pass


# ======================================================================
# PARSING LINK E URL MESSAGGIO
# ======================================================================
RE_INVITE = re.compile(r"(?:t(?:elegram)?\.me)/(?:\+|joinchat/)([A-Za-z0-9_\-]+)", re.I)
RE_USER   = re.compile(r"(?:t(?:elegram)?\.me)/([A-Za-z0-9_]{4,})(?:/\d+)?", re.I)

def parse_link(link: str):
    m = RE_INVITE.search(link)
    if m:
        return "invite", m.group(1)
    m = RE_USER.search(link)
    if m:
        name = m.group(1)
        if name.lower() in ("joinchat", "addstickers", "share", "iv"):
            return None, None
        return "username", name
    return None, None

def build_message_link(chat, message_id: int) -> str:
    username = getattr(chat, "username", None)
    if username:
        return f"https://t.me/{username}/{message_id}"
    cid = str(chat.id)
    if cid.startswith("-100"):
        cid = cid[4:]
    elif cid.startswith("-"):
        cid = cid[1:]
    return f"https://t.me/c/{cid}/{message_id}"

def build_group_link(chat) -> str | None:
    username = getattr(chat, "username", None)
    if username:
        return f"https://t.me/{username}"
    return None


# ======================================================================
# FLOODWAIT WRAPPER
# ======================================================================
async def safe(coro_factory, *args, **kwargs):
    for _ in range(6):
        try:
            return await coro_factory(*args, **kwargs)
        except FloodWaitError as e:
            wait = int(e.seconds) + 3
            log.warning(f"FloodWait {e.seconds}s → attendo {wait}s")
            await asyncio.sleep(wait)
    return None


# ======================================================================
# LOGIN
# ======================================================================
async def start_login(phone: str):
    if state.user:
        try:
            await state.user.disconnect()
        except Exception:
            pass
    state.user = TelegramClient(USER_SESSION, API_ID, API_HASH)
    await state.user.connect()
    try:
        sent = await state.user.send_code_request(phone)
        state.login_phone = phone
        state.login_hash  = sent.phone_code_hash
        state.login_awaiting_code = True
        state.login_awaiting_2fa  = False
        return (f"📩 Codice inviato a `{phone}`.\n\n"
                f"Ora invia il codice che hai ricevuto su Telegram (solo le cifre).")
    except FloodWaitError as e:
        return f"⚠️ FloodWait: riprova tra {e.seconds}s."
    except Exception as e:
        return f"❌ Errore invio codice: {e}"

async def submit_code(code: str):
    if not state.login_awaiting_code or not state.user:
        return "❌ Nessun login in corso."
    try:
        await state.user.sign_in(
            phone=state.login_phone, code=code,
            phone_code_hash=state.login_hash,
        )
    except SessionPasswordNeededError:
        state.login_awaiting_code = False
        state.login_awaiting_2fa  = True
        return "🔐 2FA attivo. Invia ora la tua password Telegram."
    except (PhoneCodeInvalidError, PhoneCodeExpiredError) as e:
        return f"❌ Codice non valido/scaduto: {e}"
    except FloodWaitError as e:
        return f"⚠️ FloodWait: riprova tra {e.seconds}s."
    except Exception as e:
        return f"❌ Errore: {e}"
    state.login_awaiting_code = False
    return "✅ Account collegato con successo!"

async def submit_password(pwd: str):
    if not state.login_awaiting_2fa or not state.user:
        return "❌ Nessuna 2FA in attesa."
    try:
        await state.user.sign_in(password=pwd)
        state.login_awaiting_2fa = False
        return "✅ Account collegato con 2FA!"
    except PasswordHashInvalidError:
        return "❌ Password errata."
    except Exception as e:
        return f"❌ Errore: {e}"

def user_ready() -> bool:
    return state.user is not None and state.user.is_connected()

async def user_authorized() -> bool:
    if not user_ready():
        return False
    try:
        return await state.user.is_user_authorized()
    except Exception:
        return False


# ======================================================================
# JOIN LINK
# ======================================================================
async def join_link(link: str):
    kind, val = parse_link(link)
    if not kind:
        return False, f"Link non riconosciuto: {link}"
    try:
        if kind == "invite":
            await safe(state.user(ImportChatInviteRequest(val)))
            return True, f"Unito via invito: {link}"
        entity = await safe(state.user.get_entity, val)
        if entity is None:
            return False, f"Entità non trovata: {val}"
        if not isinstance(entity, (Channel, Chat)):
            return False, f"Non è gruppo/canale: {val}"
        await safe(state.user(JoinChannelRequest(entity)))
        return True, f"Unito a: {val}"
    except UserAlreadyParticipantError:
        return False, f"Già membro: {link}"
    except (InviteHashExpiredError, InviteHashInvalidError):
        return False, f"Invito scaduto/invalido: {link}"
    except (ChannelPrivateError, UsernameNotOccupiedError, UsernameInvalidError):
        return False, f"Gruppo inesistente/privato: {link}"
    except Exception as e:
        return False, f"Errore su {link}: {e}"


async def join_links_background(links, chat_id, msg_id):
    """Entra nei link in background e aggiorna il report all'admin."""
    if not await user_authorized():
        try:
            await state.bot.edit_message(chat_id, msg_id,
                "❌ Account non collegato.", buttons=kb_links())
        except Exception:
            pass
        return

    success, already, failed = 0, 0, 0
    report = []
    for i, link in enumerate(links, 1):
        ok, msg = await join_link(link)
        if ok:
            success += 1
            report.append(f"✅ {msg}")
        else:
            low = msg.lower()
            if "già membro" in low:
                already += 1
                report.append(f"ℹ️ {msg}")
            else:
                failed += 1
                report.append(f"⚠️ {msg}")
        # aggiorna progress live ogni 5 link
        if i % 5 == 0 or i == len(links):
            txt = (f"⏳ **Ingresso link in corso** ({i}/{len(links)})\n"
                   f"✅ {success}  ℹ️ {already}  ⚠️ {failed}")
            try:
                await state.bot.edit_message(chat_id, msg_id, txt)
            except Exception:
                pass
        await asyncio.sleep(JOIN_DELAY_SECONDS)

    final = (f"📊 **Report ingresso link**\n"
             f"✅ Successi: {success}\n"
             f"ℹ️ Già membro: {already}\n"
             f"⚠️ Falliti: {failed}\n\n"
             + "\n".join(report))
    try:
        await state.bot.edit_message(chat_id, msg_id, final[:4000], buttons=kb_links())
    except Exception as e:
        log.error(f"Errore report join: {e}")


# ======================================================================
# CODA LINK (riprende i link già in lista)
# ======================================================================
async def run_queue():
    links = _read_lines(LINKS_FILE)
    if not links:
        log.warning("Nessun link nel file.")
        state.queue_running = False
        return

    prog = load_progress()
    state.current_index = prog.get("current_index", 0)
    state.total_links   = len(links)
    state.processed     = prog.get("processed", 0)
    state.skipped       = prog.get("skipped", 0)

    state.queue_running = True
    save_runtime()
    log.info(f"▶️ Coda avviata da {state.current_index}/{state.total_links}")

    for i in range(state.current_index, len(links)):
        if not state.queue_running:
            break
        while state.queue_paused and state.queue_running:
            await asyncio.sleep(2)

        link = links[i]
        ok, msg = await join_link(link)
        if ok:
            state.processed += 1
            log.info(f"✅ {msg}")
        else:
            state.skipped += 1
            log.info(f"⏭ {msg}")
        state.last_msg = msg
        state.current_index = i + 1
        save_progress()
        await asyncio.sleep(random.uniform(3, 8))

    state.queue_running = False
    state.queue_paused  = False
    save_progress()
    save_runtime()
    log.info("🏁 Coda terminata.")

def start_queue_task():
    if state.queue_task and not state.queue_task.done():
        return False
    state.queue_task = asyncio.create_task(run_queue())
    return True


# ======================================================================
# RICERCA CONTENUTO — VERSIONE VELOCE E CONCORRENTE
# ======================================================================
_dialog_cache = {"list": [], "at": 0.0}

async def get_dialogs_cached(force=False):
    now = time.time()
    if not force and _dialog_cache["list"] and (now - _dialog_cache["at"]) < SEARCH_DIALOG_CACHE_TTL:
        return _dialog_cache["list"]
    dialogs = [d async for d in state.user.iter_dialogs()]
    _dialog_cache["list"] = dialogs
    _dialog_cache["at"] = now
    return dialogs


async def search_content_records(query: str, limit: int = 30,
                                 scan_limit: int = SEARCH_SCAN_LIMIT):
    """
    Cerca `query` in tutti i gruppi/canali dell'account.
    Ritorna dict: {ok, results, scanned, total, error?}
    Ogni risultato ha: text, sender_name, sender_username, sender_id,
                       group_name, group_type, group_link, message_link, date
    """
    if not await user_authorized():
        return {"ok": False, "error": "❌ Nessun account collegato."}
    q = (query or "").lower().strip()
    if not q:
        return {"ok": False, "error": "Query vuota."}

    results = []
    scanned = [0]
    sem = asyncio.Semaphore(SEARCH_MAX_CONCURRENT)
    dialogs = await get_dialogs_cached()

    async def scan(dialog):
        if not isinstance(dialog.entity, (Channel, Chat)):
            return
        async with sem:
            try:
                async for m in state.user.iter_messages(dialog.entity, limit=scan_limit):
                    if not m.text:
                        continue
                    scanned[0] += 1
                    if q not in m.text.lower():
                        continue

                    sender_name, sender_username, sender_id = "—", "—", None
                    try:
                        s = m.sender
                        if isinstance(s, User):
                            sender_name = " ".join(p for p in [s.first_name, s.last_name] if p) or "Utente"
                            if s.username:
                                sender_username = f"@{s.username}"
                            else:
                                sender_username = f"id:{s.id}"
                            sender_id = s.id
                        elif isinstance(s, (Channel, Chat)):
                            sender_name = s.title or "Canale/Gruppo"
                            sender_username = (f"@{s.username}" if getattr(s, "username", None)
                                               else f"id:{s.id}")
                            sender_id = s.id
                    except Exception:
                        pass

                    results.append({
                        "text": m.text.strip(),
                        "sender_name": sender_name,
                        "sender_username": sender_username,
                        "sender_id": sender_id,
                        "group_name": dialog.name,
                        "group_type": "Canale" if isinstance(dialog.entity, Channel) else "Gruppo",
                        "group_link": build_group_link(dialog.entity),
                        "message_link": build_message_link(dialog.entity, m.id),
                        "date": m.date,
                    })
            except FloodWaitError as e:
                await asyncio.sleep(e.seconds + 2)
            except Exception as exc:
                log.debug(f"Skip {getattr(dialog,'name','?')}: {exc}")

    await asyncio.gather(*[scan(d) for d in dialogs])

    results.sort(key=lambda x: x["date"] or datetime.min, reverse=True)
    return {"ok": True, "results": results[:limit],
            "scanned": scanned[0], "total": len(results)}


def result_buttons(result):
    """Costruisce i bottoni URL per ogni risultato."""
    rows = []
    if result.get("message_link"):
        rows.append([Button.url("🔗 Apri messaggio", result["message_link"])])

    sender_username = result.get("sender_username", "") or ""
    sender_id = result.get("sender_id")
    if sender_username.startswith("@"):
        rows.append([Button.url("👤 Profilo mittente", f"https://t.me/{sender_username[1:]}")])
    elif sender_id:
        rows.append([Button.url("👤 Profilo mittente", f"tg://user?id={sender_id}")])

    if result.get("group_link"):
        rows.append([Button.url("📁 Apri gruppo/canale", result["group_link"])])
    return rows


async def send_search_results(e, text, limit=30):
    """Esegue la ricerca, manda i risultati con i bottoni."""
    await action_pause(e.sender_id)
    res = await search_content_records(text, limit=limit)
    if not res.get("ok"):
        await clean_respond(e, res.get("error", "Errore ricerca."), buttons=kb_main())
        return
    if not res.get("results"):
        await clean_respond(
            e,
            f"🔎 Nessun risultato per: `{text}`\n"
            f"Messaggi analizzati: {res.get('scanned', 0)}",
            buttons=kb_main(),
        )
        return

    await clean_respond(
        e,
        f"🔎 **Trovati {res['total']} messaggi** per: `{text}`\n"
        f"Messaggi analizzati: {res.get('scanned', 0)}\n"
        f"(mostro i {min(limit, len(res['results']))} più recenti)",
        buttons=kb_main(),
    )

    for r in res["results"]:
        date_str = r["date"].strftime("%d/%m/%Y %H:%M") if r["date"] else "—"
        body = (f"📝 **Messaggio**\n{r['text'][:2500]}\n\n"
                f"👤 **Mittente:** {r['sender_name']} — {r['sender_username']}\n"
                f"📁 **{r['group_type']}:** {r['group_name']}\n"
                f"🕒 {date_str}")
        try:
            await clean_result_respond(e, body, buttons=result_buttons(r))
        except Exception as exc:
            log.debug(f"Invio risultato fallito: {exc}")


# ======================================================================
# AUTORISPOSTA / BLOCCO / VoIP
# ======================================================================
async def auto_reply_keywords(keywords, reply_text, max_groups: int = 10):
    if not await user_authorized():
        return "❌ Nessun account collegato."
    if not keywords:
        return "Nessuna keyword impostata."
    sent = 0
    async for d in state.user.iter_dialogs():
        if not isinstance(d.entity, (Channel, Chat)):
            continue
        if sent >= max_groups:
            break
        found = False
        try:
            async for m in state.user.iter_messages(d.entity, limit=60):
                if m.text and any(k.lower() in m.text.lower() for k in keywords):
                    found = True
                    break
        except Exception:
            continue
        if found:
            try:
                await safe(state.user.send_message, d.entity, reply_text)
                sent += 1
                await asyncio.sleep(random.uniform(5, 12))
            except ChatWriteForbiddenError:
                continue
            except Exception as e:
                log.error(f"Invio fallito su {d.name}: {e}")
    return f"✅ Messaggi inviati in {sent} gruppi."

async def forward_voip_view():
    if not await user_authorized():
        return "❌ Nessun account collegato."
    numbers = _read_lines(VOIP_VIEW_FILE)
    if not numbers:
        return "Lista VoIP visualizzazione vuota."
    if not state.bot:
        return "❌ Bot non pronto."
    ok = 0
    for num in numbers:
        for admin in ADMIN_IDS:
            try:
                await state.bot.send_message(admin, f"👁 VoIP (view): `{num}`")
                ok += 1
            except Exception:
                pass
    return f"👁 Inoltrati {ok} messaggi (numeri × admin)."

async def block_recent(limit: int = 25):
    if not await user_authorized():
        return "❌ Nessun account collegato."
    blocked = 0
    async for d in state.user.iter_dialogs():
        if isinstance(d.entity, User) and not d.entity.bot and not d.entity.self:
            try:
                await safe(state.user.block_user, d.entity)
                blocked += 1
                await asyncio.sleep(1.5)
            except Exception as e:
                log.error(f"Errore blocco: {e}")
        if blocked >= limit:
            break
    return f"🚫 Bloccati {blocked} contatti."


# ======================================================================
# TASTIERE
# ======================================================================
def kb_main():
    return [
        [Button.inline("➕ Aggiungi Account",  b"add_acc")],
        [Button.inline("▶️ Avvia Coda",       b"q_start"),
         Button.inline("⏸ Pausa",            b"q_pause"),
         Button.inline("⏹ Stop",             b"q_stop")],
        [Button.inline("📊 Stato",            b"status")],
        [Button.inline("🔗 Gestione Link",    b"menu_links")],
        [Button.inline("🔑 Gestione Keyword", b"menu_kw")],
        [Button.inline("📞 Gestione VoIP",    b"menu_voip")],
        [Button.inline("🔎 Cerca Contenuto",  b"search")],
        [Button.inline("💬 Auto-Risposta",    b"autoreply")],
        [Button.inline("🚫 Blocca Contatti",  b"block")],
    ]

def kb_links():
    return [
        [Button.inline("➕ Aggiungi link",   b"lnk_add"),
         Button.inline("📋 Vedi link",      b"lnk_view")],
        [Button.inline("🗑 Rimuovi link",   b"lnk_del"),
         Button.inline("🧹 Svuota lista",   b"lnk_clear")],
        [Button.inline("⬅️ Indietro",       b"menu_main")],
    ]

def kb_kw():
    return [
        [Button.inline("➕ Aggiungi keyword", b"kw_add"),
         Button.inline("📋 Vedi keyword",     b"kw_view")],
        [Button.inline("🗑 Rimuovi keyword",  b"kw_del"),
         Button.inline("🧹 Svuota lista",     b"kw_clear")],
        [Button.inline("⬅️ Indietro",         b"menu_main")],
    ]

def kb_voip():
    return [
        [Button.inline("➕ VoIP (view)",     b"voip_add_view"),
         Button.inline("➕ VoIP (reply)",    b"voip_add_reply")],
        [Button.inline("📋 Vedi liste",      b"voip_show"),
         Button.inline("🧹 Svuota view",     b"voip_clear_view"),
         Button.inline("🧹 Svuota reply",    b"voip_clear_reply")],
        [Button.inline("👁 Inoltra view",    b"voip_forward"),
         Button.inline("💬 Testo auto-risposta", b"set_reply")],
        [Button.inline("⬅️ Indietro",        b"menu_main")],
    ]

def kb_cancel():
    return [[Button.inline("❌ Annulla", b"menu_main")]]


# ======================================================================
# PULIZIA MESSAGGI
# ======================================================================
async def _cleanup_admin_messages(chat_id):
    ids = state.admin_bot_messages.pop(chat_id, [])
    if ids:
        try:
            await state.bot.delete_messages(chat_id, ids)
        except Exception:
            pass

async def clean_respond(event, text, buttons=None, delete_previous=True):
    chat_id = event.chat_id
    if delete_previous:
        await _cleanup_admin_messages(chat_id)
    msg = await event.respond(text, buttons=buttons)
    state.admin_bot_messages.setdefault(chat_id, []).append(msg.id)
    return msg

async def clean_result_respond(event, text, buttons=None):
    msg = await clean_respond(event, text, buttons=buttons, delete_previous=False)
    async def later_delete():
        await asyncio.sleep(AUTO_DELETE_AFTER_SECONDS)
        try:
            await state.bot.delete_messages(event.chat_id, msg.id)
        except Exception:
            pass
        ids = state.admin_bot_messages.get(event.chat_id, [])
        if msg.id in ids:
            ids.remove(msg.id)
    asyncio.create_task(later_delete())
    return msg

async def action_pause(admin_id):
    now = asyncio.get_running_loop().time()
    last = state.last_action_at.get(admin_id, 0)
    wait = ACTION_COOLDOWN_SECONDS - (now - last)
    if wait > 0:
        await asyncio.sleep(wait)
    state.last_action_at[admin_id] = asyncio.get_running_loop().time()


# ======================================================================
# HANDLERS
# ======================================================================
def register_handlers(bot: TelegramClient):

    @bot.on(events.NewMessage(pattern=r"^/start(?:@\w+)?$"))
    async def _start(e):
        if e.sender_id not in ADMIN_IDS:
            return await e.respond("⛔ Non sei autorizzato.")
        state.waiting_input = None
        await e.respond(
            "🤖 **Convocatoria Bot – Pannello v2**\n\n"
            "1️⃣ **➕ Aggiungi Account**\n"
            "2️⃣ Aggiungi i link dal menu **🔗 Gestione Link**\n"
            "3️⃣ Avvia la coda con **▶️ Avvia Coda**\n"
            "4️⃣ Usa **🔎 Cerca Contenuto** per trovare messaggi nei gruppi.",
            buttons=kb_main(),
        )

    @bot.on(events.NewMessage(pattern=r"^/id$"))
    async def _id(e):
        await e.respond(f"🆔 Il tuo ID: `{e.sender_id}`")

    @bot.on(events.NewMessage(pattern=r"^/cerca(?:\s+(.+))?$"))
    async def _cerca(e):
        if e.sender_id not in ADMIN_IDS:
            return
        text = (e.pattern_match.group(1) or "").strip()
        if not text:
            state.waiting_input = "search_text"
            await clean_respond(e, "🔎 Scrivi il testo o la keyword da cercare.\n\n/cancel per annullare.", buttons=kb_cancel())
            return
        await clean_respond(e, "⏳ Ricerca in corso…")
        await send_search_results(e, text)

    @bot.on(events.NewMessage(pattern=r"^/cancel$"))
    async def _cancel(e):
        if e.sender_id not in ADMIN_IDS:
            return
        state.waiting_input = None
        await e.respond("❌ Operazione annullata.", buttons=kb_main())

    # ---------- INPUT TESTUALE ----------
    @bot.on(events.NewMessage(func=lambda e: e.is_private and e.text and not e.text.startswith("/")))
    async def _input(e):
        if e.sender_id not in ADMIN_IDS:
            return

        if state.login_awaiting_code:
            txt = e.text.strip()
            if txt.startswith("+") and re.fullmatch(r"\+\d{6,15}", txt):
                state.login_awaiting_code = False
                await e.respond(await start_login(txt))
                return
            await e.respond(await submit_code(txt))
            return

        if state.login_awaiting_2fa:
            await e.respond(await submit_password(e.text.strip()))
            return

        action = state.waiting_input
        if not action:
            return
        txt = e.text.strip()
        state.waiting_input = None

        if action == "search_text":
            await clean_respond(e, "⏳ Ricerca in corso…")
            await send_search_results(e, txt)
            return

        if action == "add_acc":
            if not re.fullmatch(r"\+\d{6,15}", txt):
                await e.respond("❌ Numero non valido. Usa `+393331234567`.", buttons=kb_main())
                return
            await clean_respond(e, "⏳ Invio del codice…")
            await e.respond(await start_login(txt))
            return

        if action == "lnk_add":
            await action_pause(e.sender_id)
            items = _read_lines(LINKS_FILE)
            links = [c.strip() for c in re.split(r"[\s,;]+", txt) if c.strip()]
            links = links[:MAX_LINKS_PER_ACTION]
            added = 0
            for link in links:
                if link not in items:
                    items.append(link); added += 1
            _write_lines(LINKS_FILE, items)

            if not await user_authorized():
                await clean_respond(e, f"✅ Salvati {added} link. Collega prima l'account: poi avvia la coda.", buttons=kb_links())
                return

            msg = await clean_respond(
                e,
                f"⏳ Ingresso in {len(links)} link in background…\n"
                f"Riceverai un report a fine operazione.",
                buttons=kb_links(),
            )
            asyncio.create_task(join_links_background(links, e.chat_id, msg.id))
            return

        if action == "lnk_del":
            items = _read_lines(LINKS_FILE)
            if txt in items:
                items.remove(txt)
                _write_lines(LINKS_FILE, items)
                await e.respond(f"🗑 Rimosso: {txt}", buttons=kb_links())
            else:
                await e.respond("❌ Link non presente.", buttons=kb_links())
            return

        if action == "kw_add":
            items = _read_lines(KEYWORDS_FILE)
            added = 0
            for c in re.split(r"[\n,;]+", txt):
                c = c.strip()
                if c and c not in items:
                    items.append(c); added += 1
            _write_lines(KEYWORDS_FILE, items)
            await e.respond(f"✅ Aggiunte {added} keyword. Totale: {len(items)}", buttons=kb_kw())
            return

        if action == "kw_del":
            items = _read_lines(KEYWORDS_FILE)
            if txt in items:
                items.remove(txt)
                _write_lines(KEYWORDS_FILE, items)
                await e.respond(f"🗑 Rimossa: {txt}", buttons=kb_kw())
            else:
                await e.respond("❌ Keyword non presente.", buttons=kb_kw())
            return

        if action == "voip_add_view":
            items = _read_lines(VOIP_VIEW_FILE)
            for c in re.split(r"[\n,;]+", txt):
                c = c.strip()
                if c and c not in items:
                    items.append(c)
            _write_lines(VOIP_VIEW_FILE, items)
            await e.respond(f"✅ Lista VoIP view aggiornata ({len(items)} elementi).", buttons=kb_voip())
            return

        if action == "voip_add_reply":
            items = _read_lines(VOIP_REPLY_FILE)
            for c in re.split(r"[\n,;]+", txt):
                c = c.strip()
                if c and c not in items:
                    items.append(c)
            _write_lines(VOIP_REPLY_FILE, items)
            await e.respond(f"✅ Lista VoIP reply aggiornata ({len(items)} elementi).", buttons=kb_voip())
            return

        if action == "set_reply":
            global DEFAULT_REPLY
            DEFAULT_REPLY = txt
            await e.respond(f"✅ Nuovo testo auto-risposta:\n_{DEFAULT_REPLY}_", buttons=kb_voip())
            return

    # ---------- CALLBACK ----------
    @bot.on(events.CallbackQuery())
    async def _cb(e):
        if e.sender_id not in ADMIN_IDS:
            return await e.answer("Non autorizzato.", alert=True)
        d = e.data.decode()
        state.waiting_input = None

        if d == "menu_main":
            await e.answer()
            await e.edit("🤖 **Pannello principale**", buttons=kb_main())

        elif d == "menu_links":
            n = len(_read_lines(LINKS_FILE))
            await e.answer()
            await e.edit(f"🔗 **Gestione Link**  ({n} link)", buttons=kb_links())

        elif d == "menu_kw":
            n = len(_read_lines(KEYWORDS_FILE))
            await e.answer()
            await e.edit(f"🔑 **Gestione Keyword**  ({n} keyword)", buttons=kb_kw())

        elif d == "menu_voip":
            await e.answer()
            await e.edit("📞 **Gestione VoIP**", buttons=kb_voip())

        # --- LINK ---
        elif d == "lnk_add":
            await e.answer()
            state.waiting_input = "lnk_add"
            await e.edit(
                "✏️ Inviami uno o più link (anche su più righe).\n\n"
                "Esempi:\n`https://t.me/nomegruppo`\n`https://t.me/+AbCdEf123`\n\n"
                "Entrerò automaticamente (o invierò richiesta) in ognuno.\n"
                "Usa /cancel per annullare.",
                buttons=kb_cancel(),
            )

        elif d == "lnk_view":
            items = _read_lines(LINKS_FILE)
            if not items:
                await e.answer()
                await e.edit("📋 Lista link vuota.", buttons=kb_links())
                return
            txt = f"📋 **{len(items)} link:**\n\n"
            for i, lnk in enumerate(items[:40], 1):
                txt += f"`{i}.` {lnk}\n"
            if len(items) > 40:
                txt += f"\n… e altri {len(items)-40}."
            await e.answer()
            await e.edit(txt, buttons=kb_links())

        elif d == "lnk_del":
            await e.answer()
            state.waiting_input = "lnk_del"
            await e.edit("✏️ Inviami il link ESATTO da rimuovere.", buttons=kb_cancel())

        elif d == "lnk_clear":
            _write_lines(LINKS_FILE, [])
            await e.answer("Lista svuotata.")
            await e.edit("🧹 Lista link svuotata.", buttons=kb_links())

        # --- KEYWORD ---
        elif d == "kw_add":
            await e.answer()
            state.waiting_input = "kw_add"
            await e.edit("✏️ Inviami una o più keyword (separate da riga o virgola).",
                         buttons=kb_cancel())

        elif d == "kw_view":
            items = _read_lines(KEYWORDS_FILE)
            txt = "📋 **Keyword:**\n\n" + ("\n".join(f"• {k}" for k in items) or "—")
            await e.answer()
            await e.edit(txt, buttons=kb_kw())

        elif d == "kw_del":
            await e.answer()
            state.waiting_input = "kw_del"
            await e.edit("✏️ Inviami la keyword ESATTA da rimuovere.", buttons=kb_cancel())

        elif d == "kw_clear":
            _write_lines(KEYWORDS_FILE, [])
            await e.answer("Keyword svuotate.")
            await e.edit("🧹 Lista keyword svuotata.", buttons=kb_kw())

        # --- VoIP ---
        elif d == "voip_add_view":
            await e.answer()
            state.waiting_input = "voip_add_view"
            await e.edit("✏️ Inviami i numeri VoIP per la lista *view*.", buttons=kb_cancel())

        elif d == "voip_add_reply":
            await e.answer()
            state.waiting_input = "voip_add_reply"
            await e.edit("✏️ Inviami i numeri VoIP per la lista *reply*.", buttons=kb_cancel())

        elif d == "voip_show":
            v = _read_lines(VOIP_VIEW_FILE)
            r = _read_lines(VOIP_REPLY_FILE)
            txt  = "📄 **Liste VoIP**\n\n"
            txt += "👁 View:\n"  + ("\n".join(f"• {x}" for x in v) or "—") + "\n\n"
            txt += "💬 Reply:\n" + ("\n".join(f"• {x}" for x in r) or "—") + "\n\n"
            txt += f"Testo auto-risposta:\n_{DEFAULT_REPLY}_"
            await e.answer()
            await e.edit(txt, buttons=kb_voip())

        elif d == "voip_clear_view":
            _write_lines(VOIP_VIEW_FILE, [])
            await e.answer("Svuotata.")
            await e.edit("🧹 Lista VoIP view svuotata.", buttons=kb_voip())

        elif d == "voip_clear_reply":
            _write_lines(VOIP_REPLY_FILE, [])
            await e.answer("Svuotata.")
            await e.edit("🧹 Lista VoIP reply svuotata.", buttons=kb_voip())

        elif d == "voip_forward":
            await e.answer("Inoltro…")
            await e.edit(await forward_voip_view(), buttons=kb_voip())

        elif d == "set_reply":
            await e.answer()
            state.waiting_input = "set_reply"
            await e.edit("✏️ Inviami il nuovo testo di auto-risposta.", buttons=kb_cancel())

        # --- ACCOUNT ---
        elif d == "add_acc":
            await e.answer()
            if await user_authorized():
                me = await state.user.get_me()
                await e.edit(
                    f"✅ Account già collegato: **{me.first_name}** (@{me.username or 'no-username'})\n\n"
                    f"Vuoi sostituirlo? Inviami il nuovo numero con prefisso internazionale.\n"
                    f"Es: `+393331234567`\n/cancel per annullare.",
                    buttons=kb_cancel(),
                )
            else:
                await e.edit(
                    "➕ **Collega account Telegram**\n\n"
                    "Inviami il numero di telefono con prefisso internazionale.\n"
                    "Es: `+393331234567`\n\n/cancel per annullare.",
                    buttons=kb_cancel(),
                )
            state.waiting_input = "add_acc"

        # --- CODA ---
        elif d == "q_start":
            if state.queue_running:
                await e.answer("Coda già attiva.", alert=True); return
            if not await user_authorized():
                await e.answer("Nessun account collegato.", alert=True); return
            if not _read_lines(LINKS_FILE):
                await e.answer("Nessun link. Aggiungili dal menu 🔗.", alert=True); return
            ok = start_queue_task()
            await e.answer("Coda avviata." if ok else "Errore avvio.")
            await e.edit("▶️ Coda avviata.", buttons=kb_main())

        elif d == "q_pause":
            if not state.queue_running:
                await e.answer("Coda non attiva.", alert=True); return
            state.queue_paused = not state.queue_paused
            save_runtime()
            msg = "⏸ In pausa." if state.queue_paused else "▶️ Ripresa."
            await e.answer(msg)
            await e.edit(msg, buttons=kb_main())

        elif d == "q_stop":
            if not state.queue_running:
                await e.answer("Coda non attiva.", alert=True); return
            state.queue_running = False
            state.queue_paused  = False
            save_progress(); save_runtime()
            await e.answer("Coda fermata.")
            await e.edit("⏹ Coda fermata.", buttons=kb_main())

        elif d == "status":
            acc = "✅" if await user_authorized() else "❌"
            txt = (
                f"📊 **Stato**\n\n"
                f"• Account: {acc}\n"
                f"• Coda attiva: {'✅' if state.queue_running else '❌'}\n"
                f"• In pausa: {'⏸' if state.queue_paused else '▶️'}\n"
                f"• Link totali: {state.total_links}\n"
                f"• Processati: {state.processed}\n"
                f"• Saltati: {state.skipped}\n"
                f"• Posizione: {state.current_index}/{state.total_links}\n"
                f"• Ultimo: {state.last_msg or '—'}\n"
                f"• Link in lista: {len(_read_lines(LINKS_FILE))}\n"
                f"• Keyword: {len(_read_lines(KEYWORDS_FILE))}\n"
                f"• VoIP view: {len(_read_lines(VOIP_VIEW_FILE))}\n"
                f"• VoIP reply: {len(_read_lines(VOIP_REPLY_FILE))}\n"
            )
            await e.answer()
            await e.edit(txt, buttons=kb_main())

        elif d == "search":
            await e.answer()
            state.waiting_input = "search_text"
            await e.edit("🔎 Scrivi il testo o la keyword da cercare nei gruppi e canali.\n\n/cancel per annullare.", buttons=kb_cancel())

        elif d == "autoreply":
            kws = _read_lines(KEYWORDS_FILE)
            if not kws:
                await e.answer("Aggiungi prima delle keyword.", alert=True); return
            await e.answer("Invio…")
            await e.edit("⏳ Invio risposte automatiche…", buttons=kb_main())
            await e.edit(await auto_reply_keywords(kws, DEFAULT_REPLY), buttons=kb_main())

        elif d == "block":
            await e.answer("Blocco…")
            await e.edit("⏳ Blocco contatti in corso…", buttons=kb_main())
            await e.edit(await block_recent(), buttons=kb_main())


# ======================================================================
# AVVIO
# ======================================================================
async def main():
    ensure_files()
    load_runtime()

    if os.path.exists(f"{USER_SESSION}.session"):
        try:
            state.user = TelegramClient(USER_SESSION, API_ID, API_HASH)
            await state.user.connect()
            if await state.user.is_user_authorized():
                me = await state.user.get_me()
                log.info(f"✅ Sessione utente ripristinata: {me.first_name}")
            else:
                log.warning("⚠️ Sessione presente ma non autorizzata.")
        except Exception as e:
            log.error(f"Errore ripristino sessione: {e}")

    state.bot = TelegramClient(BOT_SESSION, API_ID, API_HASH)
    await state.bot.start(bot_token=BOT_TOKEN)
    register_handlers(state.bot)

    log.info("🤖 Convocatoria Bot v2 pronto. Apri il bot e premi /start.")
    await state.bot.run_until_disconnected()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Chiusura…")