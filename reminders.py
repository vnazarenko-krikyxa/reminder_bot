"""Решение «что и когда отправить» (чистая логика, без Telegram) и текст напоминания.

Интерфейс — украинский; название события показываем как его сказал пользователь.
"""
import calendar
from datetime import date, datetime, time, timedelta, timezone

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from config import (
    DIGEST_HOUR, EVENING_HOUR, GIFT_NAG_DAYS, REMIND_AT, REMIND_OFFSETS, REPEAT_EVERY, REPEAT_FROM_HOUR,
    REPEAT_MAX,
)
from dates import fmt_day_month, plural_days, plural_years

PRE_HOURS = 2  # за сколько часов до события со временем напомнить в сам день

ICONS = {"birthday": "🎉", "anniversary": "💞", "holiday": "🎊", "task": "📌", "event": "🎫", "wish": "🌠"}
SNOOZE = {"snz30": ("30 хв", timedelta(minutes=30)), "snz120": ("2 год", timedelta(hours=2)),
          "snztm": ("Завтра", None), "snzw": ("тиждень", timedelta(days=7))}
OLD_SNOOZE = ("snooze", "snz60", "snzm")  # кнопки в старих повідомленнях — ще обробляємо
# Ключи «сегодняшних» напоминаний: после них, если никак не отреагировал, бот повторяет
REPEATABLE = ("d0", "at")


def rolled(occ) -> bool:
    return bool(occ["rolled"]) if "rolled" in occ.keys() else False


def cap(s: str) -> str:
    return s[:1].upper() + s[1:]


def icon(e) -> str:
    kind = e["kind"] if "kind" in e.keys() else None
    if kind in ICONS:
        return ICONS[kind]
    return "🔁" if e["recurrence"] in ("yearly", "weekly", "monthly") else "📌"


def event_at(occ, tz):
    """Момент события со временем (или None для событий на весь день)."""
    if not occ["time"]:
        return None
    return datetime.combine(date.fromisoformat(occ["date"]), time.fromisoformat(occ["time"]), tzinfo=tz)


def utc_to_local(stamp: str, tz) -> datetime:
    # SQLite пишет datetime('now') в UTC без зоны
    return datetime.fromisoformat(stamp).replace(tzinfo=timezone.utc).astimezone(tz)


def after_morning(now: datetime) -> bool:
    return now.time() >= REMIND_AT


def decide(occ, now: datetime, sent):
    """Возвращает ключ напоминания, которое пора отправить, или None.

    sent — {ключ: когда отправлен (aware datetime | None)} из sent_log (set тоже годится).
    Ключ пишется в sent_log, поэтому повторный вызов не продублирует отправку.
    """
    if isinstance(sent, (set, frozenset)):
        sent = {k: None for k in sent}
    today = now.date()
    occ_date = date.fromisoformat(occ["date"])
    days = (occ_date - today).days
    pending_gift = occ["needs_gift"] and not occ["gift_done"]
    pending_congrats = occ["needs_congrats"] and not occ["congrats_done"]
    plain = not occ["needs_gift"] and not occ["needs_congrats"]  # «просто напомнить»
    rec = occ["recurrence"]

    if not (pending_gift or pending_congrats or plain):
        return None  # всё сделано
    if rec == "holiday" and not (days == 0 and pending_gift) and not occ["snoozed_until"]:
        return None  # праздники идут отдельным блоком; сами — только «сегодня, подарки не все»

    # Отложено — молчим до указанного времени, потом одно «повторное» напоминание
    snoozed_today = False
    if occ["snoozed_until"]:
        until = datetime.fromisoformat(occ["snoozed_until"])
        if now < until:
            return None
        key = f"snooze:{occ['snoozed_until']}"
        if key not in sent:
            return key
        # повторное уже ушло — дальше обычное расписание, но в этот день только вечернее / точное
        snoozed_today = until.date() == today

    # перенесена невиконана справа — нагадуємо зранку, без прив'язки до старого часу
    at = None if rolled(occ) else event_at(occ, now.tzinfo)
    # Для задач со временем не шлём «через N дней», если N дней назад задачи ещё не было:
    # «завтра в 9 позвонить» не должно сразу превращаться в «Завтра: позвонить».
    lead = (occ_date - utc_to_local(occ["created_at"], now.tzinfo).date()).days if at else None
    morning = after_morning(now)

    candidates = []
    if days == 0:
        if at:
            if now >= at:
                candidates.append("at")
            elif lead >= 2 and now >= at - timedelta(hours=PRE_HOURS):
                candidates.append("pre")
        elif morning:
            candidates.append("d0")
        if pending_congrats and now.hour >= EVENING_HOUR and (not at or now >= at):
            candidates.append("d0:evening")
    elif days == -1:
        if pending_congrats and morning:
            candidates.append("late")
    elif days > 0 and morning and rec not in ("weekly", "monthly", "holiday"):
        fresh = at is not None and days >= lead
        if days in REMIND_OFFSETS and (pending_gift or plain or days <= 1) and not fresh:
            candidates.append(f"d{days}")
        elif pending_gift and days <= GIFT_NAG_DAYS:
            candidates.append(f"nag:{today.isoformat()}")

    if snoozed_today:
        candidates = [k for k in candidates if k in ("d0:evening", "at")]
    for key in candidates:
        if key not in sent:
            return key
    return repeat_key(occ, now, sent)


