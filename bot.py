import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone

from telegram import Update, ChatPermissions, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, MessageHandler, CallbackQueryHandler, ChatMemberHandler, CommandHandler, filters, ContextTypes
from telegram.ext import JobQueue
from telegram.error import TelegramError

logging.basicConfig(format="%(asctime)s [%(levelname)s] %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)

def require_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Environment variable {name} is required")
    return value


def require_env_int(name: str) -> int:
    raw_value = require_env(name)
    try:
        return int(raw_value)
    except ValueError as exc:
        raise RuntimeError(f"Environment variable {name} must be an integer") from exc


BOT_TOKEN = require_env("BOT_TOKEN")
CREATOR_ID = require_env_int("CREATOR_ID")
ALLOWED_CHAT = require_env_int("ALLOWED_CHAT")
DATA_FILE = os.getenv("DATA_FILE", "data/db.json")

RANK_TITLE = {5:"Создатель",4:"Старший администратор",3:"Администратор",2:"Старший модератор",1:"Модератор"}
RANK_STARS = {5:"⭐⭐⭐⭐⭐",4:"⭐⭐⭐⭐",3:"⭐⭐⭐",2:"⭐⭐",1:"⭐"}
RANK_ICON  = {5:"👑",4:"🛡",3:"🔰",2:"🔸",1:"🔹"}

WARN_EXPIRE_DAYS = 7  # Варны действуют 7 дней

def load_db():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE,"r",encoding="utf-8") as f:
            db = json.load(f)
        # Гарантируем chat_id из константы после рестарта
        if not db.get("chat_id"):
            db["chat_id"] = ALLOWED_CHAT
            save_db(db)
        return db
    # Свежая БД — chat_id берём сразу из константы
    return {
        "chat_id": ALLOWED_CHAT,
        "admins": {str(CREATOR_ID): {"rank": 5, "username": None, "name": "Создатель"}},
        "warns": {},
        "mutes": {},
        "bans": {},
        "members": {},
        "ban_forms": [],
        "msg_stats": {}
    }

def save_db(db):
    data_dir = os.path.dirname(DATA_FILE)
    if data_dir:
        os.makedirs(data_dir, exist_ok=True)
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False, indent=2)

def get_rank(db, uid):
    if uid == CREATOR_ID: return 5
    return db["admins"].get(str(uid), {}).get("rank", 0)

def can_promote(pr, tr):
    return pr >= 4 and 1 <= tr < pr

async def find_member(context, db, username):
    username = username.lower().lstrip("@")
    chat_id = db.get("chat_id")
    if not chat_id:
        return None
    for uid, info in db.get("members", {}).items():
        if (info.get("username") or "").lower() == username:
            class FU:
                id = int(uid)
                full_name = info.get("name", uid)
                first_name = info.get("name", uid)
                username = info.get("username")
            return FU()
    try:
        member = await context.bot.get_chat_member(chat_id, f"@{username}")
        u = member.user
        db.setdefault("members", {})[str(u.id)] = {"username": u.username, "name": u.full_name}
        save_db(db)
        return u
    except TelegramError as e:
        logger.warning(f"find_member get_chat_member @{username}: {e}")
    try:
        chat = await context.bot.get_chat(f"@{username}")
        class FC:
            id = chat.id
            full_name = chat.full_name or chat.first_name or username
            first_name = chat.first_name or username
            username = chat.username
        db.setdefault("members", {})[str(chat.id)] = {"username": chat.username, "name": FC.full_name}
        save_db(db)
        return FC()
    except TelegramError as e:
        logger.warning(f"find_member get_chat @{username}: {e}")
    return None

def link(user):
    name = getattr(user,"full_name",None) or getattr(user,"first_name",None) or str(user.id)
    return f'<a href="tg://user?id={user.id}">{name}</a>'

def link_by_id(uid, name):
    return f'<a href="tg://user?id={uid}">{name}</a>'

def dur_display(td):
    s = int(td.total_seconds())
    if s < 3600:   return f"{s//60} мин."
    if s < 86400:  return f"{s//3600} ч."
    d = td.days
    if d % 7 == 0: return f"{d//7} нед."
    return f"{d} д."

def parse_dur(text):
    t = text.strip().lower()
    pats = [
        (r"(\d+)\s*(мин|минут\w*)",   "minutes"),
        (r"(\d+)\s*(час\w*|ч\.?)",    "hours"),
        (r"(\d+)\s*(ден\w*|дн\w*|д\.?)","days"),
        (r"(\d+)\s*(недел\w*|нед\.?)","weeks"),
    ]
    for p,u in pats:
        m = re.search(p,t)
        if m: return timedelta(**{u:int(m.group(1))})
    m = re.match(r"^(\d+)$",t)
    if m: return timedelta(days=int(m.group(1)))
    return None

MUTE_OFF = ChatPermissions(can_send_messages=False)
MUTE_ON  = ChatPermissions(can_send_messages=True,can_send_polls=True,
                            can_send_other_messages=True,can_add_web_page_previews=True,
                            can_invite_users=True)

# ── Работа с варнами ──────────────────────────────────────────────────────────
def get_active_warns(db, uid):
    """Возвращает количество активных варнов (с учётом 7-дневного срока)."""
    uid = str(uid)
    warn_info = db.get("warns", {}).get(uid)
    if not warn_info:
        return 0
    if isinstance(warn_info, int):
        # Старый формат — конвертируем
        db["warns"][uid] = {"count": warn_info, "issued_at": datetime.now(timezone.utc).isoformat()}
        save_db(db)
        return warn_info
    issued_at_str = warn_info.get("issued_at")
    if issued_at_str:
        issued_at = datetime.fromisoformat(issued_at_str)
        if datetime.now(timezone.utc) - issued_at > timedelta(days=WARN_EXPIRE_DAYS):
            db["warns"][uid] = {"count": 0, "issued_at": None}
            save_db(db)
            return 0
    return warn_info.get("count", 0)

