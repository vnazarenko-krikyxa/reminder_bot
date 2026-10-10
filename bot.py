"""Бот-нагадувалка: дні народження, свята, задачі, робочі листи й зустрічі.

Интерфейс — украинский; названия событий — как их сказал пользователь.
"""
import logging
import warnings
from datetime import date, datetime, timedelta

import httpx
from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardRemove, Update
from telegram.constants import ParseMode
from telegram.error import NetworkError
from telegram.ext import (
    ApplicationBuilder, CallbackQueryHandler, CommandHandler, ContextTypes,
    ConversationHandler, MessageHandler, filters,
)

import ai
import db
import google_api
import meetings
import ua_holidays
import work
from config import (
    ALLOWED_USER_IDS, DIGEST_HOUR, GROQ_API_KEY, MAX_VOICE_SECONDS, REMIND_AT, TELEGRAM_TOKEN, TICK_SECONDS, TZ,
    WORK_EMAIL_FROM, WORK_EMAIL_TO,
)
from dates import fmt_day_month, occurrence_date, parse_date, plural_days
from reminders import (
    OLD_SNOOZE, SNOOZE, after_morning, cap, decide, done_line, escape, icon, render, snooze_until, snoozed_line,
)

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("reminder_bot")
# Кнопки диалога /add всегда в последнем сообщении, отслеживать каждое сообщение не нужно
warnings.filterwarnings("ignore", message=r"If 'per_message=False'")

TITLE, DATE, REPEAT, NEEDS, NOTES = range(5)
WEEKDAYS_SHORT = ["пн", "вт", "ср", "чт", "пт", "сб", "нд"]
REC_LABEL = {"yearly": "щороку", "once": "один раз", "weekly": "щотижня", "monthly": "щомісяця",
             "holiday": "свято", "someday": "колись"}
MENU = {"🎊 Всі свята": "holidays", "🎂 Всі дні народження": "birthdays",
        "🌠 Всі бажання": "wishes", "📋 Всі справи": "tasks"}
# Меню команд у Telegram (кнопка «/» біля поля вводу)
COMMANDS = [
    ("list", "Усі заплановані події"),
    ("tasks", "Всі справи"),
    ("wishes", "Всі бажання"),
    ("birthdays", "Всі дні народження"),
    ("holidays", "Всі свята"),
    ("today", "Календар на сьогодні"),
    ("all", "Змінити чи видалити подію"),
    ("add", "Додати подію покроково"),
    ("cancel", "Скасувати введення"),
    ("help", "Що вміє бот"),
]
COMMANDS_VERSION = "cmds:v1"  # змінити, щоб бот оновив меню в Telegram
# Кнопки внизу чату прибрано — розділи в меню команд «/». Старі натискання ще обробляємо.
NO_KB = ReplyKeyboardRemove()

HELP = (
    "Я нагадую про дні народження, свята й справи — заздалегідь, і смикаю, поки не відмітиш.\n\n"
    "🎙 Надиктуй голосове або просто напиши:\n"
    "• «у мами ДР 1 жовтня, потрібен подарунок»\n"
    "• «нагадай сьогодні о 20:00 купити плитку», «через 2 години подзвонити в сервіс»\n"
    "• «щомісяця 5-го оплатити інтернет», «щопонеділка звіт»\n"
    "• «перенеси ДР мами на 2 жовтня», «видали концерт»\n\n"
    "🎁 Подарунки: «ідея подарунка мамі — масажер», «на 8 березня подарунки дружині й мамі», "
    "«що я знаю про маму».\n"
    "💼 Робота: «пауза з 14 до 16», «завтра почну з 11», «сьогодні працюю до 16», «відгул», «лікарняний» — "
    "лист команді + «Не на місці» в календарі.\n"
    "📅 Зустрічі: «созвон з Андрієм завтра о 15», «перенеси зустріч з Андрієм на 16:00», "
    "«що в календарі завтра».\n\n"
    "/list — усі заплановані події\n"
    "/tasks · /wishes · /birthdays · /holidays — справи, бажання, дні народження, свята\n"
    "/today — календар на сьогодні\n"
    "/all — змінити чи видалити подію\n"
    "/add — додати подію покроково\n"
    "/cancel — скасувати введення\n"
    "/help — ця довідка\n\n"
    "Нагадую о 10:30. Що треба зробити сьогодні — повторю ще раз через 2 год (до 3 разів), "
    "а о 20:00 надішлю підсумок невідміченого."
)


def now():
    return datetime.now(TZ)


def allowed(update: Update) -> bool:
    return not ALLOWED_USER_IDS or (update.effective_user and update.effective_user.id in ALLOWED_USER_IDS)


def when_text(n: dict) -> str:
    if n.get("recurrence") == "someday" or not n.get("day"):
        return "без дати"
    s = f"{n['day']:02d}.{n['month']:02d}" + (f".{n['year']}" if n.get("year") else "")
    return s + (f" в {n['time']}" if n.get("time") else "")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return
    db.seed_holidays(update.effective_chat.id, now().date())
    await update.message.reply_text(HELP, reply_markup=NO_KB)


# ---------- /add ----------

async def add_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return ConversationHandler.END
    context.user_data["new"] = {}
    await update.message.reply_text("Що за подія? Наприклад: «ДР мами», «Річниця весілля», «Оплатити інтернет».")
    return TITLE


async def add_title(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["new"]["title"] = update.message.text.strip()[:200]
    await update.message.reply_text("Дата? Формат: 14.03 або 14.03.1990 (з роком народження покажу вік).")
    return DATE


async def add_date(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        d, m, y = parse_date(update.message.text)
    except ValueError:
        await update.message.reply_text("Не зрозумів дату 🤔 Приклад: 14.03 або 14.03.1990")
        return DATE
    context.user_data["new"].update(day=d, month=m, year=y)
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔁 Щороку", callback_data="rep:yearly"),
         InlineKeyboardButton("1️⃣ Один раз", callback_data="rep:once")],
        [InlineKeyboardButton("📆 Щотижня", callback_data="rep:weekly"),
         InlineKeyboardButton("🗓 Щомісяця", callback_data="rep:monthly")],
    ])
    await update.message.reply_text("Повторювати?", reply_markup=kb)
    return REPEAT


async def add_repeat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    new = context.user_data["new"]
    new["recurrence"] = q.data.split(":")[1]
    if new["recurrence"] != "yearly":
        today = now().date()
        if new["year"] is None:
            # без року — найближча така дата
            d = occurrence_date(new["month"], new["day"], today.year)
            new["year"] = today.year if d >= today else today.year + 1
        elif new["recurrence"] == "once" and date(new["year"], new["month"], new["day"]) < today:
            await q.edit_message_text("Ця дата вже минула. Введи дату ще раз:")
            return DATE
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🎁 Подарунок + привітати", callback_data="need:gc")],
        [InlineKeyboardButton("💬 Тільки привітати", callback_data="need:c")],
        [InlineKeyboardButton("🔔 Просто нагадати", callback_data="need:-")],
    ])
    await q.edit_message_text("Що треба зробити?", reply_markup=kb)
    return NEEDS


async def add_needs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    flags = q.data.split(":")[1]
    new = context.user_data["new"]
    new["needs_gift"] = "g" in flags
    new["needs_congrats"] = "c" in flags
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("Пропустити", callback_data="notes:skip")]])
    await q.edit_message_text("Нотатка? Що любить, розмір, адреса — або «Пропустити».", reply_markup=kb)
    return NOTES


async def add_notes_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["new"]["notes"] = update.message.text.strip()[:1000]
    return await add_finish(update.message.reply_text, update, context)


async def add_notes_skip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    context.user_data["new"]["notes"] = None
    return await add_finish(q.edit_message_text, update, context)


async def add_finish(reply, update: Update, context: ContextTypes.DEFAULT_TYPE):
    n = context.user_data.pop("new")
    await save_event(reply, update, context, n)
    return ConversationHandler.END


async def save_event(reply, update: Update, context: ContextTypes.DEFAULT_TYPE, n: dict):
    chat_id = update.effective_chat.id
    pid = db.person_id(chat_id, n["person"]) if n.get("person") else None
    db.add_event(
        chat_id, n["title"], n["day"], n["month"], n["year"], n["recurrence"],
        n["needs_gift"], n["needs_congrats"], n.get("notes"), now().date(), time=n.get("time"),
        kind=n.get("kind"), person_id=pid,
    )
    await reply(f"✅ Додав: {cap(n['title'])} — {when_text(n)}, {REC_LABEL.get(n['recurrence'], '')}.")
    # раптом уже пора щось нагадати (наприклад, подія через тиждень)
    await send_due(context.bot)


# ---------- вільне введення: голос або текст ----------
# user_data["pending"] — дія, що чекає підтвердження:
#   {"action": "add"|"update"|"delete"|"absence"|"meeting"|…, ...}
# user_data["mode"] — чого чекаємо від наступного повідомлення:
#   "fix" — виправлення картки, ["target", id] — правка події з /all, ["move", ev] — перенесення зустрічі,
#   ["gift", occ_id, person_id] — відповідь на «Що подарував?», "partial_fix" — уточнення