def repeat_key(occ, now: datetime, sent: dict):
    """Повтор «сегодняшнего» напоминания, если на него не отреагировали:
    каждые REPEAT_EVERY, до REPEAT_MAX раз (для «подарок не куплен» — 1 раз), с REPEAT_FROM_HOUR
    и до вечернего итога. Любая кнопка по этому событию сегодня (acted_on) — повторов нет."""
    today = now.date()
    if occ["acted_on"] == today.isoformat():
        return None
    if not (REPEAT_FROM_HOUR <= now.hour < DIGEST_HOUR):
        return None
    sent_today = {k: t for k, t in sent.items() if t is not None and t.date() == today}
    base = [k for k in sent_today if k in REPEATABLE or k.startswith("snooze:")]
    nag = f"nag:{today.isoformat()}"
    if not base and nag not in sent_today:
        return None
    limit = REPEAT_MAX if base else 1
    done = [k for k in sent_today if k.startswith(f"rep:{today.isoformat()}:")]
    if len(done) >= limit:
        return None
    if now - max(sent_today.values()) < REPEAT_EVERY:
        return None
    return f"rep:{today.isoformat()}:{len(done) + 1}"


def render(occ, today: date, key: str | None = None, extra_lines=(), extra_rows=()):
    """Текст и кнопки напоминания / карточки события."""
    occ_date = date.fromisoformat(occ["date"])
    days = (occ_date - today).days
    tm = occ["time"]
    if key == "at":
        head = "⏰ <b>Час!</b>"
    elif key == "pre":
        head = f"⏳ <b>Через {PRE_HOURS} години</b>"
    elif days == 0:
        head = f"📅 <b>Сьогодні в {tm}</b>" if tm else "🔔 <b>Сьогодні!</b>"
    elif days == 1:
        head = "⏰ <b>Завтра</b>"
    elif days == -1:
        head = "⚠️ <b>Вчора було</b>"
    elif days > 0:
        head = f"📅 <b>Через {plural_days(days)}</b>"
    else:
        head = f"📅 <b>{plural_days(days)} тому</b>"
    if days == 0 and rolled(occ):
        head = "⏪ <b>Не виконано — нагадую сьогодні</b>"
    if key and key.startswith("rep:"):
        head = "🔁 <b>Нагадую ще раз</b> · " + head

    extra = ""
    if occ["recurrence"] == "yearly" and occ["year"] and occ["year"] < occ_date.year:
        n = occ_date.year - occ["year"]
        extra = f" — {'виповнилось' if days < 0 else 'виповниться'} {plural_years(n)}"
    when = fmt_day_month(occ_date) + (f" в {tm}" if tm else "")
    lines = [head, f"{icon(occ)} <b>{escape(cap(occ['title']))}</b>, {when}{extra}"]

    if occ["needs_gift"] and not extra_rows:
        lines.append(("✅" if occ["gift_done"] else "⬜️") + " подарунок")
    if occ["needs_congrats"]:
        lines.append(("✅" if occ["congrats_done"] else "⬜️") + " привітати")
    lines += list(extra_lines)
    if occ["notes"]:
        lines.append(f"📝 {escape(occ['notes'])}")
    if occ["status"] == "skipped":
        lines.append("🔕 пропущено цього року")
    elif occ["status"] == "done":
        lines.append("✅ зроблено")

    kb = keyboard(occ, days, extra_rows)
    if advance(key) and not (occ["needs_gift"] or occ["needs_congrats"]):
        kb = None  # «Завтра» / «Через N днів» — просто попередження; кнопки прийдуть у сам день
    return "\n".join(lines), kb