# ── Шаблоны сообщений ─────────────────────────────────────────────────────────
def msg_mute(tgt, mod, disp, reason):
    return (f"╔══════════════════╗\n"
            f"       🔇 <b>МУТ</b>\n"
            f"╚══════════════════╝\n\n"
            f"👤 <b>Пользователь:</b> {link(tgt)}\n"
            f"⏱ <b>Срок:</b> {disp}\n"
            f"💬 <b>Причина:</b> {reason}\n"
            f"👮 <b>Модератор:</b> {link(mod)}")

def msg_warn(tgt, mod, count, reason, auto_ban=False):
    bar = "🟥"*count + "⬜"*(3-count)
    ex  = "\n\n🚫 <b>3/3 — автобан на 10 дней!</b>" if auto_ban else ""
    return (f"╔══════════════════╗\n"
            f"    ⚠️ <b>ПРЕДУПРЕЖДЕНИЕ</b>\n"
            f"╚══════════════════╝\n\n"
            f"👤 <b>Пользователь:</b> {link(tgt)}\n"
            f"📊 <b>Варны:</b> {bar} ({count}/3)\n"
            f"💬 <b>Причина:</b> {reason}\n"
            f"⏳ <b>Варн действует:</b> {WARN_EXPIRE_DAYS} дней\n"
            f"👮 <b>Модератор:</b> {link(mod)}{ex}")

def msg_ban(tgt, mod, disp, reason):
    hdr = f"🔴 <b>БАН · {disp}</b>" if disp else "🔴 <b>БАН НАВСЕГДА</b>"
    return (f"╔══════════════════╗\n"
            f"     {hdr}\n"
            f"╚══════════════════╝\n\n"
            f"👤 <b>Пользователь:</b> {link(tgt)}\n"
            f"💬 <b>Причина:</b> {reason}\n"
            f"👮 <b>Модератор:</b> {link(mod)}")

def msg_promote(tgt, rank):
    return (f"╔══════════════════╗\n"
            f"   {RANK_ICON[rank]} <b>НАЗНАЧЕНИЕ</b>\n"
            f"╚══════════════════╝\n\n"
            f"✅ {link(tgt)} назначен(а)\n"
            f"{RANK_STARS[rank]} <b>{RANK_TITLE[rank]}</b>")

def msg_demote(tgt, rank):
    if rank == 0: return f"🔻 {link(tgt)} лишён(а) всех прав"
    return (f"🔻 {link(tgt)} понижен(а) до\n"
            f"{RANK_STARS[rank]} <b>{RANK_TITLE[rank]}</b>")

# ── Группа: входящие сообщения ────────────────────────────────────────────────
async def on_group_msg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if not chat or chat.type not in ("group","supergroup"): return
    if chat.id != ALLOWED_CHAT: return
    db = load_db()
    if db.get("chat_id") != chat.id:
        logger.info(f"Chat registered/updated: {chat.id}")
        db["chat_id"] = chat.id
    u = update.effective_user
    if u and not u.is_bot:
        db.setdefault("members",{})[str(u.id)] = {"username":u.username,"name":u.full_name}
        # Счётчик сообщений за сегодня
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        db.setdefault("msg_stats", {}).setdefault(today, {})
        uid_str = str(u.id)
        db["msg_stats"][today][uid_str] = db["msg_stats"][today].get(uid_str, 0) + 1
    if str(CREATOR_ID) not in db["admins"]:
        db["admins"][str(CREATOR_ID)] = {"rank":5,"username":None,"name":"Создатель"}
    save_db(db)

    txt = (update.message.text or "").strip().lower() if update.message else ""

    if txt in ("кто админ","кто адм","кто администратор","список админов","adminlist"):
        await show_admins(update, db)
    elif txt == "варны":
        await show_my_warns(update, db)
    elif txt in ("банлист","banlist"):
        await show_banlist(update, db)
    elif txt in ("мутлист","mutelist"):
        await show_mutelist(update, db)
    elif txt in ("стата","статистика","stats"):
        await show_stats(update, db)

async def show_admins(update, db):
    if not db["admins"]:
        await update.message.reply_text("Список администраторов пуст."); return
    by_rank = {}
    for uid,info in db["admins"].items():
        by_rank.setdefault(info.get("rank",0),[]).append(info)
    lines = ["<b>👥 Администрация чата</b>\n"]
    for r in sorted(by_rank.keys(),reverse=True):
        lines.append(f"{RANK_STARS[r]} <b>{RANK_TITLE[r]}</b>")
        for info in by_rank[r]:
            lines.append(f"  {RANK_ICON[r]} {info.get('name','—')}")
        lines.append("")
    await update.message.reply_html("\n".join(lines))

async def show_my_warns(update, db):
    """Пользователь написал 'варны' — показываем его предупреждения."""
    u = update.effective_user
    count = get_active_warns(db, u.id)
    bar = "🟥" * count + "⬜" * (3 - count)
    if count == 0:
        text = (f"╔══════════════════╗\n"
                f"    📊 <b>ВАШИ ВАРНЫ</b>\n"
                f"╚══════════════════╝\n\n"
                f"👤 {link(u)}\n\n"
                f"✅ Активных предупреждений нет!")
    else:
        warn_info = db.get("warns", {}).get(str(u.id), {})
        expire_text = ""
        if isinstance(warn_info, dict) and warn_info.get("issued_at"):
            issued_at = datetime.fromisoformat(warn_info["issued_at"])
            expires_at = issued_at + timedelta(days=WARN_EXPIRE_DAYS)
            remaining = expires_at - datetime.now(timezone.utc)
            rem_days = max(0, remaining.days)
            expire_text = f"\n⏳ <b>Истекает через:</b> {rem_days} д."
        text = (f"╔══════════════════╗\n"
                f"    📊 <b>ВАШИ ВАРНЫ</b>\n"
                f"╚══════════════════╝\n\n"
                f"👤 {link(u)}\n"
                f"📊 <b>Варны:</b> {bar} ({count}/3){expire_text}\n\n"
                f"⚠️ При 3 варнах — автобан на 10 дней!")
    await update.message.reply_html(text)

