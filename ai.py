"""Голос -> текст и текст -> действие со списком событий (любая LLM, см. llm.py)."""
import asyncio
import calendar
import json
import logging
import re
from datetime import date, datetime, timedelta

import httpx

import llm
import meetings
import work
from dates import next_yearly, occurrence_date

log = logging.getLogger("reminder_bot.ai")

EVENT_RULES = """Событие — JSON-объект:
{"title": str, "day": int, "month": int, "year": int|null, "time": "HH:MM"|null,
 "recurrence": "yearly"|"once"|"weekly"|"monthly",
 "kind": "birthday"|"anniversary"|"holiday"|"task"|"event"|"wish", "kind_sure": bool, "person": str|null,
 "turns_age": int|null, "needs_gift": bool, "needs_congrats": bool, "notes": str|null}
Правила для события:
- title — коротко и СТРОГО на языке самой фразы, не переводи: русская фраза -> русское название
  («Оплатить интернет», «ДР мамы»), украинская -> украинское («Зателефонувати в банк»).
- Дни рождения, годовщины, праздники — "yearly"; встречи, концерты, дедлайны — "once".
  «Каждую неделю», «каждый понедельник» — "weekly" (дата — ближайший такой день);
  «каждый месяц», «каждое 5-е число» — "monthly" (дата — ближайшее такое число).
- person — чей это день рождения/годовщина/праздник, в именительном падеже, как человека называет пользователь
  («у мамы» -> "мама", «брату» -> "брат", «Лены» -> "Лена"), иначе null. Если человек уже есть в списке людей
  ниже — пиши его имя ровно как в списке.
- kind: wish — мечта/желание, которое хотелось бы когда-нибудь осуществить («подняться на гору»,
  «побывать в Японии», «научиться играть на гитаре»); у wish дата не обязательна — если не названа, day и month null.
  task — конкретное дело, которое надо сделать/купить/выполнить («поставить коронки», «купить ленту»).
  kind_sure — false, если непонятно, это дело (task) или мечта (wish); иначе true.
  Если дата НЕ названа и фраза может быть желанием («купить велосипед», «съездить к морю») — kind wish,
  kind_sure false (пользователь сам выберет раздел), а не error.
  birthday — день рождения; anniversary — годовщина; holiday — праздник (Новый год, 8 марта…);
  task — дело/задача («позвонить», «купить», «оплатить»); event — мероприятие (концерт, приезд, поход).
- year: для yearly — год рождения/начала, только если он назван прямо; иначе null.
  Для once — год события (если не назван — ближайшая будущая такая дата).
- turns_age: если сказано, сколько исполнится («исполнится 30», «юбилей 60») — это число, иначе null.
  Год рождения из возраста НЕ вычисляй.
- «завтра», «через неделю», «в следующую пятницу», «через 2 часа» считай от текущих даты и времени.
- Названо только время без даты («напомни в 20:00 …») — это сегодня, а если это время сегодня
  уже прошло — завтра.
- time — время, если оно названо («в 20:00», «в восемь вечера» -> "20:00", «через 2 часа»), иначе null.
  Время пиши в time, а не в notes. «Утром/вечером» без часа — null.
  Час от 1 до 7 без «утра»/«ночи» — это день/вечер: «в 4» -> "16:00", «в 7» -> "19:00»; «в 8», «в 9» — утро.
- «Напомни …», «надо …», «не забыть …» — это событие-задача: title — сама задача с большой буквы
  («Купить плитку»), recurrence "once", если не сказано «каждый год».
- needs_gift — если упомянут подарок/купить; needs_congrats — если это чей-то праздник,
  который надо поздравить (ДР, годовщина, именины). Для концертов/встреч оба false.
- notes — полезные детали (идеи подарка, что любит, адрес), иначе null.
  Не пиши в notes то, что уже есть в других полях (сам факт «нужен подарок», дату)."""

