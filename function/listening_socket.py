import queue
import random
import threading
import time

import requests
import websocket
import json
from config import host, api, headers, question_type, submit_before_deadline_seconds
from util.notice import answer_failed_notice
from util.ai import request_ai
from util.timestamp import get_date_time


def _refresh_lesson_token(lesson_id):
    """Re-checkin to obtain a fresh lesson token (community-verified recovery
    for answer submissions rejected with token-expiry codes like 50004).
    Returns the new Set-Auth JWT or None."""
    try:
        # Imported lazily: check_in imports this module at load time.
        from function.check_in import check_in_on_listening
        response = check_in_on_listening(lesson_id)
        if response.status_code == 200:
            jwt = response.headers.get("Set-Auth")
            if jwt:
                print("[token] 重新签到成功，已获取新 lesson token", flush=True)
                return jwt
    except Exception as error:
        print(f"[token] 重新签到失败: {type(error).__name__}: {error!r}", flush=True)
    return None

def deadline_from_limit(limit, now=None, dt=None):
    """Convert a problem's `limit` (seconds, -1 = no limit; `now`/`dt` are
    server millisecond clocks from probleminfo replies) into an epoch
    deadline. Returns None when the problem has no deadline."""
    if limit is None or int(limit) == -1:
        return None
    if now is not None and dt is not None:
        time_left = int(limit) - (int(now) - int(dt)) / 1000
    else:
        time_left = int(limit)
    return time.time() + max(time_left, 0)


def submit_target(deadline_ts):
    """Target submit moment: deadline minus the hold margin."""
    if deadline_ts is None:
        return None
    return deadline_ts - submit_before_deadline_seconds


PING_INTERVAL_SECONDS = 30
PING_TIMEOUT_SECONDS = 10
RECONNECT_DELAY_SECONDS = 5


def send_if_connected(ws, payload):
    """Send a WebSocket payload only while the underlying connection is open."""
    sock = getattr(ws, "sock", None)
    if not getattr(sock, "connected", False):
        print("WebSocket 未连接，跳过发送", flush=True)
        return False

    try:
        ws.send(json.dumps(payload))
        return True
    except (OSError, websocket.WebSocketException) as error:
        print(f"WebSocket 发送失败: {error!r}", flush=True)
        return False


