"""SQLite-хранилище.

events — шаблон события, occurrences — конкретный раз (год/неделя/месяц).
people — близкие (мама, жена…): к ним привязаны идеи подарков и история подарков.
"""
import calendar
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone

import ua_holidays
from config import DB_PATH, TZ
from dates import next_yearly

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id             INTEGER PRIMARY KEY,
    chat_id        INTEGER NOT NULL,
    title          TEXT    NOT NULL,
    day            INTEGER NOT NULL,
    month          INTEGER NOT NULL,
    year           INTEGER,             -- год рождения/начала (yearly), год события (once), первая дата (weekly/monthly)
    recurrence     TEXT    NOT NULL,    -- once | yearly | weekly | monthly | holiday | someday (мрія без дати)
    needs_gift     INTEGER NOT NULL DEFAULT 0,
    needs_congrats INTEGER NOT NULL DEFAULT 0,
    notes          TEXT,
    time           TEXT,                -- 'HH:MM' или NULL (весь день)
    kind           TEXT,                -- birthday | anniversary | holiday | task | event (для значка)
    rule           TEXT,                -- для праздников: правило даты (ua_holidays)
    person_id      INTEGER,             -- чей это день рождения/годовщина
    archived       INTEGER NOT NULL DEFAULT 0,
    created_at     TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS occurrences (
    id             INTEGER PRIMARY KEY,
    event_id       INTEGER NOT NULL REFERENCES events(id),
    date           TEXT    NOT NULL,
    gift_done      INTEGER NOT NULL DEFAULT 0,
    congrats_done  INTEGER NOT NULL DEFAULT 0,
    snoozed_until  TEXT,
    acted_on       TEXT,                -- дата последнего нажатия любой кнопки (отменяет повторы в этот день)
    status         TEXT    NOT NULL DEFAULT 'active',  -- active | done | skipped | closed
    UNIQUE (event_id, date)
);
CREATE TABLE IF NOT EXISTS reminder_msgs (  -- сообщения-напоминания в чате, чтобы убрать их после «сделано»
    occurrence_id  INTEGER NOT NULL,
    chat_id        INTEGER NOT NULL,
    message_id     INTEGER NOT NULL,
    PRIMARY KEY (chat_id, message_id)
);
CREATE TABLE IF NOT EXISTS sent_log (
    occurrence_id  INTEGER NOT NULL,
    key            TEXT    NOT NULL,
    sent_at        TEXT    NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (occurrence_id, key)
);
CREATE TABLE IF NOT EXISTS people (
    id             INTEGER PRIMARY KEY,
    chat_id        INTEGER NOT NULL,
    name           TEXT    NOT NULL,
    notes          TEXT
);
CREATE TABLE IF NOT EXISTS ideas (
    id             INTEGER PRIMARY KEY,
    person_id      INTEGER NOT NULL REFERENCES people(id),
    text           TEXT    NOT NULL,
    used           INTEGER NOT NULL DEFAULT 0,
    created_at     TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS gifts (          -- что и когда подарено
    id             INTEGER PRIMARY KEY,
    person_id      INTEGER NOT NULL REFERENCES people(id),
    occasion       TEXT    NOT NULL,
    year           INTEGER NOT NULL,
    text           TEXT    NOT NULL,
    created_at     TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS event_recipients (   -- кому подарок на праздник (из года в год)
    event_id       INTEGER NOT NULL,
    person_id      INTEGER NOT NULL,
    PRIMARY KEY (event_id, person_id)
);
CREATE TABLE IF NOT EXISTS occ_recipients (     -- галочки «подарок куплен» на конкретный раз
    occurrence_id  INTEGER NOT NULL,
    person_id      INTEGER NOT NULL,
    done           INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (occurrence_id, person_id)
);
CREATE TABLE IF NOT EXISTS flags (              -- «уже отправлено» для блоков: итог дня, праздники
    key            TEXT PRIMARY KEY,
    created_at     TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


@contextmanager
def connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init():
    with connect() as c:
        c.executescript(SCHEMA)
        cols = {r["name"] for r in c.execute("PRAGMA table_info(events)")}
        had_kind = "kind" in cols
        for col in ("time", "kind", "rule"):  # базы, созданные до появления этих полей
            if col not in cols:
                c.execute(f"ALTER TABLE events ADD COLUMN {col} TEXT")
        if "person_id" not in cols:
            c.execute("ALTER TABLE events ADD COLUMN person_id INTEGER")
        if not had_kind:
            c.execute("UPDATE events SET kind='birthday' WHERE title LIKE 'ДР %' OR lower(title) LIKE '%рожден%'")
        ocols = {r["name"] for r in c.execute("PRAGMA table_info(occurrences)")}
        if "acted_on" not in ocols:
            c.execute("ALTER TABLE occurrences ADD COLUMN acted_on TEXT")
        # выполненные/пропущенные разовые события сразу убираем из списков
        c.execute("UPDATE events SET archived=1 WHERE recurrence='once' AND archived=0 AND id IN"
                  " (SELECT event_id FROM occurrences WHERE status IN ('done', 'skipped'))")


def seed_holidays(chat_id, today: date):
    """Праздники Украины для чата (один раз; удалённые пользователем не возвращаем)."""
    with connect() as c:
        have = {r["rule"] for r in c.execute(
            "SELECT rule FROM events WHERE chat_id=? AND recurrence='holiday'", (chat_id,))}
        for rule, title, _ in ua_holidays.HOLIDAYS:
            if rule in have:
                continue
            d = ua_holidays.next_date(rule, today)
            c.execute(
                "INSERT INTO events (chat_id, title, day, month, recurrence, kind, rule) VALUES (?,?,?,?,?,?,?)",
                (chat_id, title, d.day, d.month, "holiday", "holiday", rule),
            )
    sync_occurrences(today)


# ---------- events ----------

def add_event(chat_id, title, day, month, year, recurrence, needs_gift, needs_congrats, notes, today,
              time=None, kind=None, person_id=None):
    with connect() as c:
        cur = c.execute(
            "INSERT INTO events (chat_id, title, day, month, year, time, kind, recurrence, needs_gift,"
            " needs_congrats, notes, person_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (chat_id, title, day, month, year, time, kind, recurrence, int(needs_gift), int(needs_congrats),
             notes, person_id),
        )
        event_id = cur.lastrowid
    sync_occurrences(today)
    return event_id


def list_events(chat_id, holidays=False):
    with connect() as c:
        return c.execute(
            "SELECT * FROM events WHERE chat_id=? AND archived=0"
            + ("" if holidays else " AND recurrence != 'holiday'") + " ORDER BY month, day",
            (chat_id,),
        ).fetchall()


def get_event(chat_id, event_id):
    with connect() as c:
        return c.execute("SELECT * FROM events WHERE id=? AND chat_id=?", (event_id, chat_id)).fetchone()


EVENT_FIELDS = ("title", "day", "month", "year", "time", "kind", "recurrence", "needs_gift", "needs_congrats", "notes")


def events_brief(chat_id, holidays=True):
    """{id: событие в формате черновика} — для ИИ и для карточек."""
    return {
        e["id"]: {k: (bool(e[k]) if k.startswith("needs_") else e[k]) for k in EVENT_FIELDS}
        for e in list_events(chat_id, holidays)
    }


def update_event(chat_id, event_id, ev: dict, today: date):
    """Перезаписывает событие. Если сдвинулась дата — текущий раз заводится заново
    (галочки «подарок/поздравил» при этом сбрасываются), иначе галочки сохраняются."""
    with connect() as c:
        old = c.execute(
            "SELECT * FROM events WHERE id=? AND chat_id=? AND archived=0", (event_id, chat_id)
        ).fetchone()
        if not old:
            return False
        fields = EVENT_FIELDS
        if old["recurrence"] == "holiday":  # у праздника дата по правилу — её не трогаем
            fields = ("title", "time", "needs_congrats", "notes")
        c.execute(
            "UPDATE events SET " + ", ".join(f"{k}=?" for k in fields) + " WHERE id=?",
            (*(int(ev[k]) if k.startswith("needs_") else ev.get(k) for k in fields), event_id),
        )
        moved = old["recurrence"] != "holiday" and (
            any(old[k] != ev.get(k) for k in ("day", "month", "time", "recurrence"))
            or (ev["recurrence"] != "yearly" and old["year"] != ev["year"])
        )
        if moved:
            yesterday = (today - timedelta(days=1)).isoformat()
            stale = [r["id"] for r in c.execute(
                "SELECT id FROM occurrences WHERE event_id=? AND (date >= ? OR status='active')",
                (event_id, yesterday),
            )]
            for oid in stale:
                c.execute("DELETE FROM sent_log WHERE occurrence_id=?", (oid,))
                c.execute("DELETE FROM occ_recipients WHERE occurrence_id=?", (oid,))
                c.execute("DELETE FROM occurrences WHERE id=?", (oid,))
    sync_occurrences(today)
    return True


def archive_event(chat_id, event_id):
    with connect() as c:
        cur = c.execute(
            "UPDATE events SET archived=1 WHERE id=? AND chat_id=?", (event_id, chat_id)
        )
        c.execute(
            "UPDATE occurrences SET status='closed' WHERE event_id=? AND status='active'", (event_id,)
        )
        return cur.rowcount > 0


# ---------- occurrences ----------

def month_day(year: int, month: int, day: int) -> date:
    """31-е в коротком месяце -> последний день месяца."""
    return date(year, month, min(day, calendar.monthrange(year, month)[1]))


def next_date(e, today: date) -> date | None:
    """Ближайшая (сегодня или позже) дата события; None — разовое уже прошло."""
    rec = e["recurrence"]
    if rec == "someday":
        return None  # мрія без дати
    if rec == "yearly":
        return next_yearly(e["month"], e["day"], today)
    if rec == "holiday":
        return ua_holidays.next_date(e["rule"], today)
    first = date(e["year"], e["month"], e["day"])
    if rec == "once":
        return first if first >= today - timedelta(days=1) else None
    if first >= today:
        return first
    if rec == "weekly":
        return first + timedelta(days=-(-(today - first).days // 7) * 7)
    if rec == "monthly":
        d = month_day(today.year, today.month, e["day"])
        if d < today:
            y, m = (today.year + 1, 1) if today.month == 12 else (today.year, today.month + 1)
            d = month_day(y, m, e["day"])
        return d
    raise ValueError(rec)


def sync_occurrences(today: date):
    """Закрывает прошедшие разы и заводит следующий раз для каждого активного события.

    Идемпотентно: можно вызывать на каждом тике.
    """
    yesterday = (today - timedelta(days=1)).isoformat()
    # отложенное (например, на месяц) не закрываем, пока не сработает — даже если дата уже прошла
    snoozed_future = "(snoozed_until IS NOT NULL AND snoozed_until > ?)"
    now_iso = datetime.now(TZ).isoformat()
    with connect() as c:
        # Прошедшие больше суток назад — закрываем (день «вчера» оставляем для «опоздал поздравить?»)
        c.execute(f"UPDATE occurrences SET status='closed' WHERE status='active' AND date < ?"
                  f" AND NOT {snoozed_future}", (yesterday, now_iso))
        for e in c.execute("SELECT * FROM events WHERE archived=0 AND recurrence != 'someday'").fetchall():
            if c.execute("SELECT 1 FROM occurrences WHERE event_id=? AND status='active'", (e["id"],)).fetchone():
                continue
            d = next_date(e, today)
            if d is None:
                c.execute("UPDATE events SET archived=1 WHERE id=?", (e["id"],))  # разовое прошло
                continue
            cur = c.execute("INSERT OR IGNORE INTO occurrences (event_id, date) VALUES (?, ?)",
                            (e["id"], d.isoformat()))
            if cur.rowcount:
                # получатели подарков на этот раз — из списка события
                c.execute("INSERT OR IGNORE INTO occ_recipients (occurrence_id, person_id)"
                          " SELECT ?, person_id FROM event_recipients WHERE event_id=?", (cur.lastrowid, e["id"]))


OCC_COLS = ("o.*, e.chat_id, e.title, e.day, e.month, e.year, e.recurrence, e.rule, e.person_id,"
            " e.time, e.kind, e.created_at, e.needs_gift, e.needs_congrats, e.notes")


def active_occurrences():
    """Все активные разы вместе с данными события."""
    with connect() as c:
        return c.execute(
            f"SELECT {OCC_COLS} FROM occurrences o JOIN events e ON e.id = o.event_id"
            " WHERE o.status='active' AND e.archived=0 ORDER BY o.date"
        ).fetchall()


def upcoming(chat_id, until: date):
    with connect() as c:
        return c.execute(
            f"SELECT {OCC_COLS} FROM occurrences o JOIN events e ON e.id = o.event_id"
            " WHERE e.chat_id=? AND e.archived=0 AND o.status != 'closed' AND o.date <= ?"
            " ORDER BY o.date, e.time",
            (chat_id, until.isoformat()),
        ).fetchall()


def get_occurrence(chat_id, occ_id):
    with connect() as c:
        return c.execute(
            f"SELECT {OCC_COLS} FROM occurrences o JOIN events e ON e.id = o.event_id"
            " WHERE o.id=? AND e.chat_id=?",
            (occ_id, chat_id),
        ).fetchone()


def update_occurrence(occ_id, **fields):
    cols = ", ".join(f"{k}=?" for k in fields)
    with connect() as c:
        c.execute(f"UPDATE occurrences SET {cols} WHERE id=?", (*fields.values(), occ_id))


# ---------- sent log и флаги ----------

def sent_keys(occ_id) -> dict:
    """{ключ: когда отправлен (aware, TZ)}."""
    with connect() as c:
        return {
            r["key"]: datetime.fromisoformat(r["sent_at"]).replace(tzinfo=timezone.utc).astimezone(TZ)
            for r in c.execute("SELECT key, sent_at FROM sent_log WHERE occurrence_id=?", (occ_id,))
        }


def mark_sent(occ_id, key):
    with connect() as c:
        c.execute("INSERT OR IGNORE INTO sent_log (occurrence_id, key) VALUES (?, ?)", (occ_id, key))


def set_flag(key: str) -> bool:
    """True, если флаг поставлен впервые (блок ещё не отправлялся)."""
    with connect() as c:
        return c.execute("INSERT OR IGNORE INTO flags (key) VALUES (?)", (key,)).rowcount > 0


# ---------- выполнение и уборка чата ----------

def complete_occurrence(chat_id, occ_id, status: str) -> bool:
    """done/skipped. Разовое событие после этого сразу уходит в архив (из списков)."""
    with connect() as c:
        row = c.execute(
            "SELECT o.event_id, e.recurrence FROM occurrences o JOIN events e ON e.id=o.event_id"
            " WHERE o.id=? AND e.chat_id=?", (occ_id, chat_id),
        ).fetchone()
        if not row:
            return False
        c.execute("UPDATE occurrences SET status=?, snoozed_until=NULL WHERE id=?", (status, occ_id))
        if row["recurrence"] == "once":
            c.execute("UPDATE events SET archived=1 WHERE id=?", (row["event_id"],))
    return True


def add_msg(occ_id, chat_id, message_id):
    with connect() as c:
        c.execute("INSERT OR IGNORE INTO reminder_msgs VALUES (?,?,?)", (occ_id, chat_id, message_id))


def pop_msgs(occ_id) -> list[tuple[int, int]]:
    with connect() as c:
        rows = c.execute("SELECT chat_id, message_id FROM reminder_msgs WHERE occurrence_id=?", (occ_id,)).fetchall()
        c.execute("DELETE FROM reminder_msgs WHERE occurrence_id=?", (occ_id,))
    return [(r["chat_id"], r["message_id"]) for r in rows]


# ---------- люди, идеи, подарки ----------

def people(chat_id) -> list[dict]:
    with connect() as c:
        return [dict(r) for r in c.execute("SELECT * FROM people WHERE chat_id=? ORDER BY name", (chat_id,))]


def person_id(chat_id, name: str) -> int:
    """Находит человека по имени (без учёта регистра) или заводит нового."""
    name = name.strip()
    with connect() as c:
        for r in c.execute("SELECT id, name FROM people WHERE chat_id=?", (chat_id,)):
            if r["name"].lower() == name.lower():
                return r["id"]
        return c.execute("INSERT INTO people (chat_id, name) VALUES (?, ?)", (chat_id, name)).lastrowid


def get_person(chat_id, pid):
    with connect() as c:
        r = c.execute("SELECT * FROM people WHERE id=? AND chat_id=?", (pid, chat_id)).fetchone()
        return dict(r) if r else None


def add_idea(pid, text) -> int:
    with connect() as c:
        return c.execute("INSERT INTO ideas (person_id, text) VALUES (?, ?)", (pid, text.strip())).lastrowid


def ideas(pid) -> list[dict]:
    with connect() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM ideas WHERE person_id=? AND used=0 ORDER BY id", (pid,))]


def get_idea(pid, idea_id):
    with connect() as c:
        r = c.execute("SELECT * FROM ideas WHERE id=? AND person_id=?", (idea_id, pid)).fetchone()
        return dict(r) if r else None


def use_idea(idea_id):
    with connect() as c:
        c.execute("UPDATE ideas SET used=1 WHERE id=?", (idea_id,))


def add_gift(pid, occasion: str, year: int, text: str):
    with connect() as c:
        c.execute("INSERT INTO gifts (person_id, occasion, year, text) VALUES (?,?,?,?)",
                  (pid, occasion, year, text.strip()))


def gifts(pid) -> list[dict]:
    with connect() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM gifts WHERE person_id=? ORDER BY year DESC, id DESC", (pid,))]


def person_events(chat_id, pid) -> list:
    with connect() as c:
        return c.execute(
            "SELECT * FROM events WHERE chat_id=? AND archived=0 AND (person_id=? OR id IN"
            " (SELECT event_id FROM event_recipients WHERE person_id=?)) ORDER BY month, day",
            (chat_id, pid, pid),
        ).fetchall()


def set_recipients(chat_id, event_id, pids: list[int]) -> bool:
    """Кому подарок на праздник/событие; обновляет и текущий раз (галочки уже купленных сохраняются)."""
    with connect() as c:
        if not c.execute("SELECT 1 FROM events WHERE id=? AND chat_id=?", (event_id, chat_id)).fetchone():
            return False
        c.execute("DELETE FROM event_recipients WHERE event_id=?", (event_id,))
        c.executemany("INSERT OR IGNORE INTO event_recipients VALUES (?, ?)", [(event_id, p) for p in pids])
        c.execute("UPDATE events SET needs_gift=? WHERE id=?", (int(bool(pids)), event_id))
        for o in c.execute("SELECT id FROM occurrences WHERE event_id=? AND status='active'", (event_id,)).fetchall():
            ph = ",".join("?" * len(pids)) or "NULL"
            c.execute(f"DELETE FROM occ_recipients WHERE occurrence_id=? AND person_id NOT IN ({ph})", (o["id"], *pids))
            c.executemany("INSERT OR IGNORE INTO occ_recipients (occurrence_id, person_id) VALUES (?, ?)",
                          [(o["id"], p) for p in pids])
            _recount_gift(c, o["id"])
    return True


def recipients(occ_id) -> list[dict]:
    with connect() as c:
        return [dict(r) for r in c.execute(
            "SELECT r.person_id, r.done, p.name FROM occ_recipients r JOIN people p ON p.id=r.person_id"
            " WHERE r.occurrence_id=? ORDER BY p.name", (occ_id,))]


def mark_recipient(occ_id, pid):
    with connect() as c:
        c.execute("UPDATE occ_recipients SET done=1 WHERE occurrence_id=? AND person_id=?", (occ_id, pid))
        _recount_gift(c, occ_id)


def _recount_gift(c, occ_id):
    """gift_done праздника = подарки куплены всем получателям."""
    left, total = c.execute(
        "SELECT SUM(done=0), COUNT(*) FROM occ_recipients WHERE occurrence_id=?", (occ_id,)).fetchone()
    if total:
        c.execute("UPDATE occurrences SET gift_done=? WHERE id=?", (int(left == 0), occ_id))


def link_person(event_id, pid):
    with connect() as c:
        c.execute("UPDATE events SET person_id=? WHERE id=?", (pid, event_id))


def by_kind(chat_id, kinds: tuple, today: date) -> list[tuple]:
    """[(событие, ближайшая дата | None)] нужных видов, по дате (без даты — в конце)."""
    ph = ",".join("?" * len(kinds))
    with connect() as c:
        rows = c.execute(f"SELECT * FROM events WHERE chat_id=? AND archived=0 AND COALESCE(kind, 'task') IN ({ph})",
                         (chat_id, *kinds)).fetchall()
    out = [(e, next_date(e, today)) for e in rows]
    return sorted(out, key=lambda x: (x[1] is None, x[1] or date.max, x[0]["title"]))


def clear_date(chat_id, event_id) -> bool:
    """«Прибрати дату»: подія стає справою/бажанням без дати, поточні нагадування знімаються."""
    with connect() as c:
        cur = c.execute(
            "UPDATE events SET recurrence='someday', day=0, month=0, year=NULL, time=NULL"
            " WHERE id=? AND chat_id=? AND archived=0 AND COALESCE(kind, 'task') IN ('task', 'wish', 'event')",
            (event_id, chat_id),
        )
        c.execute("UPDATE occurrences SET status='closed' WHERE event_id=? AND status='active'", (event_id,))
        return cur.rowcount > 0


def undated_tasks(chat_id) -> list:
    with connect() as c:
        return c.execute(
            "SELECT * FROM events WHERE chat_id=? AND archived=0 AND recurrence='someday'"
            " AND COALESCE(kind, 'task') != 'wish' ORDER BY created_at", (chat_id,)).fetchall()
