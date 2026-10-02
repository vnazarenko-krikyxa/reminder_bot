"""Голос -> текст (Groq Whisper) и текст -> действие со списком событий (Groq LLM)."""
import asyncio
import calendar
import json
import logging
import re
from datetime import date, datetime, timedelta

import httpx

from config import GROQ_API_KEY, GROQ_MODELS, GROQ_STT_MODEL
import meetings
import work
from dates import next_yearly, occurrence_date

log = logging.getLogger("reminder_bot.ai")
API = "https://api.groq.com/openai/v1"
# Без браузерного UA Cloudflare перед Groq отвечает 403 (error code 1010)
HEADERS = {
    "Authorization": f"Bearer {GROQ_API_KEY}",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/128.0 Safari/537.36",
}

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

INTERPRET = """Ты помощник-напоминалка. Сейчас {now}.
Календарь на ближайшие дни (бери даты для «в среду», «до пятницы» отсюда): {calendar}
Сохранённые события пользователя (id -> событие):
{events}
Люди пользователя (близкие, кому дарят подарки): {people}

{rules}

Пойми, что хочет пользователь, и верни ТОЛЬКО JSON одного из видов.
Сначала проверь: «идея подарка …», «… хочет …», «можно подарить …» без даты — это action "idea", а не событие.
- добавить новое событие: {{"action": "add", "event": <событие>}}
- изменить существующее: {{"action": "update", "event_id": <id>, "event": <событие целиком
  с учётом изменения; поля, о которых не говорили, скопируй из сохранённого без изменений>}}
- удалить существующее: {{"action": "delete", "event_id": <id>}}
- сообщить об отсутствии на работе (пауза, начну позже, закончу раньше, отгул/выходной, больничный):
  {{"action": "absence", "absence": {{"kind": "pause"|"late_start"|"early_finish"|"day_off"|"sick",
    "date": "YYYY-MM-DD", "date_to": "YYYY-MM-DD"|null, "from": "HH:MM"|null, "to": "HH:MM"|null}}}}
  kind: pause — «пауза/перерыв/отойду с … до …» (from и to; пауза всегда в пределах одного дня, date_to null);
  late_start — «начну работать с 11», «старт с 11», «буду с 11» (from = время начала работы);
  early_finish — «работаю до 16», «закончу раньше в 16», «уйду в 16» (to = время окончания работы);
  day_off — «отгул», «беру выходной», «отпрошусь на день» (date, при нескольких днях — date_to);
  sick — «больничный», «заболел» (date, при нескольких днях — date_to).
  Дата не названа — сегодня. Время, которое не сказали, — null (не выдумывай).
- назначить встречу/созвон с КОЛЛЕГАМИ в Google Calendar — только если сказано «встреча», «созвон»,
  «митинг», «забронируй встречу» и т.п. Приезд родных, визит к врачу, «мама приедет в 4», а также
  созвон/встреча с семьёй, родными, друзьями («созвон с семьёй», «встреча с мамой») — это НЕ meeting,
  а обычное событие (add, kind event; «каждую пятницу» — recurrence weekly):
  {{"action": "meeting", "meeting": {{"title": str|null, "date": "YYYY-MM-DD"|null, "time": "HH:MM"|null,
    "duration_min": int|null, "attendees": [str], "meet": bool, "notes": str|null}}}}
  attendees — с кем, как сказал пользователь, в именительном падеже («с Андреем» -> "Андрей",
  «с Олей и Сашей» -> ["Оля", "Саша"]), или e-mail, если назван. Себя не добавляй.
  title — тема, если названа («тема — отчёт по SEO» -> "Отчёт по SEO"), иначе null.
  duration_min — если сказано («на час» -> 60, «на полчаса» -> 30), иначе null.
  meet — false только если сказано «офлайн», «в офисе», «без Meet»; иначе true.
  Дата не названа — сегодня. Время не названо — null (не выдумывай).
- отменить или перенести уже назначенную встречу/созвон в календаре:
  {{"action": "meeting_change", "change": "cancel"|"move",
    "find": {{"attendees": [str], "date": "YYYY-MM-DD"|null, "time": "HH:MM"|null, "title": str|null}},
    "new": {{"date": "YYYY-MM-DD"|null, "time": "HH:MM"|null, "duration_min": int|null}}}}
  find — по каким признакам искать встречу (с кем, когда, тема) — только то, что сказано.
  new — только для move: новая дата/время/длительность (что не сказано — null).
  «Удали/отмени встречу (созвон, митинг) …», «перенеси встречу/созвон …» — это ВСЕГДА meeting_change,
  а не delete/update: встречи в календаре хранятся не в списке сохранённых событий выше.
- показать список своих сохранённых событий/напоминаний («покажи мои события», «список всех
  запланированных событий», «что я запланировал», «мои напоминания»): {{"action": "reminders", "days": int|null}}
  days — если назван срок («на месяц» -> 30), иначе null (весь список).
- посмотреть Google-календарь/расписание/встречи — ТОЛЬКО если сказано «календарь», «расписание»,
  «встречи», «созвоны» («что в календаре завтра», «расписание на пятницу», «какие у меня встречи»):
  {{"action": "agenda", "week": "this"|"next"|null,
  "date": "YYYY-MM-DD"|null, "date_to": "YYYY-MM-DD"|null}}
  Про неделю («на этой неделе» -> "this"; «на следующей неделе» -> "next"; «на неделю», «на ближайшую
  неделю», «на 7 дней» -> "7d") — заполни только week,
  даты не считай. Иначе week null, date — день (или первый день), date_to — последний день периода.
  Ничего не сказано — всё null.
- запомнить идею подарка («идея подарка маме — массажёр», «брат хочет наушники», «жене можно подарить
  серьги»): {{"action": "idea", "person": str, "text": str}} — person в именительном падеже, text — сама идея
  на языке фразы, без перевода.
- назначить, кому подарки на праздник или событие из списка («на 8 марта подарки жене, маме и тёще»,
  «на Новый год дарю маме и брату»): {{"action": "recipients", "event_id": <id события из списка>,
  "people": [str]}} — people в именительном падеже.
- показать, что известно о человеке («что я знаю про маму», «идеи подарков для жены», «что я дарил брату»):
  {{"action": "person_info", "person": str}}
- не получилось: {{"action": "error", "error": "коротко, чего не хватает — ПО-УКРАИНСКИ",
  "candidates": [<id подходящих событий, если непонятно, какое именно>]}}
ВАЖНО: если фраза про встречу/созвон/митинг с человеком или в календаре («удали встречу с Андреем»,
«отмени созвон в 15», «перенеси митинг») — это meeting_change, даже если в списке выше есть похожее
событие. delete/update для напоминаний — только когда сказано «напоминание» или речь явно о событии из списка
(ДР, праздник, задача).
Изменить/удалить можно только событие из списка выше: ищи по смыслу («мамин день рождения» = «ДР мамы»).
«Не напоминай про …», «убери праздник …» — это delete соответствующего события из списка (праздники тоже там).
Если подходит несколько событий — action "error" с их candidates.
Дата не названа: для дня рождения/годовщины — action "error"; мечта или дело — можно без даты
(day и month null). «Убери дату у …», «сделай без даты», «пусть будет без даты» — это update события
из списка с day null и month null (остальные поля без изменений).
Все тексты error пиши по-украински.
{target}
Фраза: {text}"""