def event_summary(n: dict) -> list[str]:
    todo = [x for x, on in (("🎁 подарунок", n["needs_gift"]), ("💬 привітати", n["needs_congrats"])) if on]
    what = " + ".join(todo) if todo else "🔔 просто нагадати"
    if n.get("kind") == "wish":
        what = "🌠 бажання" + (" — без нагадувань" if n["recurrence"] == "someday" else "")
    elif n["recurrence"] == "someday":
        what = "📋 справа без дати — нагадаю списком 1-го числа"
    lines = [f"<b>{escape(cap(n['title']))}</b> — {when_text(n)}, {REC_LABEL.get(n['recurrence'], '')}", what]
    if n.get("person"):
        lines.append(f"👤 {escape(n['person'])}")
    if n.get("notes"):
        lines.append(f"📝 {escape(n['notes'])}")
    return lines


def absence_card(a: dict) -> list[str]:
    c = work.compose(a)
    start, end = c["start"], c["end"]
    if a["kind"] in ("day_off", "sick"):
        period = f"{work.ua_range(start.date(), end.date())}, {start:%H:%M}–{end:%H:%M}"
    elif end.time() == datetime.min.time() and end.date() == start.date() + timedelta(days=1):
        period = f"{work.ua_date(start.date())}, {start:%H:%M}–24:00"
    else:
        period = f"{work.ua_date(start.date())}, {start:%H:%M}–{end:%H:%M}"
    return [
        f"<b>{work.KINDS[a['kind']]}</b>\n",
        f"✉️ <b>Кому:</b> {escape(WORK_EMAIL_TO)}",
        f"<b>Тема:</b> {escape(c['subject'])}",
        f"<i>{escape(c['body'])}</i>\n",
        f"📅 <b>Не на місці:</b> {period}",
    ]


def pending_card(p: dict, events: dict, heard: str | None = None):
    lines = [f"🎙 <i>{escape(heard)}</i>\n"] if heard else []
    if p["action"] == "absence":
        lines += absence_card(p["absence"])
        lines.append("\nНадіслати лист і поставити в календар?")
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Надіслати", callback_data="pend:save"),
            InlineKeyboardButton("✏️ Виправити", callback_data="pend:fix"),
            InlineKeyboardButton("❌", callback_data="pend:cancel"),
        ]])
        return "\n".join(lines), kb
    if p["action"] == "delete":
        lines.append("🗑 <b>Видалити?</b>")
        lines += event_summary(events[p["event_id"]])
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("🗑 Так, видалити", callback_data="pend:save"),
            InlineKeyboardButton("❌ Ні", callback_data="pend:cancel"),
        ]])
        return "\n".join(lines), kb
    if p["action"] == "update":
        lines.append("✏️ <b>Було:</b>")
        lines += event_summary(events[p["event_id"]])
        lines.append("\n<b>Стане:</b>")
    else:
        lines.append("➕ <b>Нова подія:</b>")
    ev = p["event"]
    lines += event_summary(ev)
    rows = []
    if p["action"] == "add" and ev.get("kind") in ("task", "wish", None):
        # розділ: справа чи бажання — можна перемкнути
        if ev.get("kind_sure") is False:
            lines.append("\n🤔 Не впевнений, куди покласти — обери розділ ⬇️")
        rows.append([
            InlineKeyboardButton(("✓ " if ev.get("kind") != "wish" else "") + "📌 Справа", callback_data="cat:task"),
            InlineKeyboardButton(("✓ " if ev.get("kind") == "wish" else "") + "🌠 Бажання", callback_data="cat:wish"),
        ])
    lines.append("\nЗберегти?")
    rows.append([
        InlineKeyboardButton("✅ Зберегти", callback_data="pend:save"),
        InlineKeyboardButton("✏️ Виправити", callback_data="pend:fix"),
        InlineKeyboardButton("❌", callback_data="pend:cancel"),
    ])
    return "\n".join(lines), InlineKeyboardMarkup(rows)