INTERPRET = """Ты — личный помощник-напоминалка в Telegram. Человек пишет или надиктовывает голосом что угодно
своими словами (по-русски или по-украински; в голосе бывают ошибки распознавания — угадывай смысл).
Пойми, чего он хочет, и верни ТОЛЬКО JSON с действием. Сейчас {now}.
Календарь на ближайшие дни (даты для «в среду», «до пятницы» бери отсюда): {calendar}
Недавний разговор (помогает понять продолжение вроде «и на завтра тоже», «нет, в 6»):
{history}
Сохранённые события пользователя (id -> событие):
{events}
Люди пользователя (близкие, кому дарят подарки): {people}

{rules}

Как решать (по порядку):
1. Фраза про событие, которое уже есть в списке? Человек сообщает, что оно сделано (done), просит его
   убрать (delete) или поменять — перенести, переименовать, другое время (update). Учитывай недавний
   разговор: «нет, в 19» сразу после записи — это update только что записанного.
2. Смысл фразы явно о другом действии ниже (не на работе, рабочий календарь, идея подарка, показать
   списки или расписание)? — это действие.
3. Всё остальное — записать как новое напоминание (add). Почти любая фраза о деле, покупке, звонке,
   поездке, визите, встрече, дедлайне, о том, что нельзя забыть, — это add, даже без слова «напомни»
   и даже если сказано коротко («фриланс в 19:30», «шиномонтаж завтра»).
Не переспрашивай то, что можно разумно предположить (нет времени — напоминание на весь день; нет даты
у дела — дело без даты). Не выдумывай того, чего не было сказано.

Действия:
- add — новое напоминание/событие: {{"action": "add", "event": <событие>}}
- update — изменить событие из списка (перенести, переименовать, убрать дату, поменять время):
  {{"action": "update", "event_id": <id>, "event": <событие целиком; что не меняли — скопируй из списка>}}
  Без даты: day и month null.
- done — человек сообщает, что дело из списка уже сделано: {{"action": "done", "event_id": <id>}}
- delete — человек просит удалить/убрать событие из списка или больше о нём не напоминать
  (укр. «видали», «прибери» = удали, убери): {{"action": "delete", "event_id": <id>}}
- idea — идея подарка для человека, без даты: {{"action": "idea", "person": str, "text": str}}
  (person в именительном падеже, text — идея на языке фразы).
- recipients — кому дарить подарки на праздник/событие из списка:
  {{"action": "recipients", "event_id": <id>, "people": [str]}}
- person_info — что известно о человеке (события, идеи, что дарили): {{"action": "person_info", "person": str}}
- reminders — показать сохранённые напоминания: {{"action": "reminders", "days": int|null}} (days — если назван срок).
- agenda — показать рабочий Google-календарь (встречи, расписание):
  {{"action": "agenda", "week": "this"|"next"|"7d"|null, "date": "YYYY-MM-DD"|null, "date_to": "YYYY-MM-DD"|null}}
  «эта неделя» -> week "this", «следующая неделя» -> "next", «на неделю/7 дней вперёд» -> "7d" (даты тогда null);
  иначе date — день или начало периода, date_to — конец; ничего не сказано — всё null.
- absence — человек говорит о себе, что в рабочее время его не будет или он отойдёт: перерыв, отойду,
  начну позже, уйду раньше, отгул, болею (бот покажет письмо команде и «Не на місці» на подтверждение):
  {{"action": "absence", "absence": {{"kind": "pause"|"late_start"|"early_finish"|"day_off"|"sick",
    "date": "YYYY-MM-DD", "date_to": "YYYY-MM-DD"|null, "from": "HH:MM"|null, "to": "HH:MM"|null}}}}
  pause — отойдёт на время и вернётся («с 14 до 15 отойду»: from и to, один день); late_start — начнёт работать позже (from);
  early_finish — закончит раньше (to); day_off — отгул/выходной (date, несколько дней — date_to);
  sick — болеет (date, date_to). Дата не названа — сегодня; несказанное время — null.
- meeting — забронировать встречу/созвон в РАБОЧЕМ Google-календаре с приглашением коллег:
  {{"action": "meeting", "meeting": {{"title": str|null, "date": "YYYY-MM-DD"|null, "time": "HH:MM"|null,
    "duration_min": int|null, "attendees": [str], "meet": bool, "notes": str|null}}}}
  attendees — с кем, в именительном падеже, или e-mail; себя не добавляй. title — тема, если названа.
  meet — false только для офлайн-встречи. Встреча с родными, друзьями, клиентом, врачом, личные дела —
  это НЕ meeting, а add. Если не уверен, что нужно бронировать рабочий календарь, — add.
- meeting_change — отменить или перенести уже назначенную встречу в рабочем календаре (их нет в списке выше):
  {{"action": "meeting_change", "change": "cancel"|"move",
    "find": {{"attendees": [str], "date": "YYYY-MM-DD"|null, "time": "HH:MM"|null, "title": str|null}},
    "new": {{"date": "YYYY-MM-DD"|null, "time": "HH:MM"|null, "duration_min": int|null}}}}
  find — только сказанные признаки; new — только для move.
- error — только если из фразы правда нельзя понять, что сделать или какое событие имеется в виду:
  {{"action": "error", "error": "один короткий вопрос ПО-УКРАИНСКИ", "candidates": [<id подходящих событий>]}}

События из списка ищи по смыслу («мамин день рождения» = «ДР мамы»); подходит несколько — error с candidates.
День рождения или годовщина без даты — error (спроси дату).
{target}
Фраза: {text}"""