async def show_banlist(update, db):
    """Показать всех забаненных пользователей."""
    bans = db.get("bans", {})
    now = datetime.now(timezone.utc)
    active = []
    for uid, info in bans.items():
        until_str = info.get("until")
        if until_str is None:
            active.append((uid, info, None))
        else:
            until = datetime.fromisoformat(until_str)
            if until > now:
                active.append((uid, info, until))
    if not active:
        await update.message.reply_html("📋 <b>Банлист пуст.</b>\nЗабаненных пользователей нет."); return
    lines = [f"╔══════════════════╗\n"
             f"    🔴 <b>БАНЛИСТ</b>\n"
             f"╚══════════════════╝\n"
             f"👥 Забанено: <b>{len(active)}</b>\n"]
    for uid, info, until in active:
        name = info.get("name", f"ID:{uid}")
        uname = info.get("username")
        uname_str = f" (@{uname})" if uname else ""
        until_disp = "навсегда" if until is None else f"ещё {dur_display(until - now)}"
        lines.append(f"• {link_by_id(int(uid), name)}{uname_str} — {until_disp}")
    await update.message.reply_html("\n".join(lines))

async def show_mutelist(update, db):
    """Показать всех замученных пользователей."""
    mutes = db.get("mutes", {})
    now = datetime.now(timezone.utc)
    active = []
    for uid, info in mutes.items():
        until_str = info.get("until")
        if until_str:
            until = datetime.fromisoformat(until_str)
            if until > now:
                active.append((uid, info, until))
    if not active:
        await update.message.reply_html("📋 <b>Мутлист пуст.</b>\nЗамученных пользователей нет."); return
    lines = [f"╔══════════════════╗\n"
             f"    🔇 <b>МУТЛИСТ</b>\n"
             f"╚══════════════════╝\n"
             f"👥 Замучено: <b>{len(active)}</b>\n"]
    for uid, info, until in active:
        name = info.get("name", f"ID:{uid}")
        uname = info.get("username")
        uname_str = f" (@{uname})" if uname else ""
        lines.append(f"• {link_by_id(int(uid), name)}{uname_str} — ещё {dur_display(until - now)}")
    await update.message.reply_html("\n".join(lines))