def advance(key: str | None) -> bool:
    """Попереднє нагадування «за N днів» (d1, d3, d7…), а не в сам день."""
    return bool(key) and key[0] == "d" and key[1:].isdigit() and int(key[1:]) > 0


def keyboard(occ, days, extra_rows=()):
    if occ["status"] != "active":
        return None
    oid = occ["id"]
    plain = not (occ["needs_gift"] or occ["needs_congrats"])
    rows = [list(r) for r in extra_rows]
    done_row = []
    if occ["needs_gift"] and not occ["gift_done"] and not extra_rows:
        done_row.append(InlineKeyboardButton("🎁 Подарунок куплено", callback_data=f"gift:{oid}"))
    if occ["needs_congrats"] and not occ["congrats_done"] and days <= 1:
        done_row.append(InlineKeyboardButton("💬 Привітав", callback_data=f"congrats:{oid}"))
    if plain:
        done_row.append(InlineKeyboardButton("✅ Зроблено", callback_data=f"done:{oid}"))
    if done_row:
        rows.append(done_row)
    rows.append([InlineKeyboardButton(f"⏰ {label}", callback_data=f"{code}:{oid}")
                 for code, (label, _) in SNOOZE.items()])
    if occ["recurrence"] in ("yearly", "holiday", "weekly", "monthly"):
        label = "🔕 Пропустити цього року" if occ["recurrence"] in ("yearly", "holiday") else "🔕 Пропустити цей раз"
        rows.append([InlineKeyboardButton(label, callback_data=f"skip:{oid}")])
    return InlineKeyboardMarkup(rows)


def snooze_until(occ, now: datetime, code: str) -> datetime:
    """Отложить: snz30 / snz120 / snzw (неделя) / snzm (месяц; 31.01 -> 28/29.02)."""
    now = now.replace(second=0, microsecond=0)
    if code == "snztm":
        return datetime.combine(now.date() + timedelta(days=1), REMIND_AT, tzinfo=now.tzinfo)
    if code == "snzm":
        y, m = (now.year + 1, 1) if now.month == 12 else (now.year, now.month + 1)
        return now.replace(year=y, month=m, day=min(now.day, calendar.monthrange(y, m)[1]))
    if code in SNOOZE:
        return now + SNOOZE[code][1]
    if code == "snz60":  # кнопка из старых сообщений
        return now + timedelta(hours=1)
    days = (date.fromisoformat(occ["date"]) - now.date()).days
    if days <= 0:
        return now + timedelta(hours=3)
    tomorrow = now.date() + timedelta(days=1)
    return datetime.combine(tomorrow, REMIND_AT, tzinfo=now.tzinfo)


def escape(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def done_line(occ, status: str, at: datetime) -> str:
    """Две строки вместо карточки после выполнения: что — и когда сделано."""
    what = f"{icon(occ)} <b>{escape(cap(occ['title']))}</b>"
    stamp = f"{at:%d.%m} в {at:%H:%M}"
    if status == "skipped":
        return f"{what}\n🔕 Пропущено {stamp}"
    parts = []
    if occ["needs_gift"]:
        parts.append("подарунок куплено")
    if occ["needs_congrats"]:
        parts.append("привітав")
    done = cap(", ".join(parts)) if parts else "Виконано"
    return f"{what}\n✅ {done} {stamp}"


def snoozed_line(occ, until: datetime) -> str:
    return f"{icon(occ)} <b>{escape(cap(occ['title']))}</b>\n⏰ Нагадаю {until:%d.%m} в {until:%H:%M}"