async def on_category(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Перемикач розділу в картці: 📌 Справа / 🌠 Бажання."""
    q = update.callback_query
    await q.answer()
    if not allowed(update):
        return
    p = context.user_data.get("pending")
    if not p or p.get("action") != "add":
        await q.edit_message_reply_markup(None)
        return
    kind = q.data.split(":")[1]
    p["event"] = {**p["event"], "kind": kind, "kind_sure": True}
    text, kb = pending_card(p, db.events_brief(update.effective_chat.id))
    await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)


async def free_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return
    msg = update.message
    chat_id = update.effective_chat.id
    if not GROQ_API_KEY:
        await msg.reply_text("Вільне введення не налаштоване. /add — додати покроково.")
        return
    ud = context.user_data
    events = db.events_brief(chat_id)
    names = [p["name"] for p in db.people(chat_id)]
    heard = None
    try:
        media = msg.voice or msg.audio
        if media:
            if (media.duration or 0) > MAX_VOICE_SECONDS:
                await msg.reply_text(f"Задовге — вклади в {MAX_VOICE_SECONDS // 60} хв 🙂")
                return
            await context.bot.send_chat_action(msg.chat_id, "typing")
            try:
                f = await media.get_file()
                audio = bytes(await f.download_as_bytearray())
            except (NetworkError, httpx.HTTPError):
                log.warning("Голосове не завантажилось", exc_info=True)
                await msg.reply_text("⚠️ Не зміг завантажити голосове з Telegram — надішли ще раз, будь ласка.")
                return
            heard = await ai.transcribe(audio, "voice.ogg" if msg.voice else (media.file_name or "audio.mp3"))
            if not heard:
                await msg.reply_text("Нічого не почув 🤷 Спробуй ще раз.")
                return
            text = heard
        else:
            text = msg.text
            await context.bot.send_chat_action(msg.chat_id, "typing")

        mode = ud.pop("mode", None)
        if isinstance(mode, list) and mode[0] == "gift":
            # відповідь на «Що подарував?»
            await record_gift(msg.reply_text, chat_id, mode[1], mode[2], text, heard)
            return
        pending = ud.get("pending")
        partial = ud.pop("partial", None)
        move_target = mode[1] if isinstance(mode, list) and mode[0] == "move" else None
        if move_target:
            r = await ai.interpret(text, now(), events, partial={"action": "meeting_change", "data": {
                "change": "move", "find": {"title": move_target["title"]}, "new": {}}}, people=names)
            # модель іноді бачить у «на вівторок об 11» нову зустріч — беремо з відповіді лише дату/час
            data = (r.get("meeting") if r["action"] == "meeting"
                    else (r.get("partial") or {}).get("data") if r["action"] == "error" else None)
            if data and (data.get("date") or data.get("time")):
                r = {"action": "meeting_change", "change": {"kind": "move", "find": {}, "new": {
                    "date": data.get("date"), "time": ai.parse_time(data.get("time")),
                    "duration_min": data.get("duration_min") if r["action"] == "error" else None}}}
            elif r["action"] != "meeting_change" or r["change"]["kind"] != "move":
                r = {"action": "error", "error": "на коли перенести?", "candidates": []}
        elif mode == "fix" and pending and pending["action"] in ("absence", "meeting"):
            a = pending["action"]
            r = await ai.interpret(text, now(), events, partial={"action": a, "data": pending[a]}, people=names)
        elif mode == "partial_fix" and partial:
            # відповідь на уточнення на кшталт «пауза з 14:00 — до котрої?» або «на котру годину?»
            r = await ai.interpret(text, now(), events, partial=partial, people=names)
        elif mode == "fix" and pending and pending["action"] != "delete":
            # виправляємо картку: дія й подія лишаються тими ж, змінюються поля
            r = await ai.interpret(text, now(), events, draft=pending["event"])
            if r["action"] == "add":
                r = {**pending, "event": r["event"]}
        else:
            target = mode[1] if isinstance(mode, list) and mode[0] == "target" and mode[1] in events else None
            r = await ai.interpret(text, now(), events, target_id=target, people=names)
    except ai.AIError as e:
        await msg.reply_text(f"⚠️ {cap(str(e))}. Спробуй ще раз або /add.")
        return

    prefix = f"🎙 <i>{escape(heard)}</i>\n\n" if heard else ""
    if r["action"] == "error" and r.get("partial"):
        ud["partial"] = r["partial"]
        ud["mode"] = "partial_fix"
        await msg.reply_text(f"{prefix}🤔 {escape(cap(r['error']))}", parse_mode=ParseMode.HTML)
        return
    if r["action"] == "error":
        extra = ""
        if r["candidates"]:
            extra = "\nЯку саме?\n" + "\n".join(f"• {escape(events[i]['title'])}" for i in r["candidates"])
        await msg.reply_text(
            f"{prefix}🤔 {escape(cap(r['error']))}.{extra}\n\n"
            "Приклади: «ДР брата 20 листопада, потрібен подарунок», «перенеси ДР мами на 2 жовтня», "
            "«видали концерт».",
            parse_mode=ParseMode.HTML,
        )
        return
    reply = msg.reply_text
    if r["action"] == "meeting":
        await plan_meeting(reply, ud, r["meeting"], {}, prefix)
    elif r["action"] == "meeting_change":
        if move_target:
            await change_meeting(reply, ud, r["change"], move_target, prefix)
        else:
            await find_meeting(reply, ud, r["change"], prefix)
    elif r["action"] == "reminders":
        await reply(prefix + reminders_text(chat_id, r.get("days")), parse_mode=ParseMode.HTML)
    elif r["action"] == "agenda":
        today = now().date()
        d1 = date.fromisoformat(r["date"]) if r["date"] else (
            today + timedelta(days=1) if now().hour >= AGENDA_TOMORROW_HOUR else today)
        d2 = date.fromisoformat(r["date_to"]) if r["date_to"] else d1
        await show_agenda(reply, ud, d1, d2, prefix)
    elif r["action"] == "idea":
        pid = db.person_id(chat_id, r["person"])
        db.add_idea(pid, r["text"])
        await reply(f"{prefix}💡 Записав ідею для «{escape(cap(r['person']))}»: {escape(r['text'])}",
                    parse_mode=ParseMode.HTML)
    elif r["action"] == "recipients":
        pids = [db.person_id(chat_id, n) for n in r["people"]]
        db.set_recipients(chat_id, r["event_id"], pids)
        title = events[r["event_id"]]["title"]
        await reply(f"{prefix}🎁 {escape(title)}: подарунки — " + ", ".join(escape(cap(n)) for n in r["people"])
                    + "\nНагадаю за місяць, тиждень, день і в сам день, поки не відмітиш усіх.",
                    parse_mode=ParseMode.HTML)
    elif r["action"] == "person_info":
        await reply(prefix + person_text(chat_id, r["person"]), parse_mode=ParseMode.HTML)
    else:
        ud["pending"] = r
        text, kb = pending_card(r, events, heard)
        await reply(text, parse_mode=ParseMode.HTML, reply_markup=kb)


# ---------- люди, ідеї, подарунки ----------

def gifts_line(pid, limit=3) -> str | None:
    gs = db.gifts(pid)[:limit]
    if not gs:
        return None
    return "🎁 Раніше: " + "; ".join(f"{g['year']} — {escape(g['text'])}" for g in gs)


def ideas_line(pid) -> str | None:
    items = db.ideas(pid)
    return ("💡 Ідеї: " + " · ".join(escape(i["text"]) for i in items)) if items else None


def occ_extras(occ):
    """Додаткові рядки й кнопки картки: ідеї/історія для ДР, отримувачі подарунків для свят."""
    lines, rows = [], []
    recips = db.recipients(occ["id"])
    if recips:
        for r in recips:
            line = f"{'✅' if r['done'] else '⬜️'} {escape(cap(r['name']))}"
            idea = ideas_line(r["person_id"])
            if idea and not r["done"]:
                line += " · " + idea[3:]
            lines.append(line)
        pending = [r for r in recips if not r["done"]]
        for i in range(0, len(pending), 3):
            rows.append([InlineKeyboardButton(f"🎁 {cap(r['name'])}", callback_data=f"rg:{occ['id']}:{r['person_id']}")
                         for r in pending[i:i + 3]])
    elif occ["person_id"]:
        for line in (ideas_line(occ["person_id"]), gifts_line(occ["person_id"])):
            if line:
                lines.append(line)
    return lines, rows


def render_occ(occ, today, key=None):
    lines, rows = occ_extras(occ)
    return render(occ, today, key, lines, rows)


def person_text(chat_id, name: str) -> str:
    person = next((p for p in db.people(chat_id) if p["name"].lower() == name.lower()), None)
    if not person:
        return f"🤷 Про «{escape(cap(name))}» поки нічого немає. Скажи, наприклад, «ідея подарунка {escape(name)} — …»."
    pid = person["id"]
    lines = [f"👤 <b>{escape(cap(person['name']))}</b>"]
    today = now().date()
    for e in db.person_events(chat_id, pid):
        d = db.next_date(e, today)
        if d:
            lines.append(f"{icon(e)} {escape(cap(e['title']))} — {fmt_day_month(d)} ({rel_days((d - today).days)})")
    items = db.ideas(pid)
    lines.append("💡 Ідеї:" + ("".join(f"\n  • {escape(i['text'])}" for i in items) if items else " поки немає"))
    gs = db.gifts(pid)
    if gs:
        lines.append("🎁 Дарував:" + "".join(f"\n  • {g['year']} — {escape(g['text'])} ({escape(g['occasion'])})"
                                          for g in gs[:10]))
    return "\n".join(lines)


def rel_days(n: int) -> str:
    return "сьогодні" if n == 0 else "завтра" if n == 1 else "вчора" if n == -1 else (
        f"через {plural_days(n)}" if n > 0 else f"{plural_days(n)} тому")


async def ask_what_gift(reply, ud, occ, pid):
    """«Що подарував?» — кнопки з ідеями, «Пропустити», або відповідь голосом/текстом."""
    person = db.get_person(occ["chat_id"], pid)
    if not person:
        return
    ud["mode"] = ["gift", occ["id"], pid]
    rows = [[InlineKeyboardButton(f"💡 {i['text'][:40]}", callback_data=f"gi:{occ['id']}:{pid}:{i['id']}")]
            for i in db.ideas(pid)[:6]]
    rows.append([InlineKeyboardButton("Пропустити", callback_data=f"gi:{occ['id']}:{pid}:x")])
    await reply(f"🎁 Що подарував — {escape(cap(person['name']))}? Скажи або обери ідею (можна пропустити).",
                parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(rows))


async def record_gift(reply, chat_id, occ_id, pid, text, heard=None):
    occ = db.get_occurrence(chat_id, occ_id)
    person = db.get_person(chat_id, pid)
    if not occ or not person:
        return
    db.add_gift(pid, occ["title"], date.fromisoformat(occ["date"]).year, text[:300])
    prefix = f"🎙 <i>{escape(heard)}</i>\n\n" if heard else ""
    await reply(f"{prefix}📝 Записав: {escape(cap(person['name']))} — {escape(text[:300])}. "
                "Наступного разу покажу, щоб не повторитись.", parse_mode=ParseMode.HTML)


async def on_gift_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not allowed(update):
        return
    _, occ_id, pid, idea = q.data.split(":")
    occ_id, pid = int(occ_id), int(pid)
    context.user_data.pop("mode", None)
    await q.edit_message_reply_markup(None)
    if idea == "x":
        await q.edit_message_text("🎁 Добре, без запису.")
        return
    it = db.get_idea(pid, int(idea))
    if not it:
        return
    db.use_idea(it["id"])
    await record_gift(q.message.reply_text, update.effective_chat.id, occ_id, pid, it["text"])


# ---------- зустрічі з колегами ----------
# user_data["pick"] — чекаємо вибір людини: {"meeting", "picks", "name", "options": [{name, email}]}

def fmt_slot(start: datetime, end: datetime | None = None) -> str:
    s = f"{WEEKDAYS_SHORT[start.weekday()]}, {fmt_day_month(start.date())} {start:%H:%M}"
    return s + (f"–{end:%H:%M}" if end else "")


async def plan_meeting(reply, ud: dict, m: dict, picks: dict, prefix: str = ""):
    """Знаходить учасників у каталозі, перевіряє зайнятість і показує картку зустрічі."""
    if not google_api.configured():
        await reply("⚠️ Google ще не підключено — зустріч створити не можу.")
        return
    try:
        people = await google_api.directory()
        names = [a for a in m["attendees"] if not meetings.is_email(a) and a not in picks]
        matches = await ai.match_people(names, people) if names else {}
    except (google_api.GoogleError, ai.AIError, httpx.HTTPError) as e:
        await reply(f"⚠️ {e}")
        return
    for name in names:
        idx = matches.get(name, [])
        if not idx:
            ud["partial"] = {"action": "meeting", "data": m}
            ud["mode"] = "partial_fix"
            await reply(f"{prefix}🤔 Не знайшов «{escape(name)}» у каталозі {WORK_EMAIL_FROM.split("@")[-1]}. Скажи прізвище або e-mail.",
                        parse_mode=ParseMode.HTML)
            return
        if len(idx) > 1:
            options = [people[i] for i in idx[:8]]
            ud["pick"] = {"meeting": m, "picks": picks, "name": name, "options": options}
            kb = InlineKeyboardMarkup(
                [[InlineKeyboardButton(f"{o['name']} · {o['email']}", callback_data=f"who:{i}")]
                 for i, o in enumerate(options)]
                + [[InlineKeyboardButton("❌ Скасувати", callback_data="who:x")]]
            )
            await reply(f"{prefix}👥 Кого з них маєш на увазі під «{escape(name)}»?",
                        parse_mode=ParseMode.HTML, reply_markup=kb)
            return
        picks[name] = people[idx[0]]["email"]

    by_email = {p["email"].lower(): p for p in people}
    guests = []
    for a in m["attendees"]:
        email = a if meetings.is_email(a) else picks[a]
        guests.append(by_email.get(email.lower(), {"name": email, "email": email}))
    start = meetings.start_of(m)
    end = start + timedelta(minutes=m["duration_min"])
    ud["pending"] = {
        "action": "meeting", "meeting": m, "guests": guests,
        "start": start.isoformat(), "end": end.isoformat(),
        **await check_busy(guests, start, end, m["duration_min"]),
    }
    text, kb = meeting_card(ud["pending"])
    await reply(prefix + text, parse_mode=ParseMode.HTML, reply_markup=kb)


async def check_busy(guests: list, start: datetime, end: datetime, minutes: int, ignore=None) -> dict:
    """Хто зайнятий у [start, end) і найближчі вільні для всіх слоти.

    ignore — (start, end) зустрічі, що переноситься: її власний інтервал зайнятістю не вважаємо.
    """
    try:
        busy, hidden = await google_api.freebusy(
            [WORK_EMAIL_FROM] + [g["email"] for g in guests],
            datetime.combine(start.date(), datetime.min.time(), tzinfo=TZ),
            datetime.combine(start.date() + timedelta(days=8), datetime.min.time(), tzinfo=TZ),
        )
    except (google_api.GoogleError, httpx.HTTPError) as e:
        busy, hidden = {}, []
        log.warning("freeBusy: %s", e)
    if ignore:
        busy = {k: [iv for iv in v if iv != ignore] for k, v in busy.items()}
    who = {WORK_EMAIL_FROM.lower(): "Ти"} | {g["email"].lower(): g["name"] for g in guests}
    conflicts = []
    for email, intervals in busy.items():
        for bs, be in meetings.overlaps(intervals, start, end):
            conflicts.append(f"{who.get(email.lower(), email)} — зайнято {bs:%H:%M}–{be:%H:%M}")
    merged = [iv for intervals in busy.values() for iv in intervals]
    slots = meetings.find_slots(merged, start, minutes, now()) if conflicts else []
    return {"conflicts": conflicts, "hidden": [who.get(h.lower(), h) for h in hidden],
            "slots": [x.isoformat() for x in slots]}


def meeting_card(p: dict):
    m = p["meeting"]
    start, end = datetime.fromisoformat(p["start"]), datetime.fromisoformat(p["end"])
    title = p.get("title") or m.get("title") or meetings.default_title(p["guests"])
    moving = p["action"] == "meeting_move"
    lines = [f"🔁 <b>Перенести «{escape(title)}»</b>" if moving else f"📅 <b>{escape(title)}</b>"]
    if moving:
        old_s, old_e = datetime.fromisoformat(p["old_start"]), datetime.fromisoformat(p["old_end"])
        lines.append(f"було: {fmt_slot(old_s, old_e)}")
    lines += [
        f"🕒 {'стане: ' if moving else ''}{fmt_slot(start, end)} ({m['duration_min']} хв)",
        "👥 " + ", ".join(f"{escape(g['name'])}" for g in p["guests"]),
    ]
    if not moving:
        lines.append("🎥 Google Meet" if m["meet"] else "🏢 Без Meet")
    if m.get("notes"):
        lines.append(f"📝 {escape(m['notes'])}")
    if p["hidden"]:
        lines.append("👁 Не бачу зайнятість: " + ", ".join(escape(h) for h in p["hidden"]))
    rows = []
    if p["conflicts"]:
        lines.append("\n⚠️ <b>У цей час зайняті:</b>")
        lines += [f"• {escape(c)}" for c in p["conflicts"]]
        if p["slots"]:
            lines.append("\nВільно в усіх:")
            rows += [[InlineKeyboardButton(
                "🕒 " + fmt_slot(datetime.fromisoformat(x)), callback_data=f"slot:{i}")]
                for i, x in enumerate(p["slots"])]
        rows.append([InlineKeyboardButton(f"Все одно о {start:%H:%M}", callback_data="pend:save")])
        rows.append([InlineKeyboardButton("✏️ Виправити", callback_data="pend:fix"),
                     InlineKeyboardButton("❌", callback_data="pend:cancel")])
    else:
        lines.append("\n✅ Усі вільні. " + ("Перенести? Учасники отримають оновлення." if moving
                                           else "Створити зустріч і надіслати запрошення?"))
        rows.append([
            InlineKeyboardButton("✅ Перенести" if moving else "✅ Створити", callback_data="pend:save"),
            InlineKeyboardButton("✏️ Виправити", callback_data="pend:fix"),
            InlineKeyboardButton("❌", callback_data="pend:cancel"),
        ])
    return "\n".join(lines), InlineKeyboardMarkup(rows)


async def on_meeting_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Кнопки вибору людини (who:i) і вільного слоту (slot:i)."""
    q = update.callback_query
    await q.answer()
    if not allowed(update):
        return
    ud = context.user_data
    kind, _, arg = q.data.partition(":")
    if kind == "who":
        pick = ud.pop("pick", None)
        await q.edit_message_reply_markup(None)
        if not pick or arg == "x":
            await q.message.reply_text("❌ Скасував.")
            return
        chosen = pick["options"][int(arg)]
        picks = {**pick["picks"], pick["name"]: chosen["email"]}
        await plan_meeting(q.message.reply_text, ud, pick["meeting"], picks)
        return
    p = ud.get("pending")
    if not p or p.get("action") not in ("meeting", "meeting_move") or int(arg) >= len(p["slots"]):
        await q.edit_message_reply_markup(None)
        return
    start = datetime.fromisoformat(p["slots"][int(arg)])
    end = start + timedelta(minutes=p["meeting"]["duration_min"])
    p["meeting"] = {**p["meeting"], "date": start.date().isoformat(), "time": f"{start:%H:%M}"}
    p.update(start=start.isoformat(), end=end.isoformat(), conflicts=[], slots=[])
    text, kb = meeting_card(p)
    await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)


