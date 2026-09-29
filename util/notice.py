import os
import smtplib
import threading
from email.header import Header
from email.mime.text import MIMEText

from config import email_host, email_port, email_user, email_pass, to_email

# Don't block the scan loop while SMTP talks to the server.
_send_lock = threading.Lock()

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
def email_notice(subject: str, content: str):
    if not all([email_user, email_pass, to_email]):
        print("邮件配置不完整（EMAIL_USER/EMAIL_PASS/TO_EMAIL），跳过通知", flush=True)
        return

    def _send():
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

    threading.Thread(target=_send, daemon=True).start()


# SESSION 失效提醒（check_in 连续 N 轮失效后调用）
def session_expired_notice(rounds: int):
    email_notice(
        subject="雨课堂 SESSION 已失效，请更新",
        content=(
            f"雨课堂监听服务的 SESSION 已连续 {rounds} 轮失效，自动重登未成功。\n\n"
            "请重新登录 changjiang.yuketang.cn 获取新的 sessionid（F12 → "
            "Application → Cookies → sessionid）。\n\n"
            f"更新方式：{_refresh_guide()}\n\n"
            "（同一次失效只发这封邮件，恢复后计数重置）"
        ),
    )


# 答题失败提醒（answer() 各失败路径调用）
def answer_failed_notice(course_name, question_type_name, problem_content, reason):
    body = "一道题目作答失败，建议尽快手动前往雨课堂补答：\n\n"
    if course_name:
        body += f"课程：{course_name}\n"
    body += f"题型：{question_type_name}\n"
    if problem_content:
        snippet = str(problem_content)[:80].replace("\n", " ")
        body += f"题目：{snippet}\n"
    body += f"原因：{reason}\n"
    email_notice(subject="雨课堂答题失败，请手动处理", content=body)
