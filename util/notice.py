import smtplib
import threading
from email.header import Header
from email.mime.text import MIMEText

from config import email_host, email_port, email_user, email_pass, to_email

# Don't block the scan loop while SMTP talks to the server.
_send_lock = threading.Lock()


# 发送邮件提醒 标题、内容（收件人取环境变量 TO_EMAIL）
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