AGENDA_TOMORROW_HOUR = 19  # після цієї години «які зустрічі?» без дати — про завтра


def agenda_line(e: dict) -> str:
    if e["type"] == "workingLocation":
        return f"📍 {escape(e['title'])}"
    if e["all_day"]:
        return f"▫️ весь день — {escape(e['title'])}"
    start, end = datetime.fromisoformat(e["start"]), datetime.fromisoformat(e["end"])
    until = "24:00" if end.time() == datetime.min.time() and end.date() > start.date() else f"{end:%H:%M}"
    if e["type"] == "outOfOffice":
        return f"🏖 Не на місці {start:%H:%M}–{until}"
    parts = [f"<b>{start:%H:%M}–{until}</b>  {escape(e['title'])}"]
    if e["guests"]:
        parts.append(f"{e['guests']} уч.")
    if e["meet"]:
        parts.append(f'<a href="{e["meet"]}">🎥 Meet</a>')
    mark = {"needsAction": "❔ ", "tentative": "❔ "}.get(e["response"], "")
    return mark + " · ".join(parts)


async def show_agenda(reply, ud: dict, d1: date, d2: date, prefix: str = ""):
    if not google_api.configured():
        await reply("⚠️ Google ще не підключено.")
        return
    t_min = datetime.combine(d1, datetime.min.time(), tzinfo=TZ)
    t_max = datetime.combine(d2 + timedelta(days=1), datetime.min.time(), tzinfo=TZ)
    try:
        items = await google_api.agenda(t_min, t_max)
    except (google_api.GoogleError, httpx.HTTPError) as e:
        await reply(f"⚠️ {e}")
        return
    day_name = lambda dd: f"{WEEKDAYS_SHORT[dd.weekday()].capitalize()}, {fmt_day_month(dd)}"  # noqa: E731
    lines, day = [], None
    for e in items:
        dd = max(date.fromisoformat(e["start"][:10]), d1)
        if d1 != d2 and dd != day:  # заголовки днів — лише коли період більший за день
            day = dd
            lines.append(f"\n📅 <b>{day_name(dd)}</b>")
        lines.append(agenda_line(e))
    period = day_name(d1) if d1 == d2 else f"{fmt_day_month(d1)} – {fmt_day_month(d2)}"
    if not items:
        await reply(f"{prefix}🗓 {period}: у календарі порожньо.", parse_mode=ParseMode.HTML)
        return
    # кнопки перенести/скасувати — лише для моїх майбутніх зустрічей з учасниками
    t = now()
    own = [e for e in items if e["mine"] and not e["all_day"] and e["type"] == "default"
           and e["attendees"] and datetime.fromisoformat(e["start"]) > t][:6]
    ud["agenda"] = own
    rows = [[
        InlineKeyboardButton(f"🔁 {datetime.fromisoformat(e['start']):%d.%m %H:%M} {e['title'][:18]}",
                             callback_data=f"agmv:{i}"),
        InlineKeyboardButton("🗑", callback_data=f"agdel:{i}"),
    ] for i, e in enumerate(own)]
    await reply(prefix + f"🗓 <b>Календар: {period}</b>\n" + "\n".join(lines),
                parse_mode=ParseMode.HTML, disable_web_page_preview=True,
                reply_markup=InlineKeyboardMarkup(rows) if rows else None)


