import json
import os
import smtplib
import threading
import time
from email.header import Header
from email.mime.text import MIMEText

from config import email_host, email_port, email_user, email_pass, to_email

# Don't block the scan loop while SMTP talks to the server.
_send_lock = threading.Lock()

# ---------------------------------------------------------------------------
# One mail per event
#
# A repeating condition (an exam listed for days, an expired session, a
# question that keeps getting rejected) would otherwise mail on every scan
# round. Every notice therefore carries an event key and the ledger below
# remembers which keys were already delivered, so each event mails once.
# Kept in a file rather than a set so a container restart does not re-send.
# ---------------------------------------------------------------------------
STATE_FILE = os.getenv("NOTICE_STATE_FILE", "notified.json")
DEDUPE_TTL_SECONDS = float(os.getenv("NOTICE_DEDUPE_TTL_SECONDS", str(7 * 24 * 3600)))
_state_lock = threading.Lock()

# Event keys owned by this module.
SESSION_EXPIRED_KEY = "session-expired"


def _load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as file:
            state = json.load(file)
        return state if isinstance(state, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as error:
        print(f"通知状态文件读取失败: {error!r}", flush=True)
        return {}


def _save_state(state):
    # Write-then-rename so an interrupted write never leaves a half file.
    temp_path = f"{STATE_FILE}.tmp"
    try:
        with open(temp_path, "w", encoding="utf-8") as file:
            json.dump(state, file, ensure_ascii=False, indent=2)
        os.replace(temp_path, STATE_FILE)
    except Exception as error:
        print(f"通知状态文件写入失败: {error!r}", flush=True)


def _claim(event_key):
    """Reserve an event. Returns True only for the first caller."""
    now = time.time()
    with _state_lock:
        state = {key: sent_at for key, sent_at in _load_state().items()
                 if isinstance(sent_at, (int, float))
                 and now - sent_at < DEDUPE_TTL_SECONDS}
        if event_key in state:
            return False
        state[event_key] = now
        _save_state(state)
        return True


def forget_notice(event_key):
    """Drop an event so a genuinely new occurrence can be mailed again."""
    with _state_lock:
        state = _load_state()
        if state.pop(event_key, None) is not None:
            _save_state(state)


# Where the refresh instructions point the user, per deployment platform.
# Set DEPLOY_PLATFORM to match your setup; auto-detected as a best effort.
_platform = os.getenv("DEPLOY_PLATFORM", "").strip()
if not _platform:
    if os.path.exists("/.dockerenv"):
        _platform = "Docker"
    elif os.path.getsize("/proc/1/cgroup") if os.path.exists("/proc/1/cgroup") else 0:
        # container-ish but not classic docker; Zeabur/k8s pods also land here
        _platform = "容器平台（Zeabur 等）"
    else:
        _platform = "本地"

REFRESH_GUIDE = {
    "Docker": (
        "Docker 部署：更新 .env 里的 SESSION，然后重启容器\n"
        "  docker restart rain-classroom"
    ),
    "容器平台（Zeabur 等）": (
        "容器平台部署：在控制台的 Variables 里更新 SESSION，然后 Redeploy/Restart 服务"
    ),
    "本地": (
        "本地部署：更新环境变量（或 config.ini）里的 SESSION，重新运行 python start.py"
    ),
}


def _refresh_guide():
    return REFRESH_GUIDE.get(_platform, REFRESH_GUIDE["容器平台（Zeabur 等）"])


# 发送邮件提醒 标题、内容（收件人取环境变量 TO_EMAIL，发件箱为用户自己的邮箱）
# event_key: 同一事件的去重键，None 表示不去重
def email_notice(subject: str, content: str, event_key: str = None):
    if not all([email_user, email_pass, to_email]):
        print("邮件配置不完整（EMAIL_USER/EMAIL_PASS/TO_EMAIL），跳过通知", flush=True)
        return

    # Claimed before sending on purpose: the requirement is "at most one mail
    # per event", so a failed delivery must not turn into a mail storm.
    if event_key is not None and not _claim(event_key):
        print(f"同一事件已提醒过，跳过: {event_key}", flush=True)
        return

    threading.Thread(target=_deliver, args=(subject, content), daemon=True).start()


def _deliver(subject, content):
    """Do the SMTP round trip. Split out so tests can capture deliveries."""
    # Serialize sends so repeated failures don't stack up connections.
    with _send_lock:
        try:
            msg = MIMEText(content, 'plain', 'utf-8')
            msg['Subject'] = Header(subject, 'utf-8')
            msg['From'] = email_user
            msg['To'] = to_email
            with smtplib.SMTP_SSL(host=email_host, port=email_port, timeout=15) as server:
                server.login(email_user, email_pass)
                server.sendmail(email_user, [to_email], msg.as_string())
            print(f"邮件已发送: {subject}", flush=True)
        except Exception as e:
            print(f"邮件发送失败: {e!r}", flush=True)


# SESSION 失效提醒（check_in 连续 N 轮失效后调用）
def session_expired_notice(rounds: int):
    email_notice(
        subject="雨课堂 SESSION 已失效，请更新",
        content=(
            f"雨课堂监听服务的 SESSION 已连续 {rounds} 轮失效，自动重登未成功。\n\n"
            "请重新登录 changjiang.yuketang.cn 获取新的 sessionid（F12 → "
            "Application → Cookies → sessionid）。\n\n"
            f"更新方式：{_refresh_guide()}\n\n"
            "（同一次失效只发这封邮件，恢复后自动重置）"
        ),
        event_key=SESSION_EXPIRED_KEY,
    )


# 答题失败提醒（answer() 各失败路径调用）
# problem_id: 题目 id，同一道题只提醒一次
def answer_failed_notice(course_name, question_type_name, problem_content, reason,
                         problem_id=None):
    body = "一道题目作答失败，建议尽快手动前往雨课堂补答：\n\n"
    if course_name:
        body += f"课程：{course_name}\n"
    body += f"题型：{question_type_name}\n"
    if problem_content:
        snippet = str(problem_content)[:80].replace("\n", " ")
        body += f"题目：{snippet}\n"
    body += f"原因：{reason}\n"
    event_key = f"answer-failed:{problem_id}" if problem_id is not None else None
    email_notice(subject="雨课堂答题失败，请手动处理", content=body, event_key=event_key)


# 考试提醒（同一场考试只提醒一次）
def exam_notice(exams):
    """Mail once per exam listed under upcomingExam.

    The API lists upcoming exams for days ahead and repeats the whole list on
    every scan, so the key has to come from the exam identity, not the payload
    order: classroom_id + exam id are stable across scans.
    """
    for exam in exams:
        if not isinstance(exam, dict):
            continue
        exam_id = exam.get("id")
        classroom_id = exam.get("classroom_id")
        if exam_id is None and classroom_id is None:
            # Unknown shape: fall back to title + start time so at least the
            # same listing does not re-send every round.
            event_key = f"exam:{exam.get('title')}:{exam.get('start_time')}"
        else:
            event_key = f"exam:{classroom_id}:{exam_id}"

        title = exam.get("title") or "(未命名考试)"
        classroom = exam.get("classroom_name") or ""
        start = exam.get("start_time")
        end = exam.get("end_time")
        body = f"{title} 已出现在雨课堂考列表中，请前往查看。\n\n"
        if classroom:
            body += f"课程：{classroom}\n"
        if isinstance(start, (int, float)):
            body += f"开始时间：{_beijing_time(start)}\n"
        if isinstance(end, (int, float)):
            body += f"结束时间：{_beijing_time(end)}\n"
        body += "\n（同一场考试只提醒一次）"
        email_notice(subject="雨课堂考试提醒", content=body, event_key=event_key)


def _beijing_time(timestamp):
    # The API speaks epoch seconds; render in UTC+8 without needing a tz db.
    return time.strftime("%Y-%m-%d %H:%M", time.gmtime(timestamp + 8 * 3600))