def on_message_connect(ppt_jwt, lesson_id, identity_id, socket_jwt, sleep_second=10,
                       stop_event=None, answered_problem_ids=None,
                       processing_problem_ids=None, seen_problem_ids=None,
                       problem_state_lock=None, course_name=None):
    problem_list = dict()
    if answered_problem_ids is None:
        answered_problem_ids = set()
    if processing_problem_ids is None:
        processing_problem_ids = set()
    if seen_problem_ids is None:
        seen_problem_ids = set()
    if problem_state_lock is None:
        problem_state_lock = threading.Lock()
    processor_stop = threading.Event()
    messages = queue.Queue()
    reported_errors = set()
    queried_problem_ids = set()  # probleminfo queries already sent
    answer_attempts = {}  # q_id -> attempts made (retry cap per question)
    token_box = {"jwt": ppt_jwt}  # live lesson JWT, refreshed from Set-Auth
    MAX_ANSWER_ATTEMPTS = 3

    def spawn_answer(q_id, delay_until_ts):
        """Answer q_id in its own thread so the message loop never blocks.

        The thread generates the AI answer immediately, then holds the
        submission until delay_until_ts (deadline - margin)."""
        problem = problem_list.get(q_id)
        if problem is None:
            return False
        with problem_state_lock:
            if q_id in answered_problem_ids or q_id in processing_problem_ids:
                return False
            attempts = answer_attempts.get(q_id, 0)
            if attempts >= MAX_ANSWER_ATTEMPTS:
                return False
            answer_attempts[q_id] = attempts + 1
            processing_problem_ids.add(q_id)

        def run():
            try:
                ok = answer(
                    problem_id=q_id,
                    problem_type=problem["type"],
                    problem_content=problem["content"],
                    options=problem["options"],
                    jwt=ppt_jwt,
                    img_url=problem["img_url"],
                    course_name=problem["course_name"],
                    delay_until_ts=delay_until_ts,
                    token_box=token_box,
                    lesson_id=lesson_id,
                )
                if ok:
                    with problem_state_lock:
                        answered_problem_ids.add(q_id)
                        problem_list.pop(q_id, None)
                else:
                    # Submission rejected: allow the poll loop to re-query the
                    # deadline and spawn a fresh attempt (community-verified
                    # recovery: token may have been renewed by now, and the
                    # question is usually still open until its deadline).
                    with problem_state_lock:
                        queried_problem_ids.discard(q_id)
                    print(f"[retry] 题目 {q_id} 提交失败，稍后重试"
                          f"（第 {answer_attempts.get(q_id, MAX_ANSWER_ATTEMPTS)}"
                          f"/{MAX_ANSWER_ATTEMPTS} 次）", flush=True)
            finally:
                with problem_state_lock:
                    processing_problem_ids.discard(q_id)

        threading.Thread(target=run, daemon=True, name=f"answer-{q_id}").start()
        return True

    def query_deadline(ws, q_id):
        """Ask the server for the problem's remaining time (probleminfo).
        Falls back to answering without delay if no reply arrives."""
        with problem_state_lock:
            if (q_id in queried_problem_ids or q_id in answered_problem_ids
                    or q_id in processing_problem_ids):
                return
            attempts = answer_attempts.get(q_id, 0)
            if attempts >= MAX_ANSWER_ATTEMPTS:
                return
            queried_problem_ids.add(q_id)
        send_if_connected(ws, {
            "op": "probleminfo",
            "lessonid": lesson_id,
            "problemid": q_id,
            "msgid": 1,
        })

        def fallback():
            # If the server never replies to probleminfo, answer immediately
            # rather than dropping the question.
            processor_stop.wait(90)
            if stopped():
                return
            with problem_state_lock:
                already = (q_id in answered_problem_ids or q_id in processing_problem_ids)
                queried_problem_ids.discard(q_id)
            if not already:
                print(f"[timer] probleminfo 未回复，题目 {q_id} 无延迟兜底作答", flush=True)
                spawn_answer(q_id, None)

        threading.Thread(target=fallback, daemon=True, name=f"probe-{q_id}").start()

    def stopped():
        return processor_stop.is_set() or (stop_event is not None and stop_event.is_set())

    def process_message(ws, msg_json):
        if stopped():
            return
        action = msg_json.get("op")
        if action == "fetchtimeline":
            # 检查返回timeline的最后一个（最新的时间）是否为problem，是则回答问题
            # time_lines = msg_json.get("timeline", [])
            # 过滤 time_lines["type"]!="problem"移除列表
            time_lines = msg_json.get("unlockedproblem",[])
            # Polling observability: leave evidence of what the server sent so
            # live-class issues (empty unlock lists, unknown IDs) are diagnosable.
            known = [q for q in time_lines if q in problem_list]
            unknown = [q for q in time_lines if q not in problem_list]
            if time_lines or problem_list:
                print(f"[poll] unlocked={len(time_lines)} known={len(known)} "
                      f"unknown={len(unknown)} cached={len(problem_list)} "
                      f"answered={len(answered_problem_ids)}", flush=True)
                if unknown:
                    print(f"[poll] 未缓存的题目ID: {unknown}", flush=True)
            # 最新的题目
            if len(time_lines) == 0:
                # 没题可答，继续获取PPT内容，看看是否老师换了新的PPT文件
                if processor_stop.wait(sleep_second * 2) or stopped():
                    return
                auth_payload = {
                    "op": "hello",
                    "userid": identity_id,
                    "role": "student",
                    "auth": socket_jwt,
                    "lessonid": lesson_id
                }
                send_if_connected(ws, auth_payload)
            else:
                for q_id in time_lines:
                    if stopped():
                        return
                    # Poll replies carry IDs only — ask probleminfo for the
                    # deadline, the answer thread is spawned from that reply
                    # (with a no-delay fallback if the server stays silent).
                    if q_id in problem_list:
                        query_deadline(ws, q_id)
                # 整批处理后只发一次查询，避免多题时请求成倍增长
                if processor_stop.wait(sleep_second) or stopped():
                    return
                send_if_connected(ws, {
                    "op": "fetchtimeline",
                    "lessonid": str(lesson_id),
                    "msgid": 1
                })
        elif action == "probleminfo":
            # Server reply to our deadline query: spawn the delayed answer.
            q_id = msg_json.get("problemid")
            if q_id is not None and q_id in problem_list:
                deadline = deadline_from_limit(
                    msg_json.get("limit"), msg_json.get("now"), msg_json.get("dt"))
                target = submit_target(deadline)
                if target is not None:
                    print(f"[timer] 题目 {q_id} 将于 {target - time.time():.0f} 秒后提交"
                          f"（截止前 {submit_before_deadline_seconds}s）", flush=True)
                spawn_answer(q_id, target)
        elif action == "unlockproblem":
            # Teacher just pushed a problem (server push at publish time) —
            # answer immediately instead of waiting for the next poll cycle.
            # Reference implementations rely on this push, not polling.
            problem = msg_json.get("problem", {})
            q_id = problem.get("prob") or problem.get("problemid")
            if q_id is not None:
                print(f"[push] 收到发题推送: {q_id} (limit={problem.get('limit')})", flush=True)
                cached = problem_list.get(q_id)
                if cached is not None:
                    # Push messages carry the time limit themselves.
                    deadline = deadline_from_limit(problem.get("limit"))
                    target = submit_target(deadline)
                    if target is not None:
                        print(f"[timer] 将于 {target - time.time():.0f} 秒后提交"
                              f"（截止前 {submit_before_deadline_seconds}s）", flush=True)
                    spawn_answer(q_id, target)
                else:
                    # Not cached yet: sweep presentations again (the new
                    # problem may live in an updated PPT); the deadline query
                    # then happens through the poll path.
                    print(f"[push] 题目 {q_id} 未缓存，重新拉取课件", flush=True)
                    send_if_connected(ws, {
                        "op": "fetchtimeline",
                        "lessonid": str(lesson_id),
                        "msgid": 1
                    })
            # keep the poll loop alive
            send_if_connected(ws, {
                "op": "fetchtimeline",
                "lessonid": str(lesson_id),
                "msgid": 1
            })
        elif action in ("presentationupdated", "presentationcreated"):
            # Teacher switched/added a PPT — re-sweep so new problems get
            # cached, then resume polling.
            print(f"[push] 课件更新: {action}", flush=True)
            send_if_connected(ws, {
                "op": "hello",
                "userid": identity_id,
                "role": "student",
                "auth": socket_jwt,
                "lessonid": lesson_id
            })
        else:
            # 首次获取PPT内容，进而保存所有题目
            # 解析出pres_id
            ppt_ids = set()
            if "timeline" in msg_json:
                time_lines = list(msg_json["timeline"])
                # 每一item中type=slide代表每一张PPT，拿到pres后，请求get_ppt接口拿到PPT具体内容，然后进行检测是否有problem
                for item in time_lines:
                    # 是PPT
                    if item["type"] == "slide":
                        ppt_ids.add(item["pres"])
            else:
                return
            # 开始获取PPT
            # copy() is required: the global headers dict is shared by all
            # lesson threads, in-place mutation would leak this lesson's JWT
            # into every other concurrent request.
            new_headers = headers.copy()
            new_headers["Authorization"] = "Bearer " + ppt_jwt
            new_headers["User-Agent"] = (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/129.0.0.0 Safari/537.36 Edg/129.0.0.0")

            new_problem_count = 0
            total_problems_found = 0
            total_slides = 0
            for pres_id in ppt_ids:
                if stopped():
                    return
                url = host + api["get_ppt"].format(pres_id)

                response = requests.get(headers=new_headers, url=url, timeout=15)
                if response.status_code == 200:
                    ppt_pages = response.json()["data"]["slides"]
                    total_slides += len(ppt_pages)
                    slides_with_problem = [p for p in ppt_pages if "problem" in p]
                    total_problems_found += len(slides_with_problem)
                    for ppt in ppt_pages:
                        # 有答题
                        if "problem" in ppt:
                            # print(ppt)
                            # 先保存所有题目，供索引，然后监听socket，对应的问题发送的瞬间进行answer
                            question = ppt["problem"]
                            options = None
                            q_type = question["problemType"]
                            if q_type == 1 or q_type == 2 or q_type == 3:
                                options = question["options"]

                            answered = list(ppt["problem"]["answers"])
                            with problem_state_lock:
                                should_save = (question["problemId"] not in answered_problem_ids
                                               and question["problemId"] not in processing_problem_ids)
                            if len(answered) == 0 and should_save:
                                # 保存
                                save_dict = {
                                    "type": question["problemType"],
                                    "content": question["body"],
                                    "options": options,
                                    "img_url": ppt["coverAlt"],
                                    "course_name": course_name,
                                }
                                with problem_state_lock:
                                    if question["problemId"] not in seen_problem_ids:
                                        seen_problem_ids.add(question["problemId"])
                                        new_problem_count += 1
                                problem_list[question["problemId"]] = save_dict
                else:
                    print(f"获取 PPT 失败，HTTP {response.status_code}", flush=True)
            # 开始监听 定时发送
            # 这是发送一次
            if new_problem_count:
                print(f"发现 {new_problem_count} 道新题，继续监听", flush=True)
            print(f"[ppt] 课件 {len(ppt_ids)} 份共 {total_slides} 页 / "
                  f"含题 {total_problems_found} 道，本回合新缓存 {new_problem_count} 道", flush=True)
            if stopped():
                return
            # hello replies can carry already-unlocked problems (joining after
            # the teacher published) — surface them via the poll immediately.
            send_if_connected(ws, {
                "op": "fetchtimeline",
                "lessonid": str(lesson_id),
                "msgid": 1
            })

    def process_queued_messages():
        while True:
            item = messages.get()
            try:
                if item is None:
                    return
                if not stopped():
                    process_message(*item)
                    reported_errors.clear()
            except Exception as error:
                error_name = type(error).__name__
                if error_name not in reported_errors:
                    print(f"监听消息处理失败: {error_name}，稍后重新获取课堂内容", flush=True)
                    reported_errors.add(error_name)
                if not stopped() and not processor_stop.wait(sleep_second * 2):
                    ws, _ = item
                    send_if_connected(ws, {
                        "op": "hello",
                        "userid": identity_id,
                        "role": "student",
                        "auth": socket_jwt,
                        "lessonid": lesson_id,
                    })
            finally:
                messages.task_done()

    # 保持 WebSocket 的接收回调空闲，以便及时读取心跳回应。
    worker = threading.Thread(target=process_queued_messages, daemon=True)
    worker.start()

    def on_message(ws, message):
        if "lessonfinished" in message:
            print("下课了，停止监听", flush=True)
            if stop_event is not None:
                stop_event.set()
            processor_stop.set()
            ws.close()
            return
        try:
            msg_json = json.loads(message)
        except json.JSONDecodeError:
            print("收到无法解析的 WebSocket 消息", flush=True)
            return
        if not isinstance(msg_json, dict):
            print("收到非对象 WebSocket 消息", flush=True)
            return
        if msg_json.get("op") == "notification" or stopped():
            return
        messages.put((ws, msg_json))

    def stop_processing():
        processor_stop.set()
        messages.put(None)

    on_message.stop_processing = stop_processing
    on_message.pending_messages = messages
    return on_message


def on_error(ws, error):
    print(f"WebSocket 错误: {error!r}", flush=True)


def on_close(ws, close_status_code, close_msg):
    print(
        f"WebSocket 已关闭，code={close_status_code!r}, reason={close_msg!r}",
        flush=True,
    )


def on_open_connet(jwt, lesson_id, identity_id):
    def on_open(ws):
        auth_payload = {
            "op": "hello",
            "userid": identity_id,
            "role": "student",
            "auth": jwt,
            "lessonid": lesson_id
        }
        send_if_connected(ws, auth_payload)

    return on_open


# 监听上课
def start_socket_ppt(ppt_jwt, socket_jwt, lesson_id, identity_id, course_name=None):
    stop_event = threading.Event()
    reconnect_attempt = 0
    answered_problem_ids = set()
    processing_problem_ids = set()
    seen_problem_ids = set()
    problem_state_lock = threading.Lock()

    while not stop_event.is_set():
        on_message = on_message_connect(
            ppt_jwt=ppt_jwt,
            lesson_id=lesson_id,
            identity_id=identity_id,
            socket_jwt=socket_jwt,
            stop_event=stop_event,
            answered_problem_ids=answered_problem_ids,
            processing_problem_ids=processing_problem_ids,
            seen_problem_ids=seen_problem_ids,
            problem_state_lock=problem_state_lock,
            course_name=course_name,
        )
        ws = websocket.WebSocketApp(
            url=api["websocket"],
            on_open=on_open_connet(lesson_id=lesson_id, identity_id=identity_id, jwt=socket_jwt),
            on_message=on_message,
            on_error=on_error,
            on_close=on_close,
        )
        try:
            ws.run_forever(
                ping_interval=PING_INTERVAL_SECONDS,
                ping_timeout=PING_TIMEOUT_SECONDS,
            )
        finally:
            on_message.stop_processing()

        if stop_event.is_set():
            break

        reconnect_attempt += 1
        print(
            f"WebSocket 连接中断，{RECONNECT_DELAY_SECONDS} 秒后重连（第 {reconnect_attempt} 次）",
            flush=True,
        )
        stop_event.wait(RECONNECT_DELAY_SECONDS)


# 多线程 多个上课同时监听
# In-process registry of lessons that already have a listener thread, so the
# 5-minute scan loop doesn't spawn duplicate listeners for the same lesson.
# Keyed by lesson_id; value cleared when the thread exits (lesson finished).
_active_listeners = {}
_listeners_lock = threading.Lock()


def is_lesson_listening(lesson_id):
    with _listeners_lock:
        return _active_listeners.get(lesson_id, False)


def _run_socket_ppt_tracking(lesson_id, ppt_jwt, socket_jwt, identity_id, course_name=None):
    try:
        start_socket_ppt(
            ppt_jwt=ppt_jwt,
            socket_jwt=socket_jwt,
            lesson_id=lesson_id,
            identity_id=identity_id,
            course_name=course_name,
        )
    finally:
        with _listeners_lock:
            _active_listeners.pop(lesson_id, None)
        print(f"课程 {lesson_id} 监听线程退出，后续扫描可重新进入", flush=True)


def start_all_sockets(on_lesson_list):
    started = []
    for item in on_lesson_list:
        lesson_id = item["lesson_id"]
        with _listeners_lock:
            if _active_listeners.get(lesson_id):
                continue  # already listening, skip duplicate
            _active_listeners[lesson_id] = True
        t = threading.Thread(
            target=_run_socket_ppt_tracking,
            kwargs={
                "lesson_id": lesson_id,
                "ppt_jwt": item["ppt_jwt"],
                "socket_jwt": item["socket_jwt"],
                "identity_id": item["identity_id"],
                "course_name": item.get("course_name"),
            }
        )
        t.start()
        started.append(lesson_id)
    return started


# 答题
def format_result(problem_type, answer):
    """Shape the LLM answer array into the payload the v3 answer API expects.

    Cross-checked against a maintained reference implementation
    (infstellar/RainClassroomAssistant): choice/vote questions take a plain
    list, fill-blank takes {"0": "...", "1": "..."} keyed by blank index,
    subjective takes {"content": "...", "pics": []}.
    """
    if problem_type == 4 and isinstance(answer, list):
        return {str(index): value for index, value in enumerate(answer)}
    if problem_type == 5:
        if isinstance(answer, list):
            content = "\n".join(str(value).strip('"') for value in answer)
        else:
            content = str(answer).strip('"')
        return {"content": content, "pics": [{"pic": "", "thumb": ""}]}
    return answer


def answer(problem_id, problem_type, jwt, problem_content, options, img_url, course_name=None,
           delay_until_ts=None, token_box=None, lesson_id=None):
    """Generate the answer first, then hold submission until delay_until_ts.

    delay_until_ts is the target submit moment (epoch seconds), typically
    deadline - SUBMIT_BEFORE_DEADLINE_SECONDS. AI generation happens
    immediately so its latency does not eat into the hold window; the wait
    happens here, inside the answering thread, never in the message loop.
    """
    print(question_type[problem_type], problem_content, options, img_url)

    try:
        raw_answer = request_ai(type=question_type[problem_type], problem=problem_content, options=options, img_url=img_url)
    except Exception as error:
        print(f"AI 调用异常: {type(error).__name__}: {error!r}", flush=True)
        answer_failed_notice(course_name, question_type[problem_type],
                             problem_content, f"AI 调用异常（{type(error).__name__}）")
        return False

    # LLM could not determine the answer (returned empty list): submitting a
    # blank is worse than not submitting — it records an instant blank answer
    # (anomalous behavior signal) and forfeits any partial credit. For choice
    # questions guess a random option (25%+ beats 0%); for text answers skip.
    if not raw_answer or all(not str(item).strip() for item in raw_answer):
        if problem_type in (1, 2, 3) and isinstance(options, list) and options:
            keys = [str(o.get("key") if isinstance(o, dict) else o) for o in options]
            keys = [k for k in keys if k] or [chr(65 + i) for i in range(len(options))]
            if problem_type == 2:
                guess = random.sample(keys, k=max(1, len(keys) // 2))
            else:
                guess = [random.choice(keys)]
            print(f"答案为空，选择题随机作答: {guess}", flush=True)
            raw_answer = guess
        else:
            print("答案为空且非选择题，放弃本次作答", flush=True)
            answer_failed_notice(course_name, question_type[problem_type],
                                 problem_content, "AI 无法判断答案（非选择题，未提交）")
            return False

    # Hold submission until (deadline - margin). The answer is already
    # generated above, so this wait only pads the submit timestamp.
    if delay_until_ts is not None:
        wait = delay_until_ts - time.time()
        if wait > 0:
            print(f"[timer] 答案就绪，{wait:.0f} 秒后提交（截止前 {submit_before_deadline_seconds}s）", flush=True)
            time.sleep(wait)

    post_json = {
        "problemId": problem_id,
        "problemType": problem_type,
        "dt": get_date_time(),
        "result": format_result(problem_type, raw_answer)
    }

    # Same copy() rationale as above: mutating the global headers in place
    # would corrupt requests from other lesson threads. token_box holds the
    # lesson's live JWT: the listener updates it whenever a response carries
    # Set-Auth, so retries never reuse a stale token.
    if token_box is not None and token_box.get("jwt"):
        jwt = token_box["jwt"]
    new_headers = headers.copy()
    new_headers["Authorization"] = "Bearer " + jwt
    new_headers["User-Agent"] = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                                 "Chrome/129.0.0.0 Safari/537.36 Edg/129.0.0.0")

    response = requests.post(url=host + api["answer"], json=post_json, headers=new_headers, timeout=15)

    # Yuketang uses HTTP 200 + business code in the body (e.g. 50000
    # UNAUTHENTICATED), so success must be judged by body code == 0, not the
    # HTTP status. Each response may also carry a fresh Set-Auth token
    # (rolling renewal) — pick it up so retries and later submissions of this
    # lesson use a live token instead of the one from check-in time.
    set_auth = response.headers.get("Set-Auth")
    if set_auth and token_box is not None:
        token_box["jwt"] = set_auth

    body = None
    try:
        body = response.json()
    except ValueError:
        pass
    # Some error paths return a bare JSON string instead of an object.
    body_dict = body if isinstance(body, dict) else {}

    code = body_dict.get("code")
    if response.status_code == 200 and code == 0:
        print("答题成功", flush=True)
        return True

    # Token-expiry style rejections: the lesson token from check-in time has
    # gone stale. Re-checkin for a fresh one so the retry (spawned by the poll
    # loop after this failure) submits with a live token. Any code != 0 gets
    # the same treatment — the re-checkin is cheap and harmless if the token
    # was actually fine.
    if code not in (None, 0) and token_box is not None and lesson_id is not None:
        new_jwt = _refresh_lesson_token(lesson_id)
        if new_jwt:
            token_box["jwt"] = new_jwt

    if body_dict:
        msg = body_dict.get("msg", response.text[:200])
    else:
        msg = response.text[:200]
    print(f"答题失败 HTTP {response.status_code} code={code} msg={msg}", flush=True)
    print(f"提交的 payload: {json.dumps(post_json, ensure_ascii=False)[:300]}", flush=True)
    if msg == "LESSON_END":
        print("题目已经结束", flush=True)
    answer_failed_notice(course_name, question_type[problem_type],
                         problem_content, f"提交被拒（HTTP {response.status_code} code={code}: {msg}）")
    return False