async def on_agenda_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not allowed(update):
        return
    ud = context.user_data
    kind, _, arg = q.data.partition(":")
    own = ud.get("agenda") or []
    if int(arg) >= len(own):
        await q.message.reply_text("Список застарів — запитай зустрічі ще раз.")
        return
    ev = own[int(arg)]
    if kind == "agdel":
        await change_meeting(q.message.reply_text, ud, {"kind": "cancel"}, ev)
        return
    ud["mode"] = ["move", ev]
    start = datetime.fromisoformat(ev["start"])
    await q.message.reply_text(
        f"🔁 На коли перенести «{escape(ev['title'])}» ({fmt_slot(start)})? "
        "Скажи або напиши: «на 16:00», «на вівторок об 11».",
        parse_mode=ParseMode.HTML,
    )


async def today_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return
    today = now().date()
    await show_agenda(update.message.reply_text, context.user_data, today, today)


async def find_meeting(reply, ud: dict, change: dict, prefix: str = ""):
    """Шукає в календарі мою зустріч за ознаками з фрази й показує картку скасування/перенесення."""
    if not google_api.configured():
        await reply("⚠️ Google ще не підключено.")
        return
    f = change["find"]
    t = now()
    if f["date"]:
        day = datetime.combine(date.fromisoformat(f["date"]), datetime.min.time(), tzinfo=TZ)
        window = (max(day, t), day + timedelta(days=1))
    else:
        window = (t, t + timedelta(days=30))
    try:
        found = await google_api.list_events(*window)
        if f["attendees"]:
            people = await google_api.directory()
            names = [a for a in f["attendees"] if not meetings.is_email(a)]
            matches = await ai.match_people(names, people) if names else {}
            wanted = [
                {a.lower()} if meetings.is_email(a) else {people[i]["email"].lower() for i in matches.get(a, [])}
                for a in f["attendees"]
            ]
            found = [e for e in found if all(w & {x.lower() for x in e["attendees"]} for w in wanted)]
    except (google_api.GoogleError, ai.AIError, httpx.HTTPError) as e:
        await reply(f"⚠️ {e}")
        return
    if f["time"]:
        found = [e for e in found if datetime.fromisoformat(e["start"]).strftime("%H:%M") == f["time"]]
    if f["title"]:
        found = [e for e in found if f["title"].lower() in e["title"].lower()] or found
    if not found:
        period = "цього дня" if f["date"] else "найближчі 30 днів"
        await reply(f"{prefix}🤷 Не знайшов таку зустріч ({period}) серед тих, де ти організатор.",
                    parse_mode=ParseMode.HTML)
        return
    if len(found) > 1:
        found = found[:6]
        ud["mpick"] = {"change": change, "events": found}
        kb = InlineKeyboardMarkup(
            [[InlineKeyboardButton(f"{fmt_slot(datetime.fromisoformat(e['start']))} · {e['title'][:30]}",
                                   callback_data=f"mpick:{i}")] for i, e in enumerate(found)]
            + [[InlineKeyboardButton("❌ Скасувати", callback_data="mpick:x")]]
        )
        await reply(f"{prefix}📅 Яку зустріч?", parse_mode=ParseMode.HTML, reply_markup=kb)
        return
    await change_meeting(reply, ud, change, found[0], prefix)


