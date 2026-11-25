import os
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("DB_PATH", os.path.join(tempfile.mkdtemp(), "t.db"))  # не чіпати справжню базу

import ai
from config import TZ

NOW = datetime(2026, 11, 25, 11, 0, tzinfo=TZ)


class Actions(unittest.TestCase):
    def test_meeting_without_colleagues_is_a_reminder(self):
        r = ai.validate_action({"action": "meeting", "meeting": {"title": "Фриланс", "date": "2026-11-25",
                                                                 "time": "19:30", "attendees": []}}, NOW, {}, False)
        self.assertEqual(r["action"], "add")
        self.assertEqual((r["event"]["title"], r["event"]["day"], r["event"]["time"]), ("Фриланс", 25, "19:30"))

    def test_meeting_event_title_from_attendees(self):
        ev = ai.meeting_event({"title": None, "date": "2026-11-26", "time": "15:00", "attendees": ["Вясан"]}, NOW)
        self.assertEqual((ev["title"], ev["kind"], ev["recurrence"]), ("Зустріч: Вясан", "event", "once"))

    def test_past_time_asks_with_context(self):
        r = ai.validate_action({"action": "add", "event": {"title": "Позвонить", "day": 25, "month": 11,
                                                          "time": "09:00", "recurrence": "once", "kind": "task"}},
                               NOW, {}, False)
        self.assertEqual(r["action"], "error")
        self.assertEqual(r["partial"]["action"], "add")   # відповідь «завтра» доповнить саме цю подію


if __name__ == "__main__":
    unittest.main()