TARGET = """Пользователь сейчас правит событие id={id}: фраза относится к нему (update, delete или done)."""

PARTIAL_FIX = """Ты только что спросил пользователя: «{question}» — по незавершённому действию "{action}": {data}
Если фраза — ответ или уточнение, верни это действие целиком в том же формате, что в списке действий,
с учётом ответа (что не упомянуто — оставь как было). Если фраза — другая, новая просьба (или человек
передумал: «просто напомни», «это не встреча»), обработай её как новую — любое действие."""

DRAFT = """Ты помощник-напоминалка. Сейчас {now}.
Календарь на ближайшие дни: {calendar}
{rules}

Это ИСПРАВЛЕНИЕ ещё не сохранённого черновика. Верни ТОЛЬКО JSON — черновик целиком
с учётом исправления: {{"action": "add", "event": <событие>}}
Текущий черновик: {draft}
Исправление: {text}"""


class AIError(Exception):
    pass



async def transcribe(audio: bytes, filename: str) -> str:
    try:
        return await llm.transcribe(audio, filename)
    except (llm.LLMError, httpx.HTTPError) as e:
        raise AIError(str(e) if isinstance(e, llm.LLMError) else "не вдалося розпізнати голос")


WEEKDAYS = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
WEEKDAYS_UA = ["понеділок", "вівторок", "середа", "четвер", "п'ятниця", "субота", "неділя"]
# День недели во фразе (рус/укр, любой падеж) -> 0..6
WEEKDAY_RE = [
    r"понедельник|понеділ(ок|ка|ку)",
    r"вторник|вівтор(ок|ка)",
    r"\bсред[аыу]\b|\bсеред[аиу]\b",
    r"четверг|\bчетвер(а|у)?\b",
    r"пятниц|п['’ʼ`]?ятниц",
    r"суббот|субот",
    r"воскресен|\bнеділ[яюі]\b",
]


def weekday_in(text: str) -> int | None:
    """Номер дня недели, если во фразе назван ровно один день недели."""
    found = {i for i, rx in enumerate(WEEKDAY_RE) if re.search(rx, text.lower())}
    return found.pop() if len(found) == 1 else None