async def change_meeting(reply, ud: dict, change: dict, ev: dict, prefix: str = ""):
    people = await google_api.directory()
    by_email = {p["email"].lower(): p for p in people}
    guests = [by_email.get(a.lower(), {"name": a, "email": a}) for a in ev["attendees"]]
    old_s, old_e = datetime.fromisoformat(ev["start"]), datetime.fromisoformat(ev["end"])
    if change["kind"] == "cancel":
        ud["pending"] = {"action": "meeting_cancel", "event": ev}
        text = "\n".join([
            f"🗑 <b>Скасувати зустріч «{escape(ev['title'])}»?</b>",
            f"🕒 {fmt_slot(old_s, old_e)}",
            "👥 " + (", ".join(escape(g["name"]) for g in guests) or "без учасників"),
            "\nУчасники отримають сповіщення про скасування.",
        ])
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("🗑 Скасувати зустріч", callback_data="pend:save"),
            InlineKeyboardButton("❌ Ні", callback_data="pend:cancel"),
        ]])
        await reply(prefix + text, parse_mode=ParseMode.HTML, reply_markup=kb)
        return
    new = change["new"]
    minutes = new["duration_min"] or int((old_e - old_s).total_seconds() // 60)
    day = date.fromisoformat(new["date"]) if new["date"] else old_s.date()
    hm = new["time"] or f"{old_s:%H:%M}"
    start = datetime.combine(day, datetime.strptime(hm, "%H:%M").time(), tzinfo=TZ)
    end = start + timedelta(minutes=minutes)
    if start <= now():
        await reply(f"{prefix}🤔 {fmt_slot(start)} вже минуло — на коли перенести?", parse_mode=ParseMode.HTML)
        return
    ud["pending"] = {
        "action": "meeting_move", "event_id": ev["id"], "title": ev["title"], "guests": guests,
        "meeting": {"duration_min": minutes, "meet": True},
        "old_start": ev["start"], "old_end": ev["end"],
        "start": start.isoformat(), "end": end.isoformat(),
        **await check_busy(guests, start, end, minutes, ignore=(old_s, old_e)),
    }
    text, kb = meeting_card(ud["pending"])
    await reply(prefix + text, parse_mode=ParseMode.HTML, reply_markup=kb)


async def on_meeting_pick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not allowed(update):
        return
    pick = context.user_data.pop("mpick", None)
    arg = q.data.split(":")[1]
    await q.edit_message_reply_markup(None)
    if not pick or arg == "x":
        await q.message.reply_text("❌ Скасував.")
        return
    await change_meeting(q.message.reply_text, context.user_data, pick["change"], pick["events"][int(arg)])


async def apply_meeting_change(reply, p: dict):
    try:
        if p["action"] == "meeting_cancel":
            await google_api.delete_event(p["event"]["id"])
            await reply(f"🗑 Зустріч «{escape(p['event']['title'])}» скасовано, учасники отримають сповіщення.",
                        parse_mode=ParseMode.HTML)
            return
        start, end = datetime.fromisoformat(p["start"]), datetime.fromisoformat(p["end"])
        res = await google_api.move_event(p["event_id"], start, end)
    except (google_api.GoogleError, httpx.HTTPError) as e:
        log.exception("Зустріч не змінено")
        await reply(f"⚠️ Не вдалося: {escape(str(e))}", parse_mode=ParseMode.HTML)
        return
    lines = [f"🔁 «{escape(p['title'])}» перенесено на {fmt_slot(start, end)}, учасники отримають оновлення."]
    if res["link"]:
        lines.append(f'📅 <a href="{res["link"]}">Відкрити в календарі</a>')
    await reply("\n".join(lines), parse_mode=ParseMode.HTML, disable_web_page_preview=True)


async def create_meeting(reply, p: dict):
    m = p["meeting"]
    start, end = datetime.fromisoformat(p["start"]), datetime.fromisoformat(p["end"])
    title = m["title"] or meetings.default_title(p["guests"])
    try:
        res = await google_api.create_meeting(title, start, end, [g["email"] for g in p["guests"]],
                                              m.get("notes"), m["meet"])
    except (google_api.GoogleError, httpx.HTTPError) as e:
        log.exception("Зустріч не створено")
        await reply(f"⚠️ Зустріч НЕ створено: {escape(str(e))}", parse_mode=ParseMode.HTML)
        return
    lines = [f"✅ <b>{escape(title)}</b> — {fmt_slot(start, end)}",
             "✉️ Запрошення надіслано: " + ", ".join(escape(g["name"]) for g in p["guests"])]
    if res["meet"]:
        lines.append(f"🎥 {res['meet']}")
    if res["link"]:
        lines.append(f'📅 <a href="{res["link"]}">Відкрити в календарі</a>')
    await reply("\n".join(lines), parse_mode=ParseMode.HTML, disable_web_page_preview=True)


async def on_pending(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not allowed(update):
        return
    ud = context.user_data
    action = q.data.split(":")[1]
    p = ud.get("pending")
    if not p:
        await q.edit_message_reply_markup(None)
        return
    chat_id = update.effective_chat.id
    if action == "fix":
        ud["mode"] = "fix"
        await q.edit_message_reply_markup(None)
        await q.message.reply_text("Що виправити? Скажи або напиши, наприклад: «не 1-го, а 2-го» або «подарунок не потрібен».")
        return
    ud.pop("pending", None)
    ud.pop("mode", None)
    await q.edit_message_reply_markup(None)
    if action == "cancel":
        await q.message.reply_text("❌ Скасував.")
    elif p["action"] == "add":
        await save_event(q.message.reply_text, update, context, p["event"])
    elif p["action"] == "update":
        ok = db.update_event(chat_id, p["event_id"], p["event"], now().date())
        if ok and p["event"].get("person"):
            db.link_person(p["event_id"], db.person_id(chat_id, p["event"]["person"]))
        await q.message.reply_text("✅ Зберіг зміни." if ok else "Подію вже видалено 🤷")
        if ok:
            await send_due(context.bot)
    elif p["action"] == "absence":
        await send_absence(q.message.reply_text, p["absence"])
    elif p["action"] == "meeting":
        await create_meeting(q.message.reply_text, p)
    elif p["action"] in ("meeting_cancel", "meeting_move"):
        await apply_meeting_change(q.message.reply_text, p)
    elif p["action"] == "delete":
        ok = db.archive_event(chat_id, p["event_id"])
        await q.message.reply_text("🗑 Видалено." if ok else "Подію вже видалено 🤷")


async def send_absence(reply, a: dict):
    if not google_api.configured():
        await reply("⚠️ Google ще не підключено (немає доступу до пошти й календаря) — нічого не надіслав.")
        return
    c = work.compose(a)
    report = []
    try:
        await google_api.send_email(c["subject"], c["body"])
        report.append(f"✉️ Лист надіслано на {escape(WORK_EMAIL_TO)}")
    except (google_api.GoogleError, httpx.HTTPError) as e:
        log.exception("Лист про відсутність не пішов")
        report.append(f"⚠️ Лист НЕ надіслано: {escape(str(e))}")
    try:
        links = [await google_api.create_ooo(s, e) for s, e in c["periods"]]
        n = f" ({len(links)} дн.)" if len(links) > 1 else ""
        report.append(f'📅 <a href="{links[0]}">«Не на місці»</a> в календарі{n}' if links[0]
                      else f"📅 «Не на місці» в календарі{n}")
    except (google_api.GoogleError, httpx.HTTPError) as e:
        log.exception("Подію в календарі не створено")
        report.append(f"⚠️ Календар НЕ оновлено: {escape(str(e))}")
    await reply("\n".join(report), parse_mode=ParseMode.HTML, disable_web_page_preview=True)


async def on_edit_event(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Кнопка ✏️ в /all: наступне повідомлення (голос/текст) — правка цієї події."""
    q = update.callback_query
    await q.answer()
    if not allowed(update):
        return
    event_id = int(q.data.split(":")[1])
    events = db.events_brief(update.effective_chat.id)
    if event_id not in events:
        await q.edit_message_text("Подію вже видалено 🤷")
        return
    context.user_data["mode"] = ["target", event_id]
    await q.message.reply_text(
        f"Що змінити в «{escape(events[event_id]['title'])}»? Скажи або напиши: "
        "«перенеси на 5 жовтня», «подарунок не потрібен», «додай нотатку: любить каву» або «видали».",
        parse_mode=ParseMode.HTML,
    )


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    for key in ("new", "pending", "mode", "draft", "editing", "partial", "pick", "mpick"):
        context.user_data.pop(key, None)
    await update.message.reply_text("Ок, скасував.")
    return ConversationHandler.END


# ---------- перегляд ----------

def reminders_text(chat_id, days: int | None) -> str:
    """Усі заплановані події одним повідомленням, блоками: події, дні народження, найближчі свята."""
    today = now().date()
    db.sync_occurrences(today)
    rows = [o for o in db.upcoming(chat_id, today + timedelta(days=days or 400)) if o["status"] == "active"]

    def when(o):
        d = date.fromisoformat(o["date"])
        return fmt_day_month(d) + (f" {d.year}" if d.year != today.year else "") + (f" в {o['time']}" if o["time"] else "")

    events = [o for o in rows if o["recurrence"] != "holiday" and o["kind"] not in ("birthday", "wish")]
    birthdays = [o for o in rows if o["kind"] == "birthday"]
    hols = [o for o in rows if o["recurrence"] == "holiday"][:3]
    head = f"<b>Події на {plural_days(days)}</b>" if days else "<b>Усі заплановані події</b>"
    blocks = [[head] + ([f"📋 {escape(cap(o['title']))} — {when(o)}" for o in events] or ["📭 Поки порожньо"])]
    if birthdays:
        blocks.append(["<b>Дні народження</b>"] + [f"🎂 {escape(cap(o['title']))} — {when(o)}" for o in birthdays])
    wishes, _ = section_text(chat_id, "wishes") if db.by_kind(chat_id, ("wish",), today) else (None, None)
    if wishes and not days:
        blocks.append(["<b>Бажання</b>"] + wishes.split("\n")[1:])
    if hols:
        blocks.append(["<b>Найближчі свята</b>"] + [f"🍻 {escape(o['title'])} — {when(o)}" for o in hols])
    return "\n\n".join("\n".join(b) for b in blocks)


def section_text(chat_id, section: str):
    """Списки для кнопок меню: свята, дні народження, бажання, справи."""
    today = now().date()

    def when(d):
        if d is None:
            return ""
        return " — " + fmt_day_month(d) + (f" {d.year}" if d.year != today.year else "")

    if section == "holidays":
        items = db.by_kind(chat_id, ("holiday",), today)
        lines = ["<b>Всі свята</b>"] + [f"🍻 {escape(e['title'])}{when(d)}" for e, d in items]
        return "\n".join(lines if items else lines + ["Поки немає. Натисни /start — додам свята України."]), None
    if section == "birthdays":
        items = db.by_kind(chat_id, ("birthday",), today)
        lines = ["<b>Всі дні народження</b>"]
        for e, d in items:
            age = f", виповниться {d.year - e['year']}" if e["year"] and d and d.year > e["year"] else ""
            lines.append(f"🎂 {escape(cap(e['title']))}{when(d)}{age}")
        return "\n".join(lines if items else lines + ["Поки немає. Скажи: «у мами ДР 1 жовтня»."]), None
    if section == "wishes":
        items = db.by_kind(chat_id, ("wish",), today)
        lines = ["<b>Всі бажання</b>"] + [f"🌠 {escape(cap(e['title']))}{when(d)}" for e, d in items]
        rows = [[InlineKeyboardButton(f"✨ Здійснив: {cap(e['title'])[:28]}", callback_data=f"wd:{e['id']}")]
                for e, _ in items[:10]]
        if not items:
            lines.append("Поки немає. Скажи: «хочу колись піднятися на Говерлу».")
        return "\n".join(lines), InlineKeyboardMarkup(rows) if rows else None
    items = db.by_kind(chat_id, ("task", "event", "anniversary"), today)
    lines = ["<b>Всі справи</b>"] + [f"📋 {escape(cap(e['title']))}" + (when(d) if d else " — без дати")
                                    + (f" в {e['time']}" if e["time"] else "") for e, d in items]
    return "\n".join(lines if items else lines + ["📭 Поки порожньо."]), None


async def on_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return
    text, kb = section_text(update.effective_chat.id, MENU[update.message.text.strip()])
    await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kb or NO_KB)


def undated_view(chat_id):
    items = db.undated_tasks(chat_id)
    if not items:
        return "📋 Справ без дат не лишилось ✅", None
    lines = ["📋 <b>У тебе є справи без дат:</b>"] + [f"• {escape(cap(e['title']))}" for e in items]
    lines.append("\n✅ — зроблено, 📅 — призначити дату")
    rows = [[InlineKeyboardButton(f"✅ {cap(e['title'])[:30]}", callback_data=f"ndd:{e['id']}"),
             InlineKeyboardButton("📅", callback_data=f"evedit:{e['id']}")] for e in items[:15]]
    return "\n".join(lines), InlineKeyboardMarkup(rows)


async def on_nodate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """🗓 Прибрати дату (у /all) і ✅ у місячному списку справ без дат."""
    q = update.callback_query
    if not allowed(update):
        await q.answer()
        return
    chat_id = update.effective_chat.id
    action, _, raw = q.data.partition(":")
    e = db.get_event(chat_id, int(raw))
    if not e:
        await q.answer()
        return
    if action == "nodate":
        ok = db.clear_date(chat_id, e["id"])
        await q.answer("🗓 Дату прибрано" if ok else "Не вийшло")
        if ok:
            await q.edit_message_text(f"{icon({**dict(e), 'kind': e['kind'] or 'task'})} <b>{escape(cap(e['title']))}</b>"
                                      " — без дати\n📋 Нагадаю списком 1-го числа", parse_mode=ParseMode.HTML)
        return
    db.archive_event(chat_id, e["id"])
    await q.answer(f"✅ {cap(e['title'])}")
    text, kb = undated_view(chat_id)
    await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)


async def section_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/tasks /wishes /birthdays /holidays — списки розділів."""
    if not allowed(update):
        return
    section = update.message.text.split()[0].lstrip("/").split("@")[0]
    text, kb = section_text(update.effective_chat.id, section)
    await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kb or NO_KB)


async def remove_old_keyboard(bot, chat_id):
    """Один раз прибирає з чату старі кнопки-меню (Telegram тримає їх, поки бот не надішле remove)."""
    if db.set_flag(f"kbremove:{chat_id}"):
        await bot.send_message(chat_id, "ℹ️ Кнопки внизу прибрав — розділи тепер у меню команд «/».",
                               reply_markup=NO_KB)


async def ensure_commands(bot):
    """Один раз (на версію списку) публікує меню команд у Telegram."""
    if db.set_flag(COMMANDS_VERSION):
        try:
            await bot.set_my_commands([BotCommand(c, d) for c, d in COMMANDS])
        except Exception:
            log.exception("Меню команд не оновлено")


async def on_wish_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not allowed(update):
        await q.answer()
        return
    chat_id = update.effective_chat.id
    e = db.get_event(chat_id, int(q.data.split(":")[1]))
    if e and not e["archived"]:
        db.archive_event(chat_id, e["id"])
        await q.answer(f"✨ {cap(e['title'])} — здійснено!")
    else:
        await q.answer()
    text, kb = section_text(chat_id, "wishes")
    await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)


async def list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return
    days = int(context.args[0]) if context.args and context.args[0].isdigit() else None
    await update.message.reply_text(reminders_text(update.effective_chat.id, days), parse_mode=ParseMode.HTML)


async def all_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return
    events = db.list_events(update.effective_chat.id)
    if not events:
        await update.message.reply_text("Поки порожньо. /add — додати.")
        return
    for e in events:
        when = "без дати" if not e["day"] else (
            fmt_day_month(date(2000, e["month"], e["day"])) + (f" {e['year']}" if e["year"] else ""))
        if e["time"]:
            when += f" в {e['time']}"
        row = [InlineKeyboardButton("✏️ Змінити", callback_data=f"evedit:{e['id']}"),
               InlineKeyboardButton("🗑 Видалити", callback_data=f"del:{e['id']}")]
        if e["day"] and e["recurrence"] == "once" and (e["kind"] or "task") in ("task", "wish", "event"):
            row.insert(1, InlineKeyboardButton("🗓 Прибрати дату", callback_data=f"nodate:{e['id']}"))
        kb = InlineKeyboardMarkup([row])
        await update.message.reply_text(
            f"{icon(e)} <b>{escape(cap(e['title']))}</b> — {when}, {REC_LABEL.get(e['recurrence'], '')}",
            parse_mode=ParseMode.HTML, reply_markup=kb,
        )


# ---------- кнопки в нагадуваннях ----------

async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not allowed(update):
        await q.answer()
        return
    action, _, raw = q.data.partition(":")
    chat_id = update.effective_chat.id
    obj_id = int(raw.split(":")[0])

    if action == "del":
        await q.answer()
        if q.message.text and "Видалити?" not in q.message.text:
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("Так, видалити", callback_data=f"delok:{obj_id}")]])
            await q.edit_message_text(q.message.text + "\n\nВидалити?", reply_markup=kb)
        return
    if action == "delok":
        ok = db.archive_event(chat_id, obj_id)
        await q.answer("Видалено" if ok else "Не знайдено")
        await q.edit_message_text("🗑 Видалено" if ok else "Не знайдено")
        return

    occ = db.get_occurrence(chat_id, obj_id)
    if not occ:
        await q.answer("Подію не знайдено")
        await q.edit_message_reply_markup(None)
        return
    today_iso = now().date().isoformat()
    db.update_occurrence(occ["id"], acted_on=today_iso)  # будь-яка реакція — сьогодні більше не повторюємо
    if action in OLD_SNOOZE or action in SNOOZE:
        until = snooze_until(occ, now(), action)
        db.update_occurrence(occ["id"], snoozed_until=until.isoformat())
        await q.answer(f"⏰ Нагадаю {until:%d.%m} в {until:%H:%M}")
        # у чаті лишаються тільки «Виконано»: відкладене нагадування прибираємо
        await cleanup_msgs(context.bot, occ["id"], keep=q.message.message_id)
        db.pop_msgs(occ["id"])
        try:
            await q.message.delete()
        except Exception:  # старше 48 год — Telegram не дасть видалити, тоді хоча б згортаємо
            await q.edit_message_text(snoozed_line(occ, until), parse_mode=ParseMode.HTML)
        return
    ask_gift_for = None
    if action == "gift":
        db.update_occurrence(occ["id"], gift_done=1)
        note = "🎁 Чудово, подарунок є!"
        ask_gift_for = occ["person_id"]
    elif action == "rg":
        pid = int(raw.split(":")[1])
        db.mark_recipient(occ["id"], pid)
        note = "🎁 Відмітив"
        ask_gift_for = pid
    elif action == "congrats":
        db.update_occurrence(occ["id"], congrats_done=1)
        note = "💬 Молодець!"
    elif action in ("skip", "done"):
        note = "🔕 Цього разу більше не нагадую" if action == "skip" else "✅ Готово!"
    else:
        await q.answer()
        return
    await q.answer(note)
    occ = db.get_occurrence(chat_id, obj_id)
    all_done = (not occ["needs_gift"] or occ["gift_done"]) and (not occ["needs_congrats"] or occ["congrats_done"])
    if action in ("skip", "done") or all_done:
        await finish_occurrence(context.bot, q, occ, "skipped" if action == "skip" else "done")
    else:
        text, kb = render_occ(occ, now().date())
        await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
    if ask_gift_for:
        await ask_what_gift(q.message.reply_text, context.user_data, occ, ask_gift_for)


