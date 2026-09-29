import os
import threading
import time

from config import filtered_courses
from function.check_in import get_listening_classes_and_sign, check_exam
from util.timestamp import get_now

# The original design was a cron one-shot: run once, then exit. On Zeabur the
# service is a long-running process, so we loop instead: scan for ongoing
# lessons every SCAN_INTERVAL_SECONDS. In-lesson listening sockets keep their
# own threads; this loop only discovers lessons and hands them over.
SCAN_INTERVAL_SECONDS = int(os.getenv("SCAN_INTERVAL_SECONDS", "300"))


def main_loop():
    while True:
        print(f"[{get_now()}] 扫描正在进行的课程...", flush=True)
        try:
            get_listening_classes_and_sign(filtered_courses)
            check_exam()
        except Exception as error:
            # Keep the daemon alive on any scan failure (cookie expiry shows
            # up here as 401s); a crash would just restart the container.
            print(f"扫描失败: {type(error).__name__}: {error!r}", flush=True)
        # get_listening_classes_and_sign blocks while lessons are being
        # listened to, so sleep here is idle time, not extra latency.
        time.sleep(SCAN_INTERVAL_SECONDS)


if __name__ == "__main__":
    print("雨课堂监听守护进程启动", flush=True)
    main_loop()