def fix_weekday(r: dict, text: str, now: datetime) -> dict:
    """Модель путает дни недели (особенно украинские): если во фразе назван один день недели,
    а дата выпала на другой — ставим ближайший такой день (сегодняшний — через неделю)."""
    wd = weekday_in(text)
    if wd is None:
        return r
    today = now.date()
    nearest = today + timedelta(days=(wd - today.weekday()) % 7 or 7)

    def fixed(d: date) -> date:
        return d if d.weekday() == wd else nearest

    ev = r.get("event")
    if r["action"] in ("add", "update") and ev and ev.get("day") and ev["recurrence"] in ("once", "weekly"):
        d = fixed(date(ev["year"], ev["month"], ev["day"]))
        ev.update(day=d.day, month=d.month, year=d.year)
    elif r["action"] == "meeting" and r["meeting"].get("date"):
        r["meeting"]["date"] = fixed(date.fromisoformat(r["meeting"]["date"])).isoformat()
    elif r["action"] == "absence" and not r["absence"].get("date_to"):
        r["absence"]["date"] = fixed(date.fromisoformat(r["absence"]["date"])).isoformat()
    elif r["action"] == "agenda" and r.get("date") and not r.get("date_to"):
        r["date"] = fixed(date.fromisoformat(r["date"])).isoformat()
    elif r["action"] == "meeting_change":
        for part in ("find", "new"):
            if r["change"][part].get("date"):
                r["change"][part]["date"] = fixed(date.fromisoformat(r["change"][part]["date"])).isoformat()
                break
    return r


async def interpret(text: str, now: datetime, events: dict, target_id: int | None = None,
                    draft: dict | None = None, partial: dict | None = None, people: list | None = None,
                    history: list | None = None) -> dict:
    """Что сделать со списком событий.

    events — {id: событие в формате черновика}. Возвращает один из вариантов:
    {"action": "add", "event": ev} · {"action": "update", "event_id": id, "event": ev}
    · {"action": "delete"|"done", "event_id": id} · {"action": "error", "error": str, "candidates": [id]}
    """
    stamp = f"{now:%Y-%m-%d %H:%M}, {WEEKDAYS[now.weekday()]}"
    cal = ", ".join(
        f"{WEEKDAYS[d.weekday()]}/{WEEKDAYS_UA[d.weekday()]} {d.isoformat()}"
        for d in (now.date() + timedelta(days=i) for i in range(15))
    )
    if draft:
        prompt = DRAFT.format(now=stamp, calendar=cal, rules=EVENT_RULES,
                              draft=json.dumps(draft, ensure_ascii=False), text=text)
    else:
        listing = "\n".join(
            f"{i}: " + json.dumps({"title": e["title"], "holiday": True} if e["recurrence"] == "holiday" else e,
                                  ensure_ascii=False)
            for i, e in events.items()
        ) or "(пусто)"
        prompt = INTERPRET.format(
            now=stamp, calendar=cal, events=listing, rules=EVENT_RULES,
            people=", ".join(people or []) or "(пока нет)",
            history="\n".join(f"— {h}" for h in history or []) or "(нет)",
            target=(PARTIAL_FIX.format(action=partial["action"], question=partial.get("question") or "уточни",
                                       data=json.dumps(partial["data"], ensure_ascii=False))
                    if partial else TARGET.format(id=target_id) if target_id else ""),
            text=text,
        )
    r = await _ask(prompt, lambda d: validate_action(d, now, events, draft is not None))
    wd = weekday_in(text)
    if r["action"] == "error" and wd == now.weekday() and not draft and not partial:
        # «у понеділок о 10», сказане в понеділок о 12, — це наступний понеділок, а не «вже минуло»
        nxt = now.date() + timedelta(days=7)
        r = await _ask(prompt + f"\nНазванный день недели — сегодня, но имеется в виду следующий: {nxt.isoformat()}.",
                       lambda d: validate_action(d, now, events, draft is not None))
    r = fix_weekday(r, text, now)
    return r