TARGET = """Пользователь сейчас правит событие id={id}: фраза относится к нему (update или delete)."""

PARTIAL_FIX = """Пользователь ДОПОЛНЯЕТ или ИСПРАВЛЯЕТ ещё не выполненное действие "{action}": {data}
Верни action "{action}" целиком с учётом фразы (что не упомянуто — оставь как было).
Тип действия и kind НЕ меняй, если пользователь прямо не сказал другое:
«до 16:30» для паузы — это её конец (to); «в 15» для встречи — это её время."""

DRAFT = """Ты помощник-напоминалка. Сейчас {now}.
Календарь на ближайшие дни: {calendar}
{rules}

Это ИСПРАВЛЕНИЕ ещё не сохранённого черновика. Верни ТОЛЬКО JSON — черновик целиком
с учётом исправления: {{"action": "add", "event": <событие>}}
Текущий черновик: {draft}
Исправление: {text}"""


class AIError(Exception):
    pass


def _retry_after(r: httpx.Response) -> float:
    """Секунды до сброса лимита: retry-after или x-ratelimit-reset-tokens («7.66s», «1m2s»)."""
    if r.headers.get("retry-after"):
        try:
            return float(r.headers["retry-after"])
        except ValueError:
            pass
    m = re.fullmatch(r"(?:(\d+)m)?([\d.]+)s", r.headers.get("x-ratelimit-reset-tokens", ""))
    return int(m.group(1) or 0) * 60 + float(m.group(2)) if m else 5.0


