import os, sys, tempfile, unittest
from datetime import date, datetime, timedelta
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "t.db")

import db
from config import TZ
from reminders import decide, done_line, snooze_until


class Cleanup(unittest.TestCase):
    def setUp(self):
        if os.path.exists(os.environ["DB_PATH"]):
            os.remove(os.environ["DB_PATH"])
        db.init()

    def test_snooze_month_clamps(self):
        t = datetime(2027, 1, 31, 10, 7, 33, tzinfo=TZ)
        self.assertEqual(snooze_until({}, t, "snzm"), datetime(2027, 2, 28, 10, 7, tzinfo=TZ))
        self.assertEqual(snooze_until({}, t, "snz120"), datetime(2027, 1, 31, 12, 7, tzinfo=TZ))

    def test_done_archives_once_event_immediately(self):
        today = datetime.now(TZ).date()
        eid = db.add_event(1, "Позвонить мастеру", today.day, today.month, today.year, "once", 0, 0, None,
                           today, time="19:00", kind="task")
        occ = db.active_occurrences()[0]
        db.add_msg(occ["id"], 1, 100)
        db.add_msg(occ["id"], 1, 101)
        self.assertTrue(db.complete_occurrence(1, occ["id"], "done"))
        self.assertEqual(db.list_events(1), [])            # исчезло из /all
        self.assertEqual(db.upcoming(1, today + timedelta(days=400)), [])
        self.assertEqual(sorted(db.pop_msgs(occ["id"])), [(1, 100), (1, 101)])
        self.assertEqual(db.pop_msgs(occ["id"]), [])
        line = done_line({"title": "Позвонить мастеру", "kind": "task", "recurrence": "once",
                          "needs_gift": 0, "needs_congrats": 0}, "done", datetime(2026, 9, 27, 19, 4, tzinfo=TZ))
        self.assertEqual(line, "📌 <b>Позвонить мастеру</b>\n✅ Виконано 27.09 в 19:04")
        self.assertIsNotNone(eid)

    def test_mark_done_by_phrase_and_undo(self):
        import asyncio
        import bot
        today = datetime.now(TZ).date()
        eid = db.add_event(1, "Зняти відео", today.day, today.month, today.year, "once", 0, 0, None,
                           today, kind="task")
        occ = db.active_occurrences()[0]
        db.add_msg(occ["id"], 1, 200)
        deleted, replies = [], []

        class Bot:
            async def delete_message(self, chat_id, message_id):
                deleted.append(message_id)

        async def reply(text, **kw):
            replies.append((text, kw["reply_markup"].inline_keyboard[0][0].callback_data))

        asyncio.run(bot.mark_done(Bot(), reply, 1, eid))
        self.assertEqual(deleted, [200])                      # нагадування прибране з чату
        self.assertEqual(db.list_events(1), [])               # разова справа закрита
        self.assertIn("✅ Виконано", replies[0][0])
        self.assertEqual(replies[0][1], f"undo:{eid}:{occ['id']}")
        self.assertTrue(db.reopen(1, eid, occ["id"]))         # «↩️ Не те»
        self.assertEqual(db.current_occurrence(1, eid)["id"], occ["id"])

    def test_yearly_done_keeps_event(self):
        today = datetime.now(TZ).date()
        db.add_event(1, "ДР мамы", 1, 10, None, "yearly", 1, 1, None, today, kind="birthday")
        occ = db.active_occurrences()[0]
        db.complete_occurrence(1, occ["id"], "done")
        self.assertEqual(len(db.list_events(1)), 1)

    def test_snoozed_week_survives_past_date(self):
        today = datetime.now(TZ).date()
        past = today - timedelta(days=3)
        db.add_event(1, "Купить ленту", past.day, past.month, past.year, "once", 0, 0, None, past)
        occ = db.active_occurrences()[0]
        until = datetime.now(TZ) + timedelta(days=4)
        db.update_occurrence(occ["id"], snoozed_until=until.isoformat())
        db.sync_occurrences(today)
        occs = db.active_occurrences()
        self.assertEqual(len(occs), 1)                     # не закрыто и не в архиве
        self.assertTrue(decide(occs[0], until + timedelta(minutes=1), set()).startswith("snooze:"))


if __name__ == "__main__":
    unittest.main()