async def _ask(prompt: str, check):
    """Питаємо модель (будь-якого провайдера) і перевіряємо JSON; не вийшло — наступна модель."""
    last = None
    try:
        async for content in llm.chat_json(prompt):
            try:
                return check(llm.extract_json(content))
            except (ValueError, KeyError, TypeError) as e:
                last = str(e)
    except llm.LLMError as e:
        raise AIError(str(e))
    except httpx.HTTPError as e:
        last = repr(e)
    log.error("LLM parse failed: %s", last)
    raise AIError("не вдалося розібрати фразу")


def validate_action(d: dict, now: datetime, events: dict, draft_mode: bool) -> dict:
    action = d.get("action")
    if action == "error" or d.get("error"):
        cands = [int(i) for i in d.get("candidates") or [] if int(i) in events]
        return {"action": "error", "error": str(d.get("error") or "не зрозумів"), "candidates": cands}
    if draft_mode:
        action = "add"
    if action == "absence":
        a = work.validate(d["absence"], now)
        if "error" in a:
            return {"action": "error", "error": a["error"], "candidates": [],
                    "partial": {"action": "absence", "data": d["absence"]}}
        return {"action": "absence", "absence": a}
    if action == "idea":
        person, idea = str(d.get("person") or "").strip(), str(d.get("text") or "").strip()
        if not person or not idea:
            return {"action": "error", "error": "для кого ідея і що саме?", "candidates": []}
        return {"action": "idea", "person": person[:60], "text": idea[:300]}
    if action == "recipients":
        eid = int(d["event_id"]) if d.get("event_id") is not None else None
        names = [str(x).strip()[:60] for x in d.get("people") or [] if str(x).strip()]
        if eid not in events:
            return {"action": "error", "error": "не знайшов таке свято чи подію", "candidates": []}
        if not names:
            return {"action": "error", "error": "кому саме подарунки?", "candidates": []}
        return {"action": "recipients", "event_id": eid, "people": names}
    if action == "person_info":
        person = str(d.get("person") or "").strip()
        if not person:
            return {"action": "error", "error": "про кого саме?", "candidates": []}
        return {"action": "person_info", "person": person[:60]}
    if action == "reminders":
        days = int(d["days"]) if d.get("days") else None
        return {"action": "reminders", "days": days if days and days > 0 else None}
    if action == "agenda":
        d1 = date.fromisoformat(d["date"]) if d.get("date") else None
        d2 = date.fromisoformat(d["date_to"]) if d.get("date_to") else None
        today = now.date()
        sunday = today + timedelta(days=6 - today.weekday())
        if d.get("week") == "this":
            d1, d2 = today, sunday
        elif d.get("week") == "next":
            d1, d2 = sunday + timedelta(days=1), sunday + timedelta(days=7)
        elif d.get("week") == "7d":
            d1, d2 = today, today + timedelta(days=6)
        if d1 and d2 and d2 < d1:
            d1, d2 = d2, d1
        if d1 and d2 and (d2 - d1).days > 13:
            d2 = d1 + timedelta(days=13)
        return {"action": "agenda", "date": d1.isoformat() if d1 else None,
                "date_to": d2.isoformat() if d2 else None}
    if action == "meeting_change":
        c = meetings.validate_change(d, now)
        if "error" in c:
            return {"action": "error", "error": c["error"], "candidates": []}
        return {"action": "meeting_change", "change": c}
    if action == "meeting" and not [a for a in d["meeting"].get("attendees") or [] if str(a).strip()]:
        action, d = "add", {"event": meeting_event(d["meeting"], now)}  # «зустріч» без колег — нагадування
    if action == "meeting":
        m = meetings.validate(d["meeting"], now)
        if "error" in m:
            return {"action": "error", "error": m["error"], "candidates": [],
                    "partial": {"action": "meeting", "data": d["meeting"]}}
        return {"action": "meeting", "meeting": m}
    if action in ("update", "delete", "done"):
        eid = int(d["event_id"])
        if eid not in events:
            return {"action": "error", "error": "не знайшов таку подію", "candidates": []}
        if action in ("delete", "done"):
            return {"action": action, "event_id": eid}
        ev = validate(d["event"], now)
        if "error" in ev:
            return {"action": "error", "error": ev["error"], "candidates": []}
        return {"action": "update", "event_id": eid, "event": ev}
    if action != "add":
        raise ValueError(f"неизвестное действие {action!r}")
    ev = validate(d["event"], now)
    if "error" in ev:
        return {"action": "error", "error": ev["error"], "candidates": [],
                "partial": {"action": "add", "data": d["event"]}}
    return {"action": "add", "event": ev}