async def transcribe(audio: bytes, filename: str) -> str:
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(
            f"{API}/audio/transcriptions",
            headers=HEADERS,
            files={"file": (filename, audio)},
            data={"model": GROQ_STT_MODEL, "response_format": "json", "temperature": "0"},
        )
    if r.status_code != 200:
        log.error("Groq STT %s: %s", r.status_code, r.text[:300])
        raise AIError("не вдалося розпізнати голос")
    return r.json().get("text", "").strip()


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


# Родные и друзья: встреча с ними — не рабочая (не ищем в каталоге компании)
PERSONAL_RE = (r"сем[ьея]|сім['’ʼ]?[юяї]|друз|друж[иб]|родн|рідн|батьк|родител|мам[аоуиі]|\bтат|пап[аоуи]|"
               r"жен[аоуые]\b|дружин|муж[аеу]?\b|чоловік|брат|сестр|дет[иь]|діт|бабус|дедус|дідус")


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
                    draft: dict | None = None, partial: dict | None = None, people: list | None = None) -> dict:
    """Что сделать со списком событий.

    events — {id: событие в формате черновика}. Возвращает один из вариантов:
    {"action": "add", "event": ev} · {"action": "update", "event_id": id, "event": ev}
    · {"action": "delete", "event_id": id} · {"action": "error", "error": str, "candidates": [id]}
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
            target=(PARTIAL_FIX.format(action=partial["action"], data=json.dumps(partial["data"], ensure_ascii=False))
                    if partial else TARGET.format(id=target_id) if target_id else ""),
            text=text,
        )
    r = await _ask(prompt, lambda d: validate_action(d, now, events, draft is not None))
    meeting = r.get("meeting") or ((r.get("partial") or {}).get("data") if (r.get("partial") or {}).get(
        "action") == "meeting" else None)
    if meeting is not None and not partial and re.search(PERSONAL_RE, text.lower()):
        # «зустріч з друзями», «созвон з мамою» — особиста подія, а не робоча зустріч у Google Calendar
        r = await _ask(prompt + "\nЭто личное событие с родными/друзьями, НЕ рабочая встреча: верни action add.",
                       lambda d: validate_action(d, now, events, draft is not None))
    r = fix_weekday(r, text, now)
    if r["action"] == "agenda" and re.search(r"на (ближайшую |всю )?неделю|на 7 дней", text.lower()):
        # модель путает «на неделю» с «на этой неделе» — считаем 7 дней от сегодня
        r.update(date=now.date().isoformat(), date_to=(now.date() + timedelta(days=6)).isoformat())
    return r


async def _ask(prompt: str, check):
    last = None
    async with httpx.AsyncClient(timeout=40) as client:
        for model in GROQ_MODELS:
            for attempt in range(2):
                r = await client.post(
                    f"{API}/chat/completions",
                    headers=HEADERS,
                    json={
                        "model": model,
                        "messages": [{"role": "user", "content": prompt}],
                        "temperature": 0,
                        "max_tokens": 1500,
                        # reasoning-модели иначе могут потратить весь бюджет на рассуждение
                        "reasoning_effort": "low",
                        "response_format": {"type": "json_object"},
                    },
                )
                if r.status_code == 429 and attempt == 0:
                    # лимит токенов в минуту: ждём, сколько просит Groq (но не дольше 20 с)
                    await asyncio.sleep(min(_retry_after(r), 20))
                    continue
                break
            if r.status_code == 200:
                content = r.json()["choices"][0]["message"].get("content") or ""
                try:
                    return check(json.loads(content))
                except (ValueError, KeyError, TypeError) as e:
                    last = f"{model}: {e}"
                    continue
            last = f"{model}: HTTP {r.status_code} {r.text[:200]}"
    log.error("Groq parse failed: %s", last)
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
    if action == "meeting":
        m = meetings.validate(d["meeting"], now)
        if "error" in m:
            return {"action": "error", "error": m["error"], "candidates": [],
                    "partial": {"action": "meeting", "data": d["meeting"]}}
        return {"action": "meeting", "meeting": m}
    if action in ("update", "delete"):
        eid = int(d["event_id"])
        if eid not in events:
            return {"action": "error", "error": "не знайшов таку подію", "candidates": []}
        if action == "delete":
            return {"action": "delete", "event_id": eid}
        ev = validate(d["event"], now)
        if "error" in ev:
            return {"action": "error", "error": ev["error"], "candidates": []}
        return {"action": "update", "event_id": eid, "event": ev}
    if action != "add":
        raise ValueError(f"неизвестное действие {action!r}")
    ev = validate(d["event"], now)
    if "error" in ev:
        return {"action": "error", "error": ev["error"], "candidates": []}
    return {"action": "add", "event": ev}


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
