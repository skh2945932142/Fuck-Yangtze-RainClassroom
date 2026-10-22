import contextlib
import io
import json
import os
import sys
import threading
import time
import types
import unittest

import requests


os.environ.setdefault("SESSION", "test-session")


def _install_optional_dependency_stubs():
    ai = types.ModuleType("util.ai")
    ai.request_ai = lambda **_kwargs: ""
    notice = types.ModuleType("util.notice")
    notice.email_notice = lambda **_kwargs: None
    notice.session_expired_notice = lambda *_args, **_kwargs: None
    notice.answer_failed_notice = lambda *_args, **_kwargs: None
    timestamp = types.ModuleType("util.timestamp")
    timestamp.get_date_time = lambda: ""
    sys.modules.setdefault("util.ai", ai)
    sys.modules.setdefault("util.notice", notice)
    sys.modules.setdefault("util.timestamp", timestamp)


_install_optional_dependency_stubs()
from function import listening_socket  # noqa: E402


class FakeWebSocket:
    def __init__(self, connected=True):
        self.sock = types.SimpleNamespace(connected=connected)
        self.sent = []
        self.closed = False

    def send(self, payload):
        self.sent.append(payload)

    def close(self):
        self.closed = True
        self.sock.connected = False


class ListeningSocketTests(unittest.TestCase):
    def test_ppt_timeout_retries_without_stopping_listener(self):
        ws = FakeWebSocket()
        calls = []

        class FakeResponse:
            status_code = 200

            def json(self):
                return {"data": {"slides": []}}

        def intermittent_get(**_kwargs):
            calls.append(1)
            if len(calls) == 1:
                raise requests.exceptions.Timeout()
            return FakeResponse()

        original_get = listening_socket.requests.get
        listening_socket.requests.get = intermittent_get
        on_message = listening_socket.on_message_connect(
            ppt_jwt="ppt", lesson_id="lesson", identity_id="user",
            socket_jwt="socket", sleep_second=0,
        )
        timeline = json.dumps({"op": "hello", "timeline": [
            {"type": "slide", "pres": "presentation"}
        ]})
        try:
            on_message(ws, timeline)
            on_message.pending_messages.join()
            self.assertEqual("hello", json.loads(ws.sent[-1])["op"])
            on_message(ws, timeline)
            on_message.pending_messages.join()
        finally:
            on_message.stop_processing()
            listening_socket.requests.get = original_get

        self.assertEqual(2, len(calls))
        self.assertEqual("fetchtimeline", json.loads(ws.sent[-1])["op"])

    def test_slow_ppt_fetch_does_not_block_lessonfinished(self):
        started = threading.Event()
        release = threading.Event()
        callback_finished = threading.Event()
        stop_event = threading.Event()
        ws = FakeWebSocket()

        class FakeResponse:
            status_code = 200

            def json(self):
                return {"data": {"slides": []}}

        def slow_get(**_kwargs):
            started.set()
            release.wait(2)
            return FakeResponse()

        original_get = listening_socket.requests.get
        listening_socket.requests.get = slow_get
        on_message = listening_socket.on_message_connect(
            ppt_jwt="ppt", lesson_id="lesson", identity_id="user",
            socket_jwt="socket", stop_event=stop_event,
        )
        try:
            def receive_timeline():
                on_message(ws, json.dumps({"op": "hello", "timeline": [
                    {"type": "slide", "pres": "presentation"}
                ]}))
                callback_finished.set()

            callback_thread = threading.Thread(target=receive_timeline, daemon=True)
            callback_thread.start()
            self.assertTrue(started.wait(1))
            self.assertTrue(callback_finished.wait(0.2))
            on_message(ws, json.dumps({"op": "lessonfinished"}))
            self.assertTrue(stop_event.is_set())
            self.assertTrue(ws.closed)
        finally:
            release.set()
            callback_thread.join(timeout=1)
            on_message.stop_processing()
            on_message.pending_messages.join()
            listening_socket.requests.get = original_get

    def test_multiple_unlocked_questions_trigger_one_follow_up_poll(self):
        ws = FakeWebSocket()

        class FakeResponse:
            status_code = 200

            def json(self):
                return {"data": {"slides": [
                    {"coverAlt": "", "problem": {"problemId": problem_id,
                     "problemType": 1, "body": "test", "options": [], "answers": []}}
                    for problem_id in ("q1", "q2")
                ]}}

        original_get = listening_socket.requests.get
        listening_socket.requests.get = lambda **_kwargs: FakeResponse()
        on_message = listening_socket.on_message_connect(
            ppt_jwt="ppt", lesson_id="lesson", identity_id="user",
            socket_jwt="socket", sleep_second=0,
        )
        try:
            on_message(ws, json.dumps({"op": "hello", "timeline": [
                {"type": "slide", "pres": "presentation"}
            ]}))
            on_message(ws, json.dumps({"op": "fetchtimeline", "unlockedproblem": ["q1", "q2"]}))
            on_message.pending_messages.join()
        finally:
            on_message.stop_processing()
            on_message.pending_messages.join()
            listening_socket.requests.get = original_get

        # Poll replies carry IDs only: one probleminfo deadline query per
        # question, plus exactly one follow-up fetchtimeline for the batch.
        queries = [json.loads(p) for p in ws.sent]
        self.assertEqual(2, sum(q["op"] == "probleminfo" for q in queries))
        self.assertEqual(2, sum(q["op"] == "fetchtimeline" for q in queries))

    def test_answered_question_is_not_reprocessed_after_reconnect(self):
        answered_ids = set()
        seen_ids = set()
        get_calls = []
        output = io.StringIO()

        class FakeResponse:
            status_code = 200

            def json(self):
                return {"data": {"slides": [{
                    "coverAlt": "",
                    "problem": {
                        "problemId": "q1",
                        "problemType": 1,
                        "body": "test question",
                        "options": [],
                        "answers": [],
                    },
                }]}}

        original_get = listening_socket.requests.get
        original_answer = listening_socket.answer
        listening_socket.requests.get = lambda **kwargs: (get_calls.append(kwargs), FakeResponse())[1]
        listening_socket.answer = lambda **kwargs: True  # thread marks q1 answered instantly
        wss = []
        try:
            with contextlib.redirect_stdout(output):
                for i in range(2):
                    ws = FakeWebSocket()
                    wss.append(ws)
                    on_message = listening_socket.on_message_connect(
                        ppt_jwt="ppt",
                        lesson_id="lesson",
                        identity_id="user",
                        socket_jwt="socket",
                        sleep_second=0,
                        answered_problem_ids=answered_ids,
                        seen_problem_ids=seen_ids,
                    )
                    on_message(ws, json.dumps({"op": "hello", "timeline": [
                        {"type": "slide", "pres": "presentation"}
                    ]}))
                    on_message(ws, json.dumps({"op": "fetchtimeline", "unlockedproblem": ["q1"]}))
                    on_message.pending_messages.join()
                    if i == 0:
                        # complete round 1: server replies with the deadline,
                        # the answer thread marks q1 answered
                        on_message(ws, json.dumps({"op": "probleminfo", "problemid": "q1",
                                                   "limit": 30, "now": 0, "dt": 0}))
                        on_message.pending_messages.join()
                        for _ in range(100):
                            if "q1" in answered_ids:
                                break
                            time.sleep(0.02)
                    on_message.stop_processing()
        finally:
            listening_socket.requests.get = original_get
            listening_socket.answer = original_answer

        # Round 1: q1 unanswered -> one deadline query sent. Round 2: q1
        # already answered (shared answered_ids) -> no new query, PPT refetch.
        self.assertEqual(2, len(get_calls))
        first = [json.loads(p) for p in wss[0].sent if json.loads(p)["op"] == "probleminfo"]
        second = [json.loads(p) for p in wss[1].sent if json.loads(p)["op"] == "probleminfo"]
        self.assertEqual(1, len(first))
        self.assertEqual(0, len(second))
        self.assertEqual({"q1"}, answered_ids)
        self.assertEqual(1, output.getvalue().count("发现 1 道新题"))


    def test_unlockproblem_push_triggers_answer_immediately(self):
        answer_calls = []
        ws = FakeWebSocket()

        class FakeResponse:
            status_code = 200

            def json(self):
                return {"data": {"slides": [
                    {"coverAlt": "", "problem": {"problemId": "q9",
                     "problemType": 1, "body": "push question", "options": [], "answers": []}}
                ]}}

        original_get = listening_socket.requests.get
        original_answer = listening_socket.answer
        listening_socket.requests.get = lambda **_kwargs: FakeResponse()
        listening_socket.answer = lambda **kwargs: (answer_calls.append(kwargs), True)[1]
        on_message = listening_socket.on_message_connect(
            ppt_jwt="ppt", lesson_id="lesson", identity_id="user",
            socket_jwt="socket", sleep_second=0, course_name="电路",
        )
        try:
            # hello reply caches the problem
            on_message(ws, json.dumps({"op": "hello", "timeline": [
                {"type": "slide", "pres": "p1"}]}))
            on_message.pending_messages.join()
            self.assertEqual(0, len(answer_calls))
            # server push: teacher publishes -> answer fires without any poll
            on_message(ws, json.dumps({"op": "unlockproblem", "problem": {"prob": "q9"}}))
            on_message.pending_messages.join()
            for _ in range(100):
                if answer_calls:
                    break
                time.sleep(0.02)
        finally:
            on_message.stop_processing()
            on_message.pending_messages.join()
            listening_socket.requests.get = original_get
            listening_socket.answer = original_answer

        self.assertEqual(1, len(answer_calls))
        self.assertEqual("电路", answer_calls[0]["course_name"])
        # push handler must also re-arm the poll loop (fetchtimeline sent)
        self.assertTrue(any(json.loads(p)["op"] == "fetchtimeline" for p in ws.sent))

    def test_unlockproblem_push_for_uncached_problem_requests_resweep(self):
        ws = FakeWebSocket()
        on_message = listening_socket.on_message_connect(
            ppt_jwt="ppt", lesson_id="lesson", identity_id="user",
            socket_jwt="socket", sleep_second=0,
        )
        try:
            on_message(ws, json.dumps({"op": "unlockproblem", "problem": {"prob": "unknown-q"}}))
            on_message.pending_messages.join()
        finally:
            on_message.stop_processing()
            on_message.pending_messages.join()

        ops = [json.loads(p)["op"] for p in ws.sent]
        self.assertIn("fetchtimeline", ops)  # asks for resweep/poll

    def test_probleminfo_reply_spawns_delayed_answer(self):
        answer_calls = []
        ws = FakeWebSocket()
        now_ms = 2_000_000_000_000

        class FakeResponse:
            status_code = 200

            def json(self):
                return {"data": {"slides": [
                    {"coverAlt": "", "problem": {"problemId": "q5",
                     "problemType": 4, "body": "fill me", "options": None, "answers": []}}
                ]}}

        original_get = listening_socket.requests.get
        original_answer = listening_socket.answer
        listening_socket.requests.get = lambda **_kwargs: FakeResponse()
        listening_socket.answer = lambda **kwargs: (answer_calls.append(kwargs), True)[1]
        on_message = listening_socket.on_message_connect(
            ppt_jwt="ppt", lesson_id="lesson", identity_id="user",
            socket_jwt="socket", sleep_second=0,
        )
        try:
            on_message(ws, json.dumps({"op": "hello", "timeline": [
                {"type": "slide", "pres": "p1"}]}))
            on_message.pending_messages.join()
            on_message(ws, json.dumps({"op": "fetchtimeline", "unlockedproblem": ["q5"]}))
            on_message.pending_messages.join()
            # server replies: 30 seconds left on the clock
            on_message(ws, json.dumps({"op": "probleminfo", "problemid": "q5",
                                       "limit": 30, "now": now_ms, "dt": now_ms}))
            on_message.pending_messages.join()
            for _ in range(100):
                if answer_calls:
                    break
                time.sleep(0.02)
        finally:
            on_message.stop_processing()
            on_message.pending_messages.join()
            listening_socket.requests.get = original_get
            listening_socket.answer = original_answer

        self.assertEqual(1, len(answer_calls))
        target = answer_calls[0]["delay_until_ts"]
        # limit 30s < margin 30s (default): submit immediately, not held
        self.assertIsNotNone(target)
        self.assertAlmostEqual(time.time(), target, delta=2)

    def test_long_limit_still_holds_until_margin(self):
        answer_calls = []
        ws = FakeWebSocket()
        now_ms = 2_000_000_000_000

        class FakeResponse:
            status_code = 200

            def json(self):
                return {"data": {"slides": [
                    {"coverAlt": "", "problem": {"problemId": "q5b",
                     "problemType": 4, "body": "fill me", "options": None, "answers": []}}
                ]}}

        original_get = listening_socket.requests.get
        original_answer = listening_socket.answer
        listening_socket.requests.get = lambda **_kwargs: FakeResponse()
        listening_socket.answer = lambda **kwargs: (answer_calls.append(kwargs), True)[1]
        on_message = listening_socket.on_message_connect(
            ppt_jwt="ppt", lesson_id="lesson", identity_id="user",
            socket_jwt="socket", sleep_second=0,
        )
        try:
            on_message(ws, json.dumps({"op": "hello", "timeline": [
                {"type": "slide", "pres": "p1"}]}))
            on_message.pending_messages.join()
            on_message(ws, json.dumps({"op": "fetchtimeline", "unlockedproblem": ["q5b"]}))
            on_message.pending_messages.join()
            # server replies: 120 seconds left on the clock
            on_message(ws, json.dumps({"op": "probleminfo", "problemid": "q5b",
                                       "limit": 120, "now": now_ms, "dt": now_ms}))
            on_message.pending_messages.join()
            for _ in range(100):
                if answer_calls:
                    break
                time.sleep(0.02)
        finally:
            on_message.stop_processing()
            on_message.pending_messages.join()
            listening_socket.requests.get = original_get
            listening_socket.answer = original_answer

        self.assertEqual(1, len(answer_calls))
        target = answer_calls[0]["delay_until_ts"]
        self.assertIsNotNone(target)
        # deadline = now + 120s; submit target = deadline - 30s => ~now + 90s
        self.assertAlmostEqual(time.time() + 90, target, delta=3)

    def test_failed_submission_is_retried_on_next_poll(self):
        answer_calls = []
        ws = FakeWebSocket()
        results = [False, True]  # first submit rejected, retry accepted

        class FakeResponse:
            status_code = 200

            def json(self):
                return {"data": {"slides": [
                    {"coverAlt": "", "problem": {"problemId": "q7",
                     "problemType": 1, "body": "retry me", "options": [], "answers": []}}
                ]}}

        def fake_answer(**kwargs):
            answer_calls.append(kwargs)
            return results.pop(0) if results else True

        original_get = listening_socket.requests.get
        original_answer = listening_socket.answer
        listening_socket.requests.get = lambda **_kwargs: FakeResponse()
        listening_socket.answer = fake_answer
        on_message = listening_socket.on_message_connect(
            ppt_jwt="ppt", lesson_id="lesson", identity_id="user",
            socket_jwt="socket", sleep_second=0,
        )
        try:
            on_message(ws, json.dumps({"op": "hello", "timeline": [
                {"type": "slide", "pres": "p1"}]}))
            on_message.pending_messages.join()
            # Round 1: submit fails -> query marked stale for retry.
            on_message(ws, json.dumps({"op": "fetchtimeline", "unlockedproblem": ["q7"]}))
            on_message.pending_messages.join()
            on_message(ws, json.dumps({"op": "probleminfo", "problemid": "q7",
                                       "limit": -1, "now": 0, "dt": 0}))
            on_message.pending_messages.join()
            for _ in range(100):
                if len(answer_calls) >= 1:
                    break
                time.sleep(0.02)
            # Let the round-1 answer thread fully exit so its processing flag
            # is released before round 2 (avoids a benign race in the test).
            time.sleep(0.1)
            # Round 2: the poll must re-query the deadline (not skip q7),
            # and the fresh reply spawns the retry that succeeds.
            on_message(ws, json.dumps({"op": "fetchtimeline", "unlockedproblem": ["q7"]}))
            on_message.pending_messages.join()
            on_message(ws, json.dumps({"op": "probleminfo", "problemid": "q7",
                                       "limit": -1, "now": 0, "dt": 0}))
            on_message.pending_messages.join()
            for _ in range(100):
                if len(answer_calls) >= 2:
                    break
                time.sleep(0.02)
        finally:
            on_message.stop_processing()
            on_message.pending_messages.join()
            listening_socket.requests.get = original_get
            listening_socket.answer = original_answer

        self.assertEqual(2, len(answer_calls))
        queries = [json.loads(p) for p in ws.sent if json.loads(p)["op"] == "probleminfo"]
        self.assertEqual(2, len(queries))  # re-queried after the failure

    def test_answer_retries_stop_after_max_attempts(self):
        answer_calls = []
        ws = FakeWebSocket()

        class FakeResponse:
            status_code = 200

            def json(self):
                return {"data": {"slides": [
                    {"coverAlt": "", "problem": {"problemId": "q8",
                     "problemType": 1, "body": "hopeless", "options": [], "answers": []}}
                ]}}

        original_get = listening_socket.requests.get
        original_answer = listening_socket.answer
        listening_socket.requests.get = lambda **_kwargs: FakeResponse()
        listening_socket.answer = lambda **kwargs: (answer_calls.append(kwargs), False)[1]
        on_message = listening_socket.on_message_connect(
            ppt_jwt="ppt", lesson_id="lesson", identity_id="user",
            socket_jwt="socket", sleep_second=0,
        )
        try:
            on_message(ws, json.dumps({"op": "hello", "timeline": [
                {"type": "slide", "pres": "p1"}]}))
            on_message.pending_messages.join()
            # Many poll rounds: attempts must cap at 3.
            for _ in range(6):
                on_message(ws, json.dumps({"op": "fetchtimeline", "unlockedproblem": ["q8"]}))
                on_message.pending_messages.join()
                on_message(ws, json.dumps({"op": "probleminfo", "problemid": "q8",
                                           "limit": -1, "now": 0, "dt": 0}))
                on_message.pending_messages.join()
                for _ in range(100):
                    if len(answer_calls) > _:
                        pass
                    time.sleep(0.001)
                time.sleep(0.05)
        finally:
            on_message.stop_processing()
            on_message.pending_messages.join()
            listening_socket.requests.get = original_get
            listening_socket.answer = original_answer

        self.assertLessEqual(len(answer_calls), 3)

    def test_answer_reads_set_auth_and_body_code(self):
        # Success is judged by body code == 0; a fresh Set-Auth header must be
        # picked up into the token box for subsequent submissions.
        token_box = {"jwt": "old-jwt"}

        class FakeResponse:
            status_code = 200
            headers = {"Set-Auth": "fresh-jwt"}
            text = '{"code": 0, "msg": "OK"}'

            def json(self):
                return {"code": 0, "msg": "OK"}

        original_post = listening_socket.requests.post
        original_ai = listening_socket.request_ai
        captured = {}

        def fake_post(**kwargs):
            captured["headers"] = kwargs["headers"]
            return FakeResponse()

        listening_socket.requests.post = fake_post
        listening_socket.request_ai = lambda **_kwargs: ["A"]
        try:
            ok = listening_socket.answer(
                problem_id="q1", problem_type=1, jwt="old-jwt",
                problem_content="q", options=[{"key": "A", "value": "x"}],
                img_url="", token_box=token_box, lesson_id="lesson",
            )
            # Second call must reuse the refreshed token from the token box.
            ok2 = listening_socket.answer(
                problem_id="q2", problem_type=1, jwt="old-jwt",
                problem_content="q2", options=[{"key": "A", "value": "x"}],
                img_url="", token_box=token_box, lesson_id="lesson",
            )
        finally:
            listening_socket.requests.post = original_post
            listening_socket.request_ai = original_ai

        self.assertTrue(ok)
        self.assertEqual("fresh-jwt", token_box["jwt"])
        # Second call must reuse the refreshed token from the token box; the
        # first call's header depended on the pre-test box state.
        self.assertEqual("Bearer fresh-jwt", captured["headers"]["Authorization"])
        self.assertTrue(ok2)

    def test_answer_bare_string_json_response_is_handled(self):
        # Regression: the server once returned a bare JSON string on reject,
        # crashing the old error handler ('str' has no .get) and hiding the
        # real reason.
        token_box = {"jwt": "jwt"}

        class FakeResponse:
            status_code = 403
            headers = {}
            text = '"forbidden"'

            def json(self):
                return "forbidden"

        original_post = listening_socket.requests.post
        original_notice = listening_socket.answer_failed_notice
        notices = []
        listening_socket.requests.post = lambda **_kwargs: FakeResponse()
        listening_socket.answer_failed_notice = lambda *a, **k: notices.append(a)
        try:
            ok = listening_socket.answer(
                problem_id="q1", problem_type=1, jwt="jwt",
                problem_content="q", options=[{"key": "A", "value": "x"}],
                img_url="", token_box=token_box, lesson_id="lesson",
            )
        finally:
            listening_socket.requests.post = original_post
            listening_socket.answer_failed_notice = original_notice

        # Transient rejection: returns False without mailing — the retry
        # logic in spawn_answer mails only when attempts are exhausted.
        self.assertFalse(ok)
        self.assertEqual(0, len(notices))

    def test_answer_terminal_states_are_not_retried_or_mailed(self):
        # 50028 ALREADY_ANSWERED means the question already has an answer
        # (user manually answered): counted as success, no mail, no retry.
        # 50026 LESSON_PROBLEM_FINISHED means the teacher closed the question:
        # unrecoverable, notify once but mark the attempt loop terminal.
        token_box = {"jwt": "jwt"}


        class FakeResponseAlready:
            status_code = 200
            headers = {}
            text = '{"code": 50028, "msg": "LESSON_PROBLEM_ALREADY_ANSWERED"}'

            def json(self):
                return {"code": 50028, "msg": "LESSON_PROBLEM_ALREADY_ANSWERED"}

        class FakeResponseFinished:
            status_code = 200
            headers = {}
            text = '{"code": 50026, "msg": "LESSON_PROBLEM_FINISHED"}'

            def json(self):
                return {"code": 50026, "msg": "LESSON_PROBLEM_FINISHED"}

        original_post = listening_socket.requests.post
        original_notice = listening_socket.answer_failed_notice
        original_ai = listening_socket.request_ai
        notices = []
        listening_socket.requests.post = lambda **_kwargs: FakeResponseAlready()
        listening_socket.answer_failed_notice = lambda *a, **k: notices.append(a)
        listening_socket.request_ai = lambda **_kwargs: ["A"]
        try:
            ok_already = listening_socket.answer(
                problem_id="q1", problem_type=1, jwt="jwt",
                problem_content="q", options=[{"key": "A", "value": "x"}],
                img_url="", token_box=token_box, lesson_id="lesson",
            )
        finally:
            listening_socket.requests.post = original_post
            listening_socket.answer_failed_notice = original_notice
            listening_socket.request_ai = original_ai

        self.assertIs(True, ok_already)  # duplicate answer counts as done
        self.assertEqual(0, len(notices))

        notices2 = []
        listening_socket.requests.post = lambda **_kwargs: FakeResponseFinished()
        listening_socket.answer_failed_notice = lambda *a, **k: notices2.append(a)
        listening_socket.request_ai = lambda **_kwargs: ["A"]
        original_notice2 = listening_socket.answer_failed_notice
        try:
            outcome = listening_socket.answer(
                problem_id="q2", problem_type=1, jwt="jwt",
                problem_content="q2", options=[{"key": "A", "value": "x"}],
                img_url="", token_box=token_box, lesson_id="lesson",
                course_name="毛概",
            )
        finally:
            listening_socket.requests.post = original_post
            listening_socket.answer_failed_notice = original_notice2
            listening_socket.request_ai = original_ai

        self.assertEqual("terminal", outcome)  # spawn_answer stops retrying
        self.assertEqual(1, len(notices2))     # exactly one mail, no reprint
        self.assertIn("结束", notices2[0][3])

    def test_notification_is_ignored_without_follow_up_send(self):
        ws = FakeWebSocket()
        on_message = listening_socket.on_message_connect(
            ppt_jwt="ppt", lesson_id="lesson", identity_id="user", socket_jwt="socket"
        )

        on_message(ws, json.dumps({"op": "notification", "notifications": [], "isReply": True}))

        self.assertEqual([], ws.sent)
        on_message.stop_processing()

    def test_lessonfinished_stops_and_does_not_process_more_messages(self):
        ws = FakeWebSocket()
        stop_event = threading.Event()
        on_message = listening_socket.on_message_connect(
            ppt_jwt="ppt",
            lesson_id="lesson",
            identity_id="user",
            socket_jwt="socket",
            stop_event=stop_event,
        )

        on_message(ws, json.dumps({"op": "lessonfinished"}))

        self.assertTrue(stop_event.is_set())
        self.assertTrue(ws.closed)
        self.assertEqual([], ws.sent)
        on_message.stop_processing()

    def test_send_if_connected_skips_closed_socket(self):
        ws = FakeWebSocket(connected=False)

        sent = listening_socket.send_if_connected(ws, {"op": "hello"})

        self.assertFalse(sent)
        self.assertEqual([], ws.sent)

    def test_disconnect_reconnects_with_keepalive_then_stops_after_lessonfinished(self):
        apps = []

        class FakeWebSocketApp(FakeWebSocket):
            def __init__(self, **callbacks):
                super().__init__()
                self.callbacks = callbacks
                self.run_options = None
                apps.append(self)

            def run_forever(self, **options):
                self.run_options = options
                if len(apps) == 2:
                    self.callbacks["on_message"](self, json.dumps({"op": "lessonfinished"}))

        original_app = listening_socket.websocket.WebSocketApp
        original_delay = listening_socket.RECONNECT_DELAY_SECONDS
        listening_socket.websocket.WebSocketApp = FakeWebSocketApp
        listening_socket.RECONNECT_DELAY_SECONDS = 0
        try:
            listening_socket.start_socket_ppt("ppt", "socket", "lesson", "user")
        finally:
            listening_socket.websocket.WebSocketApp = original_app
            listening_socket.RECONNECT_DELAY_SECONDS = original_delay

        self.assertEqual(2, len(apps))
        self.assertEqual(30, apps[0].run_options["ping_interval"])
        self.assertEqual(10, apps[0].run_options["ping_timeout"])
        self.assertTrue(apps[1].closed)

    def test_probleminfo_fallback_answers_after_short_wait(self):
        # Live 2026-10-22: the reply lagged ~30s; the old 90s fallback made
        # the submission land after the teacher closed the question.
        answer_calls = []
        ws = FakeWebSocket()

        class FakeResponse:
            status_code = 200

            def json(self):
                return {"data": {"slides": [
                    {"coverAlt": "", "problem": {"problemId": "qf",
                     "problemType": 5, "body": "subjective", "options": None, "answers": []}}
                ]}}

        original_get = listening_socket.requests.get
        original_answer = listening_socket.answer
        original_fallback = listening_socket.PROBLEMINFO_FALLBACK_SECONDS
        listening_socket.requests.get = lambda **_kwargs: FakeResponse()
        listening_socket.answer = lambda **kwargs: (answer_calls.append(kwargs), True)[1]
        listening_socket.PROBLEMINFO_FALLBACK_SECONDS = 0.05
        on_message = listening_socket.on_message_connect(
            ppt_jwt="ppt", lesson_id="lesson", identity_id="user",
            socket_jwt="socket", sleep_second=0,
        )
        try:
            on_message(ws, json.dumps({"op": "hello", "timeline": [
                {"type": "slide", "pres": "p1"}]}))
            on_message.pending_messages.join()
            on_message(ws, json.dumps({"op": "fetchtimeline", "unlockedproblem": ["qf"]}))
            on_message.pending_messages.join()
            # No probleminfo reply: the short fallback must answer anyway.
            for _ in range(100):
                if answer_calls:
                    break
                time.sleep(0.02)
        finally:
            on_message.stop_processing()
            on_message.pending_messages.join()
            listening_socket.requests.get = original_get
            listening_socket.answer = original_answer
            listening_socket.PROBLEMINFO_FALLBACK_SECONDS = original_fallback

        self.assertEqual(1, len(answer_calls))
        self.assertIsNone(answer_calls[0]["delay_until_ts"])

    def test_probleminfo_reply_cancels_fallback(self):
        # A reply that arrives before the fallback deadline must cancel it,
        # otherwise the question gets answered twice.
        answer_calls = []
        ws = FakeWebSocket()

        class FakeResponse:
            status_code = 200

            def json(self):
                return {"data": {"slides": [
                    {"coverAlt": "", "problem": {"problemId": "qg",
                     "problemType": 1, "body": "choice", "options": [], "answers": []}}
                ]}}

        original_get = listening_socket.requests.get
        original_answer = listening_socket.answer
        original_fallback = listening_socket.PROBLEMINFO_FALLBACK_SECONDS
        listening_socket.requests.get = lambda **_kwargs: FakeResponse()
        # False keeps the question unanswered so a stale fallback would fire.
        listening_socket.answer = lambda **kwargs: (answer_calls.append(kwargs), False)[1]
        listening_socket.PROBLEMINFO_FALLBACK_SECONDS = 0.2
        on_message = listening_socket.on_message_connect(
            ppt_jwt="ppt", lesson_id="lesson", identity_id="user",
            socket_jwt="socket", sleep_second=0,
        )
        try:
            on_message(ws, json.dumps({"op": "hello", "timeline": [
                {"type": "slide", "pres": "p1"}]}))
            on_message.pending_messages.join()
            on_message(ws, json.dumps({"op": "fetchtimeline", "unlockedproblem": ["qg"]}))
            on_message.pending_messages.join()
            on_message(ws, json.dumps({"op": "probleminfo", "problemid": "qg",
                                       "limit": 60, "now": 0, "dt": 0}))
            on_message.pending_messages.join()
            for _ in range(100):
                if answer_calls:
                    break
                time.sleep(0.02)
            time.sleep(0.4)  # well past the 0.2s fallback window
        finally:
            on_message.stop_processing()
            on_message.pending_messages.join()
            listening_socket.requests.get = original_get
            listening_socket.answer = original_answer
            listening_socket.PROBLEMINFO_FALLBACK_SECONDS = original_fallback

        self.assertEqual(1, len(answer_calls))

    def test_terminal_question_is_dropped_and_not_requeried(self):
        answer_calls = []
        ws = FakeWebSocket()

        class FakeResponse:
            status_code = 200

            def json(self):
                return {"data": {"slides": [
                    {"coverAlt": "", "problem": {"problemId": "qh",
                     "problemType": 5, "body": "closed", "options": None, "answers": []}}
                ]}}

        original_get = listening_socket.requests.get
        original_answer = listening_socket.answer
        original_fallback = listening_socket.PROBLEMINFO_FALLBACK_SECONDS
        listening_socket.requests.get = lambda **_kwargs: FakeResponse()
        listening_socket.answer = lambda **kwargs: (answer_calls.append(kwargs), "terminal")[1]
        listening_socket.PROBLEMINFO_FALLBACK_SECONDS = 0.05
        on_message = listening_socket.on_message_connect(
            ppt_jwt="ppt", lesson_id="lesson", identity_id="user",
            socket_jwt="socket", sleep_second=0,
        )
        try:
            on_message(ws, json.dumps({"op": "hello", "timeline": [
                {"type": "slide", "pres": "p1"}]}))
            on_message.pending_messages.join()
            on_message(ws, json.dumps({"op": "fetchtimeline", "unlockedproblem": ["qh"]}))
            on_message.pending_messages.join()
            on_message(ws, json.dumps({"op": "probleminfo", "problemid": "qh",
                                       "limit": 60, "now": 0, "dt": 0}))
            on_message.pending_messages.join()
            for _ in range(100):
                if answer_calls:
                    break
                time.sleep(0.02)
            time.sleep(0.2)
            # Another poll arrives: the closed question must not be queried
            # or answered again.
            on_message(ws, json.dumps({"op": "fetchtimeline", "unlockedproblem": ["qh"]}))
            on_message.pending_messages.join()
            time.sleep(0.2)
        finally:
            on_message.stop_processing()
            on_message.pending_messages.join()
            listening_socket.requests.get = original_get
            listening_socket.answer = original_answer
            listening_socket.PROBLEMINFO_FALLBACK_SECONDS = original_fallback

        self.assertEqual(1, len(answer_calls))
        queries = [p for p in ws.sent if json.loads(p)["op"] == "probleminfo"]
        self.assertEqual(1, len(queries))


if __name__ == "__main__":
    unittest.main()