def meeting_event(m: dict, now: datetime) -> dict:
    """Зустріч → звичайна подія-нагадування (коли це не робоча зустріч у календарі)."""
    d = date.fromisoformat(m.get("date") or now.date().isoformat())
    who = ", ".join(str(a) for a in m.get("attendees") or [])
    title = m.get("title") or (f"Зустріч: {who}" if who else "Зустріч")
    return {"title": title, "day": d.day, "month": d.month, "year": d.year, "time": m.get("time"),
            "recurrence": "once", "kind": "event", "kind_sure": True, "person": None,
            "needs_gift": False, "needs_congrats": False, "notes": m.get("notes")}


def validate(d: dict, now: datetime) -> dict:
    """Приводит событие от модели к нашему формату; ValueError, если данных не хватает."""
    today = now.date()
    if d.get("error"):
        return {"error": str(d["error"])}
    kind_sure = d.get("kind_sure") is not False
    undated = d.get("day") in (None, 0) or d.get("month") in (None, 0)
    if undated and d.get("recurrence") not in ("weekly", "monthly") and (
            d.get("kind") in ("wish", "task", "event", None) or d.get("recurrence") == "someday"):
        # без дати: мрія «колись» або справа без дати (про справи нагадую списком 1-го числа)
        title = str(d["title"]).strip()[:200]
        if not title:
            raise ValueError("порожня назва")
        kind = "wish" if d.get("kind") == "wish" else "task" if d.get("kind") != "event" else "event"
        return {"title": title, "day": 0, "month": 0, "year": None, "time": None, "kind": kind,
                "recurrence": "someday", "person": None, "needs_gift": False, "needs_congrats": False,
                "notes": (str(d["notes"]).strip()[:1000] or None) if d.get("notes") else None,
                "kind_sure": kind_sure}
    ev = {
        "title": str(d["title"]).strip()[:200],
        # у «щомісяця 5-го» модель інколи не вказує місяць — беремо поточний (найближчу дату порахує db)
        "day": int(d["day"] if d.get("day") is not None else now.day),
        "month": int(d["month"] if d.get("month") is not None else now.month),
        "year": int(d["year"]) if d.get("year") else None,
        "time": parse_time(d.get("time")),
        "kind": d.get("kind") if d.get("kind") in ("birthday", "anniversary", "holiday", "task", "event", "wish") else None,
        "kind_sure": kind_sure,
        "recurrence": d.get("recurrence") if d.get("recurrence") in ("once", "weekly", "monthly") else "yearly",
        "person": (str(d["person"]).strip()[:60] or None) if d.get("person") else None,
        "needs_gift": bool(d.get("needs_gift")),
        "needs_congrats": bool(d.get("needs_congrats")),
        "notes": (str(d["notes"]).strip()[:1000] or None) if d.get("notes") else None,
    }
    if not ev["title"]:
        raise ValueError("порожня назва")
    if ev["kind"] == "wish":
        ev["recurrence"] = "once"  # мрія з датою — разова
    if ev["kind"] == "birthday" and ev["recurrence"] == "once":
        ev["recurrence"] = "yearly"  # дни рождения всегда каждый год
        ev["year"] = None if ev["year"] and ev["year"] >= today.year else ev["year"]
    date(ev["year"] or 2000, ev["month"], ev["day"])  # проверка даты
    if ev["recurrence"] in ("weekly", "monthly"):
        # якір повтору; якщо він у минулому — db.next_date сам знайде найближчий раз
        ev["year"] = ev["year"] or today.year
        anchor = occurrence_date(ev["month"], ev["day"], ev["year"])
        if anchor == today and ev["time"] and ev["time"] <= f"{now:%H:%M}":
            # «щопонеділка о 10», сказане в понеділок о 12, — перший раз наступного тижня/місяця
            if ev["recurrence"] == "weekly":
                nxt = anchor + timedelta(days=7)
                ev["day"], ev["month"], ev["year"] = nxt.day, nxt.month, nxt.year
            else:
                y, m = today.year, today.month
                while True:  # наступний місяць, де це число існує (31-е — не в кожному)
                    y, m = (y + 1, 1) if m == 12 else (y, m + 1)
                    if ev["day"] <= calendar.monthrange(y, m)[1]:
                        break
                ev["month"], ev["year"] = m, y
    elif ev["recurrence"] == "once":
        if ev["year"] is None:
            d0 = occurrence_date(ev["month"], ev["day"], today.year)
            ev["year"] = today.year if d0 >= today else today.year + 1
        when = date(ev["year"], ev["month"], ev["day"])
        if when < today:
            return {"error": "ця дата вже минула"}
        if when == today and ev["time"] and ev["time"] < f"{now:%H:%M}":
            return {"error": f"сьогодні {ev['time']} вже минуло"}
    else:
        if d.get("turns_age"):
            # «исполнится N» относится к ближайшему такому дню
            ev["year"] = next_yearly(ev["month"], ev["day"], today).year - int(d["turns_age"])
        if ev["year"] and ev["year"] > today.year:
            ev["year"] = None  # год рождения из будущего — модель ошиблась
    return ev


