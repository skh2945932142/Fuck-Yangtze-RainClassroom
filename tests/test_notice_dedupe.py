"""One mail per event: the notice ledger must never repeat a delivery."""

import json
import os
import sys
import tempfile
import threading
import time
import unittest

os.environ.setdefault("SESSION", "test-session")

# tests/test_listening_socket.py installs a stub for util.notice at import
# time; drop it so the real ledger is what gets tested here.
sys.modules.pop("util.notice", None)

from util import notice  # noqa: E402


class Recorder:
    """Stands in for the SMTP round trip and counts what was delivered."""

    def __init__(self):
        self.calls = []
        self._lock = threading.Lock()

    def __call__(self, subject, content):
        with self._lock:
            self.calls.append((subject, content))

    def count(self):
        with self._lock:
            return len(self.calls)

    def wait_for(self, expected, timeout=2.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.count() >= expected:
                return True
            time.sleep(0.02)
        return self.count() >= expected


class NoticeDedupeTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._saved = {
            "STATE_FILE": notice.STATE_FILE,
            "DEDUPE_TTL_SECONDS": notice.DEDUPE_TTL_SECONDS,
            "_deliver": notice._deliver,
            "email_user": notice.email_user,
            "email_pass": notice.email_pass,
            "to_email": notice.to_email,
        }
        notice.STATE_FILE = os.path.join(self._tmp.name, "notified.json")
        notice.email_user = "sender@example.com"
        notice.email_pass = "authorization-code"
        notice.to_email = "receiver@example.com"
        self.recorder = Recorder()
        notice._deliver = self.recorder

    def tearDown(self):
        for name, value in self._saved.items():
            setattr(notice, name, value)
        self._tmp.cleanup()

    # ---- ledger primitives -------------------------------------------------

    def test_claim_is_first_caller_only(self):
        self.assertTrue(notice._claim("session-expired"))
        self.assertFalse(notice._claim("session-expired"))
        self.assertTrue(notice._claim("other-event"))

    def test_claim_expires_after_ttl(self):
        notice.DEDUPE_TTL_SECONDS = 10
        with open(notice.STATE_FILE, "w", encoding="utf-8") as file:
            json.dump({"exam:1:2": time.time() - 3600}, file)
        self.assertTrue(notice._claim("exam:1:2"))

    def test_forget_allows_a_new_claim(self):
        self.assertTrue(notice._claim("exam:1:2"))
        notice.forget_notice("exam:1:2")
        self.assertTrue(notice._claim("exam:1:2"))

    def test_corrupt_state_file_is_treated_as_empty(self):
        with open(notice.STATE_FILE, "w", encoding="utf-8") as file:
            file.write("{not json")
        self.assertTrue(notice._claim("exam:1:2"))

    # ---- email_notice ------------------------------------------------------

    def test_same_event_is_mailed_once(self):
        for _ in range(5):
            notice.email_notice("s", "c", event_key="exam:26437603:2268377")
        self.assertTrue(self.recorder.wait_for(1))
        time.sleep(0.15)
        self.assertEqual(self.recorder.count(), 1)

    def test_notice_without_event_key_always_sends(self):
        for _ in range(3):
            notice.email_notice("s", "c")
        self.assertTrue(self.recorder.wait_for(3))
        self.assertEqual(self.recorder.count(), 3)

    # ---- concrete notices --------------------------------------------------

    def test_answer_failure_is_mailed_once_per_question(self):
        for _ in range(4):
            notice.answer_failed_notice("数学", "主观题", "题目", "AI 调用异常", problem_id=123)
        self.assertTrue(self.recorder.wait_for(1))
        notice.answer_failed_notice("数学", "主观题", "题目", "AI 调用异常", problem_id=456)
        self.assertTrue(self.recorder.wait_for(2))
        time.sleep(0.15)
        self.assertEqual(self.recorder.count(), 2)

    def test_exam_list_mails_one_digest_then_stays_silent(self):
        exams = [
            {"id": 2268377, "classroom_id": 26437603, "title": "第五章",
             "start_time": 1794821099, "end_time": 1799542800},
            {"id": 2268352, "classroom_id": 26437603, "title": "第四章",
             "start_time": 1794821052, "end_time": 1799542800},
        ]
        # The API repeats the whole list on every scan round.
        for _ in range(3):
            notice.exam_notice(exams)
        self.assertTrue(self.recorder.wait_for(1))
        time.sleep(0.15)
        self.assertEqual(self.recorder.count(), 1)
        # Both exams are in the single digest, in Beijing time.
        subject, body = self.recorder.calls[0]
        self.assertIn("2 场考试", body)
        self.assertIn("第五章", body)
        self.assertIn("第四章", body)

    def test_new_exam_after_the_digest_gets_its_own_mail(self):
        first = [{"id": 1, "classroom_id": 9, "title": "第五章"}]
        notice.exam_notice(first)
        self.assertTrue(self.recorder.wait_for(1))
        # Same exam again: nothing new to say.
        notice.exam_notice(first)
        time.sleep(0.15)
        self.assertEqual(self.recorder.count(), 1)
        # A second exam shows up later: exactly one more mail, for it alone.
        notice.exam_notice(first + [{"id": 2, "classroom_id": 9, "title": "第六章"}])
        self.assertTrue(self.recorder.wait_for(2))
        time.sleep(0.15)
        self.assertEqual(self.recorder.count(), 2)
        self.assertIn("1 场考试", self.recorder.calls[1][1])
        self.assertIn("第六章", self.recorder.calls[1][1])

    def test_session_notice_mails_once_until_forgotten(self):
        for rounds in (3, 4, 5):
            notice.session_expired_notice(rounds)
        self.assertTrue(self.recorder.wait_for(1))
        time.sleep(0.15)
        self.assertEqual(self.recorder.count(), 1)
        # Session recovered, then expired again: a new mail is expected.
        notice.forget_notice(notice.SESSION_EXPIRED_KEY)
        notice.session_expired_notice(3)
        self.assertTrue(self.recorder.wait_for(2))
        self.assertEqual(self.recorder.count(), 2)


if __name__ == "__main__":
    unittest.main()