async def cleanup_msgs(bot, occ_id, keep=None):
    """Прибирає з чату попередні нагадування про цей раз (крім keep)."""
    for chat_id, message_id in db.pop_msgs(occ_id):
        if message_id == keep:
            db.add_msg(occ_id, chat_id, message_id)
            continue
        try:
            await bot.delete_message(chat_id, message_id)
        except Exception:  # старше 48 год або вже видалене — Telegram не дасть, не страшно
            pass


async def finish_occurrence(bot, q, occ, status: str):
    """Відмічає раз виконаним, згортає це повідомлення в «що і коли зроблено»
    і прибирає з чату попередні нагадування про цей же раз."""
    db.complete_occurrence(q.message.chat_id, occ["id"], status)
    await q.edit_message_text(done_line(occ, status, now()), parse_mode=ParseMode.HTML)
    await cleanup_msgs(bot, occ["id"], keep=q.message.message_id)
    db.pop_msgs(occ["id"])


# ---------- блоки: свята зранку, підсумок увечері ----------

def holiday_block(chat_id, today: date):
    """Свята, що потрапляють у контрольні точки: подарункові — за 30/7/1 день, державні — завтра й сьогодні."""
    items = []
    for o in db.upcoming(chat_id, today + timedelta(days=31)):
        if o["recurrence"] != "holiday" or o["status"] != "active":
            continue
        days = (date.fromisoformat(o["date"]) - today).days
        gift = ua_holidays.GIFT.get(o["rule"], False)
        if days in (ua_holidays.BLOCK_DAYS_GIFT if gift else ua_holidays.BLOCK_DAYS_STATE) or (gift and days == 0
                                                                                              and not o["needs_gift"]):
            items.append((o, days))
    if not items:
        return None, None
    lines, rows = ["🎊 <b>Свята попереду</b>"], []
    for o, days in items:
        when = rel_days(days)
        line = f"• <b>{when}</b> — {escape(o['title'])} ({fmt_day_month(date.fromisoformat(o['date']))})"
        recips = db.recipients(o["id"])
        if recips:
            line += "\n   подарунки: " + " · ".join(
                f"{'✅' if r['done'] else '⬜️'} {escape(cap(r['name']))}" for r in recips)
            rows += [[InlineKeyboardButton(f"🎁 {cap(r['name'])} · {o['title'][:18]}",
                                           callback_data=f"hbrg:{o['id']}:{r['person_id']}")]
                     for r in recips if not r["done"]]
        elif ua_holidays.GIFT.get(o["rule"]):
            line += "\n   <i>кому подарунки? скажи: «на це свято подарунки мамі й дружині»</i>"
        lines.append(line)
        rows.append([InlineKeyboardButton(f"🔕 Пропустити: {o['title'][:25]}", callback_data=f"hbskip:{o['id']}")])
    return "\n".join(lines), InlineKeyboardMarkup(rows)


def digest_items(chat_id, t: datetime):
    """Що на сьогодні не відмічено (для вечірнього підсумку)."""
    today = t.date()
    out = []
    for o in db.upcoming(chat_id, today + timedelta(days=7)):
        if o["status"] != "active":
            continue
        if o["snoozed_until"] and datetime.fromisoformat(o["snoozed_until"]) > t:
            continue
        days = (date.fromisoformat(o["date"]) - today).days
        pending_gift = o["needs_gift"] and not o["gift_done"]
        pending_congrats = o["needs_congrats"] and not o["congrats_done"]
        plain = not o["needs_gift"] and not o["needs_congrats"]
        if o["recurrence"] == "holiday" and not pending_gift:
            continue
        if days == 0 and o["time"] and datetime.combine(t.date(), datetime.strptime(o["time"], "%H:%M").time(),
                                                         tzinfo=TZ) > t - timedelta(hours=1):
            continue  # щойно нагадали (або ще попереду) — у підсумок не дублюємо
        if days == 0 and (plain or pending_gift or pending_congrats):
            out.append(o)
        elif 0 < days <= 7 and pending_gift:
            out.append(o)
    return out


