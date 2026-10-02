import os
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("DB_PATH", os.path.join(tempfile.mkdtemp(), "t.db"))

import db
from config import TZ
from reminders import decide


def at(d, h, mi=0):
    return datetime(d.year, d.month, d.day, h, mi, tzinfo=TZ)


def task(d, **kw):
    base = dict(id=1, date=d.isoformat(), needs_gift=0, needs_congrats=0, gift_done=0, congrats_done=0,
                snoozed_until=None, status="active", time=None, created_at="2026-01-01 00:00:00",
                acted_on=None, kind="task", rule=None, recurrence="once")
    base.update(kw)
    return base


class Repeats(unittest.TestCase):
    D = date(2026, 9, 28)

    def test_repeats_every_2h_up_to_3(self):
        o = task(self.D)
        self.assertEqual(decide(o, at(self.D, 10, 30), {}), "d0")
        sent = {"d0": at(self.D, 10, 30)}
        self.assertIsNone(decide(o, at(self.D, 12, 0), sent))           # ещё нет 2 часов
        self.assertEqual(decide(o, at(self.D, 12, 31), sent), "rep:2026-09-28:1")
        sent["rep:2026-09-28:1"] = at(self.D, 12, 31)
        sent["rep:2026-09-28:2"] = at(self.D, 14, 31)
        sent["rep:2026-09-28:3"] = at(self.D, 16, 31)
        self.assertIsNone(decide(o, at(self.D, 18, 40), sent))          # лимит 3

    def test_no_repeat_after_reaction_or_digest(self):
        sent = {"d0": at(self.D, 10, 30)}
        self.assertIsNone(decide(task(self.D, acted_on="2026-09-28"), at(self.D, 13), sent))
        self.assertIsNone(decide(task(self.D), at(self.D, 20, 30), sent))  # после 20:00 тишина

    def test_early_offsets_never_repeat(self):
        o = task(self.D + timedelta(days=7), needs_gift=1, recurrence="yearly", kind="birthday")
        self.assertEqual(decide(o, at(self.D, 10, 30), {}), "d7")
        self.assertIsNone(decide(o, at(self.D, 13), {"d7": at(self.D, 10, 30)}))

    def test_gift_nag_repeats_once(self):
        o = task(self.D + timedelta(days=3), needs_gift=1, recurrence="yearly", kind="birthday")
        nag = "nag:2026-09-28"
        self.assertEqual(decide(o, at(self.D, 10, 30), {}), nag)
        sent = {nag: at(self.D, 10, 30)}
        self.assertEqual(decide(o, at(self.D, 12, 40), sent), "rep:2026-09-28:1")
        sent["rep:2026-09-28:1"] = at(self.D, 12, 40)
        self.assertIsNone(decide(o, at(self.D, 15), sent))

    def test_morning_at_10_30(self):
        self.assertIsNone(decide(task(self.D), at(self.D, 10, 29), {}))
        self.assertEqual(decide(task(self.D), at(self.D, 10, 30), {}), "d0")


class Recurrence(unittest.TestCase):
    def test_weekly_and_monthly(self):
        e = {"recurrence": "weekly", "year": 2026, "month": 9, "day": 7, "rule": None}   # понедельник
        self.assertEqual(db.next_date(e, date(2026, 9, 28)), date(2026, 9, 28))
        self.assertEqual(db.next_date(e, date(2026, 9, 29)), date(2026, 10, 5))
        m = {"recurrence": "monthly", "year": 2026, "month": 1, "day": 31, "rule": None}
        self.assertEqual(db.next_date(m, date(2026, 2, 10)), date(2026, 2, 28))
        self.assertEqual(db.next_date(m, date(2026, 3, 1)), date(2026, 3, 31))

    def test_weekly_or_monthly_skip_early_offsets(self):
        d = date(2026, 9, 28)
        o = task(d + timedelta(days=7), recurrence="weekly")
        self.assertIsNone(decide(o, at(d, 11), {}))


class PeopleAndHolidays(unittest.TestCase):
    def setUp(self):
        if os.path.exists(os.environ["DB_PATH"]):
            os.remove(os.environ["DB_PATH"])
        db.init()
        self.today = datetime.now(TZ).date()

    def test_holidays_seeded_once(self):
        db.seed_holidays(7, self.today)
        db.seed_holidays(7, self.today)
        hol = [e for e in db.list_events(7, holidays=True) if e["recurrence"] == "holiday"]
        self.assertEqual(len(hol), 17)
        self.assertEqual(db.list_events(7), [])            # в обычных списках праздников нет

    def test_recipients_and_gift_done(self):
        db.seed_holidays(7, self.today)
        ev = next(e for e in db.list_events(7, holidays=True) if e["rule"] == "fixed:03-08")
        mom, wife = db.person_id(7, "мама"), db.person_id(7, "Дружина")
        self.assertEqual(db.person_id(7, "Мама"), mom)      # без учёта регистра
        db.set_recipients(7, ev["id"], [mom, wife])
        occ = next(o for o in db.active_occurrences() if o["event_id"] == ev["id"])
        self.assertEqual(occ["needs_gift"], 1)
        self.assertEqual(len(db.recipients(occ["id"])), 2)
        db.mark_recipient(occ["id"], mom)
        self.assertEqual(db.get_occurrence(7, occ["id"])["gift_done"], 0)
        db.mark_recipient(occ["id"], wife)
        self.assertEqual(db.get_occurrence(7, occ["id"])["gift_done"], 1)

    def test_ideas_and_history(self):
        mom = db.person_id(7, "мама")
        i = db.add_idea(mom, "масажер")
        db.add_idea(mom, "сережки")
        db.use_idea(i)
        self.assertEqual([x["text"] for x in db.ideas(mom)], ["сережки"])
        db.add_gift(mom, "ДР мами", 2026, "масажер")
        self.assertEqual(db.gifts(mom)[0]["text"], "масажер")


if __name__ == "__main__":
    unittest.main()


class Weekdays(unittest.TestCase):
    def test_fix_weekday(self):
        import ai
        now = datetime(2026, 9, 29, 12, 0, tzinfo=TZ)  # вівторок
        self.assertEqual(ai.weekday_in("нагадати в понеділок о 10"), 0)
        self.assertEqual(ai.weekday_in("в пʼятницю"), 4)
        self.assertIsNone(ai.weekday_in("з понеділка по вівторок"))
        ev = {"day": 2, "month": 10, "year": 2026, "recurrence": "once"}   # модель дала п'ятницю
        r = ai.fix_weekday({"action": "add", "event": ev}, "в понеділок о 10", now)
        self.assertEqual((r["event"]["day"], r["event"]["month"]), (5, 10))
        r = ai.fix_weekday({"action": "agenda", "date": "2026-09-30", "date_to": None}, "що у вівторок", now)
        self.assertEqual(r["date"], "2026-10-06")                        # сьогоднішній день — через тиждень
        r = ai.fix_weekday({"action": "agenda", "date": "2026-09-29", "date_to": None}, "що у вівторок", now)
        self.assertEqual(r["date"], "2026-09-29")                        # день тижня збігся — не чіпаємо