def parse_time(v) -> str | None:
    """'20:00', '8:05', '20.00' -> 'HH:MM'; всё прочее -> None."""
    if not v:
        return None
    parts = str(v).strip().replace(".", ":").split(":")
    try:
        h, m = int(parts[0]), int(parts[1]) if len(parts) > 1 else 0
    except ValueError:
        return None
    return f"{h:02d}:{m:02d}" if 0 <= h < 24 and 0 <= m < 60 else None


MATCH = """Список сотрудников компании (номер: имя <email>):
{people}

Для каждого из запрошенных имён найди подходящих сотрудников. Учитывай, что имя могло быть сказано
по-русски, по-украински или латиницей, уменьшительно или по фамилии: Андрей = Андрій = Andrii,
Саша = Олександр/Олександра/Alexander, Оля = Ольга/Olha, Женя = Євген/Євгенія, Катя = Катерина,
Лёша/Алёша = Олексій/Алексей/Oleksii/Alexey (НЕ Олег), Олег = Oleh/Oleg, Дима = Дмитро/Dmytro,
Вова = Володимир/Volodymyr, Миша = Михайло/Mykhailo, Юра = Юрій/Yurii, Настя = Анастасія/Anastasiia,
Лена = Олена/Olena, Таня = Тетяна/Tetiana, Ира = Ірина/Iryna, Наташа = Наталія/Nataliia, Серёжа = Сергій/Serhii.
Подходят только люди с этим именем (или фамилией) — не добавляй похожие по звучанию другие имена.
Верни ТОЛЬКО JSON: {{"matches": {{"<запрошенное имя>": [номера подходящих, лучшие первыми]}}}}
Если никто не подходит — пустой список. Не придумывай номера.
Запрошенные имена: {names}"""


async def match_people(names: list[str], people: list[dict]) -> dict:
    """{запрошенное имя: [индексы в people]} — сопоставление имён с каталогом."""
    listing = "\n".join(f"{i}: {p['name']} <{p['email']}>" for i, p in enumerate(people))
    prompt = MATCH.format(people=listing, names=json.dumps(names, ensure_ascii=False))

    def check(d):
        out = {}
        for name in names:
            idx = d["matches"].get(name, [])
            out[name] = [int(i) for i in idx if 0 <= int(i) < len(people)]
        return out

    return await _ask(prompt, check)