def digest_view(chat_id, t: datetime):
    items = digest_items(chat_id, t)
    if not items:
        return "🌙 На сьогодні все відмічено ✅", None
    lines, rows = ["🌙 <b>Сьогодні не відмічено</b>"], []
    for o in items:
        d = date.fromisoformat(o["date"])
        tail = "" if d == t.date() else f" — {fmt_day_month(d)}"
        lines.append(f"{icon(o)} {escape(cap(o['title']))}{tail}")
        if o["needs_gift"] and not o["gift_done"] and not db.recipients(o["id"]):
            act, label = "gift", "🎁"
        elif o["needs_congrats"] and not o["congrats_done"] and d == t.date():
            act, label = "congrats", "💬"
        elif not o["needs_gift"] and not o["needs_congrats"]:
            act, label = "done", "✅"
        else:
            act, label = None, None
        row = []
        if act:
            row.append(InlineKeyboardButton(f"{label} {cap(o['title'])[:22]}", callback_data=f"dg{act}:{o['id']}"))
        row.append(InlineKeyboardButton("⏰ Завтра", callback_data=f"dgsnz:{o['id']}"))
        rows.append(row)
    lines.append("\nДо ранку більше не турбую.")
    return "\n".join(lines), InlineKeyboardMarkup(rows)


async def on_block_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Кнопки у блоці свят (hb…) і вечірньому підсумку (dg…): дія + оновлення цього ж повідомлення."""
    q = update.callback_query
    await q.answer()
    if not allowed(update):
        return
    chat_id = update.effective_chat.id
    action, _, raw = q.data.partition(":")
    occ = db.get_occurrence(chat_id, int(raw.split(":")[0]))
    if not occ:
        return
    t = now()
    db.update_occurrence(occ["id"], acted_on=t.date().isoformat())
    ask_for = None
    if action in ("hbrg",):
        ask_for = int(raw.split(":")[1])
        db.mark_recipient(occ["id"], ask_for)
    elif action == "hbskip":
        db.complete_occurrence(chat_id, occ["id"], "skipped")
    elif action == "dggift":
        db.update_occurrence(occ["id"], gift_done=1)
        ask_for = occ["person_id"]
    elif action == "dgcongrats":
        db.update_occurrence(occ["id"], congrats_done=1)
    elif action == "dgdone":
        db.complete_occurrence(chat_id, occ["id"], "done")
    elif action == "dgsnz":
        until = datetime.combine(t.date() + timedelta(days=1), REMIND_AT, tzinfo=TZ)
        db.update_occurrence(occ["id"], snoozed_until=until.isoformat())
    occ = db.get_occurrence(chat_id, occ["id"])
    if occ["status"] == "active" and (not occ["needs_gift"] or occ["gift_done"]) and (
            not occ["needs_congrats"] or occ["congrats_done"]) and (occ["needs_gift"] or occ["needs_congrats"]):
        db.complete_occurrence(chat_id, occ["id"], "done")
    if occ["status"] != "active" or action == "dgdone":
        await cleanup_msgs(context.bot, occ["id"])
    if action.startswith("hb"):
        text, kb = holiday_block(chat_id, t.date())
        text = text or "🎊 Свята: усе відмічено ✅"
    else:
        text, kb = digest_view(chat_id, t)
    await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
    if ask_for:
        await ask_what_gift(q.message.reply_text, context.user_data, occ, ask_for)


# ---------- планувальник ----------

async def tick(context: ContextTypes.DEFAULT_TYPE):
    await send_due(context.bot)


def chats() -> set[int]:
    ids = set(ALLOWED_USER_IDS)
    ids |= {o["chat_id"] for o in db.active_occurrences()}
    return ids


async def send_due(bot):
    """Заводимо наступні рази подій і шлемо все, що пора (ідемпотентно)."""
    t = now()
    await ensure_commands(bot)
    db.sync_occurrences(t.date())
    for occ in db.active_occurrences():
        sent = db.sent_keys(occ["id"])
        key = decide(occ, t, sent)
        if not key:
            continue
        text, kb = render_occ(occ, t.date(), key)
        try:
            await cleanup_msgs(bot, occ["id"])  # нове нагадування замінює попереднє, а не додається
            msg = await bot.send_message(occ["chat_id"], text, parse_mode=ParseMode.HTML, reply_markup=kb)
            db.add_msg(occ["id"], occ["chat_id"], msg.message_id)
        except Exception:
            log.exception("Не вдалося надіслати нагадування occ=%s", occ["id"])
            continue
        db.mark_sent(occ["id"], key)
        log.info("Нагадування occ=%s key=%s", occ["id"], key)
    for chat_id in chats():
        try:
            await remove_old_keyboard(bot, chat_id)
            await send_blocks(bot, chat_id, t)
        except Exception:
            log.exception("Блоки для chat=%s не надіслано", chat_id)


async def send_blocks(bot, chat_id, t: datetime):
    today = t.date().isoformat()
    if after_morning(t) and db.set_flag(f"hol:{chat_id}:{today}"):
        db.seed_holidays(chat_id, t.date())
        text, kb = holiday_block(chat_id, t.date())
        if text:
            await bot.send_message(chat_id, text, parse_mode=ParseMode.HTML, reply_markup=kb)
    if t.day == 1 and after_morning(t) and db.undated_tasks(chat_id) and db.set_flag(f"nodate:{chat_id}:{t:%Y-%m}"):
        text, kb = undated_view(chat_id)
        await bot.send_message(chat_id, text, parse_mode=ParseMode.HTML, reply_markup=kb)
    if t.hour >= DIGEST_HOUR and digest_items(chat_id, t) and db.set_flag(f"dig:{chat_id}:{today}"):
        text, kb = digest_view(chat_id, t)
        await bot.send_message(chat_id, text, parse_mode=ParseMode.HTML, reply_markup=kb)


def build_app(persistence=None):
    if not TELEGRAM_TOKEN:
        raise SystemExit("Немає TELEGRAM_TOKEN — поклади його в Reminder_bot/.env (див. .env.example)")
    db.init()
    builder = ApplicationBuilder().token(TELEGRAM_TOKEN)
    if persistence:
        builder = builder.persistence(persistence)
    app = builder.build()

    app.add_handler(ConversationHandler(
        entry_points=[CommandHandler("add", add_start)],
        states={
            TITLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_title)],
            DATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_date)],
            REPEAT: [CallbackQueryHandler(add_repeat, pattern=r"^rep:")],
            NEEDS: [CallbackQueryHandler(add_needs, pattern=r"^need:")],
            NOTES: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, add_notes_text),
                CallbackQueryHandler(add_notes_skip, pattern=r"^notes:skip$"),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        name="add", persistent=persistence is not None,
    ))
    app.add_handler(CommandHandler(["start", "help"], start))
    app.add_handler(CommandHandler("list", list_cmd))
    app.add_handler(CommandHandler("all", all_cmd))
    app.add_handler(CommandHandler("today", today_cmd))
    app.add_handler(CommandHandler(["tasks", "wishes", "birthdays", "holidays"], section_cmd))
    app.add_handler(CallbackQueryHandler(
        on_button, pattern=r"^(gift|congrats|snooze|snz30|snz60|snz120|snztm|snzw|snzm|done|skip|del|delok):\d+$|^rg:\d+:\d+$"))
    app.add_handler(CallbackQueryHandler(on_block_button, pattern=r"^(hbrg:\d+:\d+|hbskip:\d+|dg(gift|congrats|done|snz):\d+)$"))
    app.add_handler(CallbackQueryHandler(on_gift_answer, pattern=r"^gi:\d+:\d+:(\d+|x)$"))
    app.add_handler(CallbackQueryHandler(on_pending, pattern=r"^pend:(save|fix|cancel)$"))
    app.add_handler(CallbackQueryHandler(on_category, pattern=r"^cat:(task|wish)$"))
    app.add_handler(CallbackQueryHandler(on_wish_done, pattern=r"^wd:\d+$"))
    app.add_handler(CallbackQueryHandler(on_nodate, pattern=r"^(nodate|ndd):\d+$"))
    app.add_handler(CallbackQueryHandler(on_edit_event, pattern=r"^evedit:\d+$"))
    app.add_handler(CallbackQueryHandler(on_meeting_button, pattern=r"^(who:(\d+|x)|slot:\d+)$"))
    app.add_handler(CallbackQueryHandler(on_meeting_pick, pattern=r"^mpick:(\d+|x)$"))
    app.add_handler(CallbackQueryHandler(on_agenda_button, pattern=r"^ag(mv|del):\d+$"))
    app.add_handler(MessageHandler(filters.Regex("^(" + "|".join(MENU) + ")$"), on_menu))
    # після ConversationHandler: текст усередині /add іде в діалог, а не сюди
    app.add_handler(MessageHandler((filters.TEXT & ~filters.COMMAND) | filters.VOICE | filters.AUDIO, free_input))
    return app


def main():
    """Постійний процес (VPS / локально)."""
    app = build_app()
    app.job_queue.run_repeating(tick, interval=TICK_SECONDS, first=5)
    app.run_polling()


if __name__ == "__main__":
    main()