async def show_stats(update, db):
    """Статистика сообщений за сегодня."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    stats = db.get("msg_stats", {}).get(today, {})
    if not stats:
        await update.message.reply_html(
            f"╔══════════════════╗\n"
            f"    📈 <b>СТАТИСТИКА</b>\n"
            f"╚══════════════════╝\n\n"
            f"📅 <b>Дата:</b> {today}\n\n"
            f"Сегодня ещё никто не написал."); return
    sorted_stats = sorted(stats.items(), key=lambda x: x[1], reverse=True)
    total = sum(v for _, v in sorted_stats)
    lines = [f"╔══════════════════╗\n"
             f"    📈 <b>СТАТИСТИКА</b>\n"
             f"╚══════════════════╝\n"
             f"📅 <b>Дата:</b> {today}\n"
             f"💬 <b>Всего сообщений:</b> {total}\n"]
    members = db.get("members", {})
    for i, (uid, count) in enumerate(sorted_stats, 1):
        info = members.get(uid, {})
        name = info.get("name") or f"ID:{uid}"
        medal = {1:"🥇", 2:"🥈", 3:"🥉"}.get(i, f"{i}.")
        lines.append(f"{medal} {link_by_id(int(uid), name)} — <b>{count}</b> сообщ.")
    await update.message.reply_html("\n".join(lines))

# ── Джоб: проверка истёкших мутов ────────────────────────────────────────────
async def check_expired_mutes(context: ContextTypes.DEFAULT_TYPE):
    """Каждую минуту проверяет истёкшие муты и уведомляет в чате."""
    db = load_db()
    chat_id = db.get("chat_id")
    if not chat_id:
        return
    now = datetime.now(timezone.utc)
    mutes = db.get("mutes", {})
    to_remove = []
    for uid, info in mutes.items():
        until_str = info.get("until")
        if until_str:
            until = datetime.fromisoformat(until_str)
            if until <= now:
                to_remove.append(uid)
                name = info.get("name", f"ID:{uid}")
                try:
                    await context.bot.send_message(
                        chat_id=chat_id,
                        text=f'🔊 {link_by_id(int(uid), name)}, ваш мут истёк. В следующий раз следите за языком!',
                        parse_mode="HTML"
                    )
                except TelegramError as e:
                    logger.warning(f"check_expired_mutes notify: {e}")
    for uid in to_remove:
        del mutes[uid]
    if to_remove:
        save_db(db)

PENDING = "pending"

# ── /start ────────────────────────────────────────────────────────────────────
async def on_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private": return
    db   = load_db()
    user = update.effective_user
    rank = get_rank(db, user.id)
    if rank == 0:
        await update.message.reply_text("❌ У вас нет прав администратора.")
        return
    await send_help(update)


# ── Личные сообщения ──────────────────────────────────────────────────────────
async def on_private_msg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private": return
    db   = load_db()
    user = update.effective_user
    rank = get_rank(db, user.id)
    if rank == 0:
        await update.message.reply_text("❌ У вас нет прав администратора."); return
    # chat_id всегда задан из константы ALLOWED_CHAT

    raw   = update.message.text.strip()
    lines = [l.strip() for l in raw.splitlines() if l.strip()]

    # Ждём @username для отложенных команд
    if PENDING in context.user_data and raw.startswith("@"):
        p = context.user_data.pop(PENDING)
        un = raw.lstrip("@")
        if p["type"] == "mute":      await do_mute(update,context,db,user,rank,un,p["dur"],p["reason"])
        elif p["type"] == "warn":    await do_warn(update,context,db,user,rank,un,p["reason"])
        elif p["type"] == "ban":     await do_ban(update,context,db,user,rank,un,p.get("dur"),p["reason"])
        elif p["type"] == "promote": await do_promote(update,context,db,user,rank,un,p["tr"])
        elif p["type"] == "demote":  await do_demote(update,context,db,user,rank,un,p["tr"])
        return

    # ── СНЯТЬ МУТ / БАН / ВАРН ──────────────────────────────────────────────
    m = re.match(r"^снять\s+мут[,\s]+@?([A-Za-z0-9_]+)$", raw, re.I)
    if m:
        await do_unmute(update, context, db, user, rank, m.group(1)); return

    m = re.match(r"^снять\s+бан[,\s]+@?([A-Za-z0-9_]+)$", raw, re.I)
    if m:
        await do_unban(update, context, db, user, rank, m.group(1)); return

    m = re.match(r"^снять\s+варн[,\s]+@?([A-Za-z0-9_]+)$", raw, re.I)
    if m:
        await do_unwarn(update, context, db, user, rank, m.group(1)); return

    # ── СНЯТЬ (должность) ────────────────────────────────────────────────────
    m = re.match(r"^снять\s+@?([A-Za-z0-9_]+)$", raw, re.I)
    if m:
        await do_fire(update, context, db, user, rank, m.group(1)); return

    # ── ПОВЫСИТЬ / ПОНИЗИТЬ ──────────────────────────────────────────────────
    m = re.match(r"^повысить\s+(\d)\s+@?(\S+)$", raw, re.I)
    if m:
        await do_promote(update,context,db,user,rank,m.group(2),int(m.group(1))); return

    m = re.match(r"^понизить\s+(\d)\s+@?(\S+)$", raw, re.I)
    if m:
        await do_demote(update,context,db,user,rank,m.group(2),int(m.group(1))); return

    # ── ФОРМЫ ────────────────────────────────────────────────────────────────
    if raw.lower() == "формы" and rank >= 4:
        await show_forms(update, context, db); return

    m = re.match(r"^/ban\s+(\d+)(?:\s*дн[её]й?|\s*д\.?)?\s*[,\s]+@?([A-Za-z0-9_]+)[,\s]+by\s+@?([A-Za-z0-9_]+)", raw, re.I)
    if m:
        if rank >= 4:
            await update.message.reply_html(
                "ℹ️ У вас достаточно прав — используйте прямой бан:\n"
                "<code>Бан N дней\n@username\nПричина</code>"
            )
            return
        tgt_un = m.group(2).strip("@,. ")
        sub_un = m.group(3).strip("@,. ")
        await do_submit(update, context, db, user, int(m.group(1)), tgt_un, sub_un)
        return

    if len(lines) >= 1:
        action = lines[0].lower().split("@")[0].strip()
        tgt, reason = _tr(lines)

        mm = re.match(r"^мут\s+(.+)$", action)
        if mm:
            dur_s = mm.group(1).split("@")[0].strip()
            if rank < 1: await update.message.reply_text("❌ Нужен Модератор+."); return
            if tgt: await do_mute(update,context,db,user,rank,tgt,dur_s,reason)
            else:
                context.user_data[PENDING]={"type":"mute","dur":dur_s,"reason":reason}
                await update.message.reply_html("✏️ Укажите @username:")
            return

        if action in ("варн","warn"):
            if rank < 2: await update.message.reply_text("❌ Нужен Старший модератор+."); return
            if tgt: await do_warn(update,context,db,user,rank,tgt,reason)
            else:
                context.user_data[PENDING]={"type":"warn","reason":reason}
                await update.message.reply_text("✏️ Укажите @username:")
            return

        bm = re.match(r"^бан\s+(.+)$", action)
        if bm:
            dur_s = bm.group(1).split("@")[0].strip()
            if rank < 3: await update.message.reply_text("❌ Нужен Администратор+."); return
            if tgt: await do_ban(update,context,db,user,rank,tgt,dur_s,reason)
            else:
                context.user_data[PENDING]={"type":"ban","dur":dur_s,"reason":reason}
                await update.message.reply_html("✏️ Укажите @username:")
            return

        if action == "бан":
            if rank < 3: await update.message.reply_text("❌ Нужен Администратор+."); return
            if tgt: await do_ban(update,context,db,user,rank,tgt,None,reason)
            else:
                context.user_data[PENDING]={"type":"ban","dur":None,"reason":reason}
                await update.message.reply_text("✏️ Укажите @username:")
            return

    await send_help(update)

def _tr(lines):
    tgt = None
    reason = "Не указана"
    all_tokens = []
    for l in lines:
        all_tokens.extend(l.split())
    uname_idx = None
    for i, tok in enumerate(all_tokens):
        if tok.startswith("@"):
            tgt = tok.lstrip("@")
            uname_idx = i
            break
    if tgt and uname_idx is not None:
        rest = all_tokens[uname_idx+1:]
        if rest:
            reason = " ".join(rest)
    return tgt, reason

async def send_help(update):
    await update.message.reply_html(
        "╔═══════════════════════╗\n"
        "║  🛡️  <b>INTERFACTS MANAGER</b>  🛡️  ║\n"
        "╚═══════════════════════╝\n\n"
        "━━━━━ 🔇 <b>МУТ</b> ━━━━━\n"
        "Временно заглушить пользователя\n"
        "<code>Мут 24 часа\n@username\nПричина</code>\n"
        "⏱ Можно: <i>30 минут · 2 часа · 1 день</i>\n\n"
        "━━━━━ ⚠️ <b>ВАРН</b> ━━━━━\n"
        "Предупреждение (3 варна → 🔨 автобан 10 дней)\n"
        "Варн сгорает через 7 дней\n"
        "<code>Варн\n@username\nПричина</code>\n"
        "<i>или в одну строку:</i>\n"
        "<code>Варн @username\nПричина</code>\n\n"
        "━━━━━ 🔴 <b>БАН</b> ━━━━━\n"
        "На срок:\n"
        "<code>Бан 5 дней\n@username\nПричина</code>\n"
        "Навсегда:\n"
        "<code>Бан\n@username\nПричина</code>\n\n"
        "━━━━━ ✅ <b>СНЯТЬ НАКАЗАНИЕ</b> ━━━━━\n"
        "🔊 <code>Снять мут, @username</code>\n"
        "✅ <code>Снять бан, @username</code>\n"
        "🗑 <code>Снять варн, @username</code>\n\n"
        "━━━━━ 👥 <b>УПРАВЛЕНИЕ АДМИНАМИ</b> ━━━━━\n"
        "⬆️ <code>Повысить 4 @username</code>\n"
        "⬇️ <code>Понизить 1 @username</code>\n"
        "🚫 <code>Снять @username</code>\n\n"
        "━━━━━ 📋 <b>ФОРМА НА БАН</b> ━━━━━\n"
        "Для модераторов без прав бана:\n"
        "<code>/ban 10 дней, @user, by @вы</code>\n\n"
        "━━━━━ 💬 <b>КОМАНДЫ В ЧАТЕ</b> ━━━━━\n"
        "📊 <code>варны</code> — мои предупреждения\n"
        "🔴 <code>банлист</code> — список забаненных\n"
        "🔇 <code>мутлист</code> — список замученных\n"
        "📈 <code>стата</code> — активность за сегодня\n\n"
        "━━━━━ 🏅 <b>РАНГИ</b> ━━━━━\n"
        "1️⃣ Модератор\n"
        "2️⃣ Старший модератор\n"
        "3️⃣ Администратор\n"
        "4️⃣ Старший администратор\n"
        "👑 Создатель"
    )

# ── Действия: мут ─────────────────────────────────────────────────────────────
async def do_mute(update,context,db,mod,rank,username,dur_str,reason):
    td = parse_dur(dur_str)
    if not td:
        await update.message.reply_html(f"❌ Не понял срок: <b>{dur_str}</b>\nПример: <code>30 минут · 2 часа · 1 день</code>"); return
    chat_id = db.get("chat_id")
    tgt = await find_member(context,db,username)
    if not tgt:
        await update.message.reply_text(f"❌ @{username} не найден.\nПользователь должен написать в чате хотя бы раз."); return
    until = datetime.now(timezone.utc)+td
    try:
        await context.bot.restrict_chat_member(chat_id=chat_id,user_id=tgt.id,permissions=MUTE_OFF,until_date=until)
    except TelegramError as e:
        await update.message.reply_text(f"❌ Telegram: {e}"); return
    disp = dur_display(td)
    # Сохраняем в мутлист
    db.setdefault("mutes", {})[str(tgt.id)] = {
        "until": until.isoformat(),
        "name": getattr(tgt,"full_name",None) or getattr(tgt,"first_name",None) or str(tgt.id),
        "username": getattr(tgt,"username",None)
    }
    save_db(db)
    await context.bot.send_message(chat_id=chat_id,text=msg_mute(tgt,mod,disp,reason),parse_mode="HTML")
    await update.message.reply_text(f"✅ Мут на {disp} выдан.")

# ── Действия: варн ────────────────────────────────────────────────────────────
async def do_warn(update,context,db,mod,rank,username,reason):
    chat_id = db.get("chat_id")
    tgt = await find_member(context,db,username)
    if not tgt:
        await update.message.reply_text(f"❌ @{username} не найден.\nПользователь должен написать в чате хотя бы раз."); return
    uid = str(tgt.id)
    current = get_active_warns(db, uid)
    new_count = current + 1
    db.setdefault("warns", {})[uid] = {"count": new_count, "issued_at": datetime.now(timezone.utc).isoformat()}
    save_db(db)
    ab = new_count >= 3
    await context.bot.send_message(chat_id=chat_id,text=msg_warn(tgt,mod,new_count,reason,ab),parse_mode="HTML")
    if ab:
        until = datetime.now(timezone.utc)+timedelta(days=10)
        try:
            await context.bot.ban_chat_member(chat_id=chat_id,user_id=tgt.id,until_date=until)
            db.setdefault("bans", {})[uid] = {
                "until": until.isoformat(),
                "name": getattr(tgt,"full_name",None) or str(tgt.id),
                "username": getattr(tgt,"username",None)
            }
        except TelegramError as e:
            await update.message.reply_text(f"❌ Не удалось забанить: {e}"); return
        db["warns"][uid] = {"count": 0, "issued_at": None}
        save_db(db)
        await update.message.reply_text("✅ 3 варна — автобан на 10 дней.")
    else:
        await update.message.reply_text(f"✅ Варн {new_count}/3 выдан. Действует {WARN_EXPIRE_DAYS} дней.")

# ── Действия: бан ─────────────────────────────────────────────────────────────
async def do_ban(update,context,db,mod,rank,username,dur_str,reason):
    chat_id = db.get("chat_id")
    tgt = await find_member(context,db,username)
    if not tgt:
        await update.message.reply_text(f"❌ @{username} не найден.\nПользователь должен написать в чате хотя бы раз."); return
    uid = str(tgt.id)
    tgt_name = getattr(tgt,"full_name",None) or str(tgt.id)
    tgt_uname = getattr(tgt,"username",None)
    if dur_str:
        td = parse_dur(dur_str)
        if not td:
            await update.message.reply_html(f"❌ Не понял срок: <b>{dur_str}</b>"); return
        until = datetime.now(timezone.utc)+td; disp = dur_display(td)
        try: await context.bot.ban_chat_member(chat_id=chat_id,user_id=tgt.id,until_date=until)
        except TelegramError as e:
            await update.message.reply_text(f"❌ Telegram: {e}"); return
        db.setdefault("bans", {})[uid] = {"until": until.isoformat(), "name": tgt_name, "username": tgt_uname}
    else:
        disp = None
        try: await context.bot.ban_chat_member(chat_id=chat_id,user_id=tgt.id)
        except TelegramError as e:
            await update.message.reply_text(f"❌ Telegram: {e}"); return
        db.setdefault("bans", {})[uid] = {"until": None, "name": tgt_name, "username": tgt_uname}
    save_db(db)
    await context.bot.send_message(chat_id=chat_id,text=msg_ban(tgt,mod,disp,reason),parse_mode="HTML")
    await update.message.reply_text(f"✅ Бан {'на '+disp if disp else 'навсегда'} выдан.")

# ── Действия: снять мут / бан / варн ─────────────────────────────────────────
async def do_unmute(update, context, db, mod, rank, username):
    if rank < 1:
        await update.message.reply_text("❌ Нужен Модератор+."); return
    chat_id = db.get("chat_id")
    tgt = await find_member(context, db, username)
    if not tgt:
        await update.message.reply_text(f"❌ @{username} не найден."); return
    try:
        await context.bot.restrict_chat_member(chat_id=chat_id, user_id=tgt.id, permissions=MUTE_ON)
    except TelegramError as e:
        await update.message.reply_text(f"❌ Telegram: {e}"); return
    db.setdefault("mutes", {}).pop(str(tgt.id), None)
    save_db(db)
    await context.bot.send_message(
        chat_id=chat_id,
        text=f"🔊 {link(tgt)}, мут снят. Добро пожаловать обратно!",
        parse_mode="HTML"
    )
    await update.message.reply_text(f"✅ Мут с @{username} снят.")

async def do_unban(update, context, db, mod, rank, username):
    if rank < 3:
        await update.message.reply_text("❌ Нужен Администратор+."); return
    chat_id = db.get("chat_id")
    tgt = await find_member(context, db, username)
    if not tgt:
        await update.message.reply_text(f"❌ @{username} не найден."); return
    try:
        await context.bot.unban_chat_member(chat_id=chat_id, user_id=tgt.id, only_if_banned=True)
    except TelegramError as e:
        await update.message.reply_text(f"❌ Telegram: {e}"); return
    db.setdefault("bans", {}).pop(str(tgt.id), None)
    save_db(db)
    await context.bot.send_message(
        chat_id=chat_id,
        text=f"✅ {link(tgt)} разбанен(а).",
        parse_mode="HTML"
    )
    await update.message.reply_text(f"✅ Бан с @{username} снят.")

async def do_unwarn(update, context, db, mod, rank, username):
    if rank < 2:
        await update.message.reply_text("❌ Нужен Старший модератор+."); return
    chat_id = db.get("chat_id")
    tgt = await find_member(context, db, username)
    if not tgt:
        await update.message.reply_text(f"❌ @{username} не найден."); return
    uid = str(tgt.id)
    db.setdefault("warns", {})[uid] = {"count": 0, "issued_at": None}
    save_db(db)
    await context.bot.send_message(
        chat_id=chat_id,
        text=f"🗑 {link(tgt)}, все предупреждения сняты.",
        parse_mode="HTML"
    )
    await update.message.reply_text(f"✅ Варны с @{username} сняты.")

# ── Действия: повысить / понизить / уволить ───────────────────────────────────
async def do_promote(update,context,db,mod,mod_rank,username,tr):
    if not can_promote(mod_rank,tr):
        await update.message.reply_text(f"❌ Нельзя назначить ранг {tr}. Ваш ранг: {mod_rank}."); return
    chat_id = db.get("chat_id")
    tgt = await find_member(context,db,username)
    if not tgt:
        await update.message.reply_text(f"❌ @{username} не найден.\nПользователь должен написать в чате хотя бы раз."); return
    uid = str(tgt.id)
    name = getattr(tgt,"full_name",None) or getattr(tgt,"first_name",None) or str(uid)
    db["admins"][uid]={"rank":tr,"username":getattr(tgt,"username",None),"name":name}
    save_db(db)
    try:
        await context.bot.promote_chat_member(
            chat_id=chat_id,user_id=tgt.id,
            can_delete_messages=True,can_restrict_members=True,
            can_pin_messages=(tr>=3),can_manage_chat=(tr>=4),can_promote_members=(tr>=4))
        await context.bot.set_chat_administrator_custom_title(chat_id=chat_id,user_id=tgt.id,custom_title=RANK_TITLE[tr])
    except TelegramError as e:
        logger.warning(f"promote TG: {e}")
    await context.bot.send_message(chat_id=chat_id,text=msg_promote(tgt,tr),parse_mode="HTML")
    await update.message.reply_text(f"✅ {RANK_TITLE[tr]} назначен(а).")

async def do_demote(update,context,db,mod,mod_rank,username,tr):
    if mod_rank < 4:
        await update.message.reply_text("❌ Недостаточно прав."); return
    chat_id = db.get("chat_id")
    tgt = await find_member(context,db,username)
    if not tgt:
        await update.message.reply_text(f"❌ @{username} не найден."); return
    uid = str(tgt.id)
    name = getattr(tgt,"full_name",None) or getattr(tgt,"first_name",None) or str(uid)
    if tr == 0: db["admins"].pop(uid,None)
    else: db["admins"][uid]={"rank":tr,"username":getattr(tgt,"username",None),"name":name}
    save_db(db)
    try:
        await context.bot.promote_chat_member(
            chat_id=chat_id,user_id=tgt.id,
            can_delete_messages=(tr>0),can_restrict_members=(tr>0),
            can_pin_messages=(tr>=3),can_manage_chat=False,can_promote_members=False)
    except TelegramError as e:
        logger.warning(f"demote TG: {e}")
    await context.bot.send_message(chat_id=chat_id,text=msg_demote(tgt,tr),parse_mode="HTML")
    await update.message.reply_text("✅ Готово.")

def msg_fire(tgt, mod):
    return (f"╔══════════════════╗\n"
            f"    🚫 <b>СНЯТИЕ С ДОЛЖНОСТИ</b>\n"
            f"╚══════════════════╝\n\n"
            f"👤 <b>Пользователь:</b> {link(tgt)}\n"
            f"📋 <b>Статус:</b> Лишён всех прав администратора\n"
            f"👮 <b>Снял:</b> {link(mod)}")

async def do_fire(update, context, db, mod, mod_rank, username):
    if mod_rank < 3:
        await update.message.reply_text("❌ Недостаточно прав. Нужен Администратор+."); return
    chat_id = db.get("chat_id")
    username = username.strip("@,. ")
    tgt = await find_member(context, db, username)
    if not tgt:
        await update.message.reply_text(f"❌ @{username} не найден."); return
    uid = str(tgt.id)
    tgt_rank = get_rank(db, tgt.id)
    if tgt.id == mod.id:
        await update.message.reply_text("❌ Нельзя снять самого себя."); return
    if tgt_rank >= mod_rank:
        await update.message.reply_text(
            f"❌ Нельзя снять {RANK_TITLE.get(tgt_rank, 'этого пользователя')} — ранг не ниже вашего."
        ); return
    if tgt.id == CREATOR_ID:
        await update.message.reply_text("❌ Нельзя снять создателя."); return
    db["admins"].pop(uid, None)
    save_db(db)
    try:
        await context.bot.promote_chat_member(
            chat_id=chat_id, user_id=tgt.id,
            can_delete_messages=False, can_restrict_members=False,
            can_pin_messages=False, can_manage_chat=False, can_promote_members=False
        )
    except TelegramError as e:
        logger.warning(f"fire promote TG: {e}")
    await context.bot.send_message(chat_id=chat_id, text=msg_fire(tgt, mod), parse_mode="HTML")
    await update.message.reply_text(f"✅ @{username} снят(а) с должности.")

# ── Формы ─────────────────────────────────────────────────────────────────────
def make_form_keyboard(form_id):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Принять",   callback_data=f"form_accept_{form_id}"),
        InlineKeyboardButton("❌ Отклонить", callback_data=f"form_reject_{form_id}"),
    ]])

async def do_submit(update, context, db, user, days, tgt_un, sub_un):
    tgt_un = tgt_un.strip("@,. ")
    sub_un = sub_un.strip("@,. ")
    form = {
        "id": len(db.get("ban_forms", [])) + 1,
        "days": days,
        "target_username": tgt_un,
        "submitter_id": user.id,
        "submitter_name": user.full_name,
        "submitter_username": sub_un,
        "status": "pending"
    }
    db.setdefault("ban_forms", []).append(form)
    save_db(db)
    await update.message.reply_html(
        f"📋 <b>Форма #{form['id']} отправлена!</b>\n\n"
        f"👤 Цель: @{tgt_un}\n"
        f"📅 Срок: {days} дней\n\n"
        f"⏳ Ожидайте решения старшего администратора."
    )
    notify = (
        f"📋 <b>Новая форма на бан #{form['id']}</b>\n\n"
        f"👤 <b>Цель:</b> @{tgt_un}\n"
        f"📅 <b>Срок:</b> {days} дней\n"
        f"👮 <b>Подал:</b> {user.full_name} (@{sub_un})\n\n"
        f"Выберите действие:"
    )
    keyboard = make_form_keyboard(form["id"])
    seniors = {CREATOR_ID} | {int(uid) for uid, info in db["admins"].items() if info.get("rank", 0) >= 4}
    for sid in seniors:
        try:
            await context.bot.send_message(sid, notify, parse_mode="HTML", reply_markup=keyboard)
        except TelegramError as e:
            logger.warning(f"Не удалось отправить форму {sid}: {e}")

async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    db   = load_db()
    user = query.from_user
    rank = get_rank(db, user.id)
    if rank < 4:
        await query.answer("❌ У вас нет прав для этого действия.", show_alert=True)
        return
    m = re.match(r"^form_(accept|reject)_(\d+)$", query.data)
    if not m:
        await query.answer()
        return
    action = m.group(1)
    fid    = int(m.group(2))
    form = next((f for f in db.get("ban_forms", []) if f["id"] == fid and f["status"] == "pending"), None)
    if not form:
        await query.answer("⚠️ Форма уже обработана.", show_alert=True)
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except TelegramError:
            pass
        return
    if action == "accept":
        await _accept_form(query, context, db, user, form)
    else:
        await _reject_form(query, context, db, user, form)

async def _accept_form(query, context, db, mod, form):
    fid             = form["id"]
    chat_id         = db.get("chat_id")
    target_username = form["target_username"].lstrip("@")
    until = datetime.now(timezone.utc) + timedelta(days=form["days"])
    user_id   = None
    full_name = f"@{target_username}"
    for uid, info in db.get("members", {}).items():
        if (info.get("username") or "").lower() == target_username.lower():
            user_id   = int(uid)
            full_name = info.get("name", full_name)
            break
    if not user_id:
        try:
            chat = await context.bot.get_chat(f"@{target_username}")
            user_id   = chat.id
            full_name = chat.full_name or full_name
            db.setdefault("members", {})[str(user_id)] = {"username": target_username, "name": full_name}
            save_db(db)
        except TelegramError as e:
            logger.warning(f"get_chat @{target_username}: {e}")
    if user_id:
        try:
            await context.bot.ban_chat_member(chat_id=chat_id, user_id=user_id, until_date=until)
            db.setdefault("bans", {})[str(user_id)] = {
                "until": until.isoformat(), "name": full_name, "username": target_username
            }
            save_db(db)
        except TelegramError as e:
            await query.answer(f"❌ Ошибка бана: {e}", show_alert=True)
            return
    else:
        await query.answer(
            f"❌ Не удалось найти @{target_username}. Аккаунт должен быть публичным или написать в чате.",
            show_alert=True
        )
        return
    form["status"] = "accepted"
    save_db(db)
    class _T: pass
    tgt_obj = _T()
    tgt_obj.id        = user_id
    tgt_obj.full_name = full_name
    tgt_obj.first_name = full_name
    reason = f"Форма #{fid}, by @{form['submitter_username']}"
    await context.bot.send_message(
        chat_id=chat_id,
        text=msg_ban(tgt_obj, mod, f"{form['days']} д.", reason),
        parse_mode="HTML"
    )
    await query.answer("✅ Бан выдан.")
    await query.edit_message_text(
        f"✅ <b>Форма #{fid} принята</b>\n\n"
        f"👤 Цель: @{target_username}\n"
        f"📅 Срок: {form['days']} дней\n"
        f"👮 Принял: {mod.full_name}",
        parse_mode="HTML"
    )
    try:
        await context.bot.send_message(
            form["submitter_id"],
            f"✅ <b>Форма #{fid} принята!</b>\n\n@{target_username} забанен на {form['days']} дней.",
            parse_mode="HTML"
        )
    except TelegramError:
        pass

async def _reject_form(query, context, db, mod, form):
    fid = form["id"]
    form["status"] = "rejected"
    save_db(db)
    await query.answer("❌ Форма отклонена.")
    await query.edit_message_text(
        f"❌ <b>Форма #{fid} отклонена</b>\n\n"
        f"👤 Цель: @{form['target_username']}\n"
        f"📅 Срок: {form['days']} дней\n"
        f"👮 Отклонил: {mod.full_name}",
        parse_mode="HTML"
    )
    try:
        await context.bot.send_message(
            form["submitter_id"],
            f"❌ <b>Форма #{fid} отклонена.</b>\n\nСтарший администратор отказал в бане @{form['target_username']}.",
            parse_mode="HTML"
        )
    except TelegramError:
        pass

async def show_forms(update, context, db):
    pend = [f for f in db.get("ban_forms", []) if f["status"] == "pending"]
    if not pend:
        await update.message.reply_text("📋 Нет ожидающих форм."); return
    await update.message.reply_html(f"📋 <b>Ожидающих форм: {len(pend)}</b>")
    for f in pend:
        notify = (
            f"📋 <b>Форма #{f['id']}</b>\n\n"
            f"👤 <b>Цель:</b> @{f['target_username']}\n"
            f"📅 <b>Срок:</b> {f['days']} дней\n"
            f"👮 <b>Подал:</b> @{f['submitter_username']}\n\n"
            f"Выберите действие:"
        )
        await update.message.reply_html(notify, reply_markup=make_form_keyboard(f["id"]))

async def on_my_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    result = update.my_chat_member
    if not result: return
    chat = result.chat
    new_status = result.new_chat_member.status
    if chat.type not in ("group","supergroup"): return
    if chat.id != ALLOWED_CHAT: return
    db = load_db()
    if new_status in ("member","administrator"):
        if db.get("chat_id") != chat.id:
            logger.info(f"Bot added/restored to chat: {chat.id} (was: {db.get('chat_id')})")
            db["chat_id"] = chat.id
            if str(CREATOR_ID) not in db["admins"]:
                db["admins"][str(CREATOR_ID)] = {"rank":5,"username":None,"name":"Создатель"}
            save_db(db)
    elif new_status in ("kicked","left"):
        logger.info(f"Bot kicked/left from chat: {chat.id}")

def main():
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .job_queue(JobQueue())
        .build()
    )
    app.add_handler(ChatMemberHandler(on_my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))
    app.add_handler(CommandHandler("start", on_start))
    app.add_handler(MessageHandler(filters.ChatType.GROUPS & filters.TEXT, on_group_msg))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.TEXT, on_private_msg))
    app.add_handler(CallbackQueryHandler(on_callback, pattern=r"^form_(accept|reject)_\d+$"))
    # Проверка истёкших мутов каждые 60 секунд
    app.job_queue.run_repeating(check_expired_mutes, interval=60, first=10)
    logger.info("🤖 Iris | Чат-менеджер запущен")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
