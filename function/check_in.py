import requests

from function.listening_socket import start_all_sockets
from function.login import login
from function.user import get_user_name
from config import host, api, headers, log_file_name, check_in_sources, \
    ykt_name, ykt_password, ykt_login_type, set_session_id
from util.file import write_log, read_log
from util.notice import email_notice, session_expired_notice
from util.timestamp import get_now


# 连续失效计数：雨课堂服务端偶发认证抖动（瞬时 UNAUTHENTICATED 后自愈），
# 连续 N 轮失效才认定真过期并发邮件，避免抖动误报
_session_failure_count = 0
SESSION_FAILURE_NOTICE_THRESHOLD = 3
_notice_sent = False


# 获取正在进行的
def get_listening():
    global _session_failure_count, _notice_sent
    response = requests.get(host + api["get_listening"], headers=headers)
    if response.status_code == 200:
        response_data = response.json()
        # Logged-out sessions get HTTP 200 with an UNAUTHENTICATED body
        # instead of a 401, so detect it by payload.
        if response_data.get("code") == 50000 or response_data.get("msg") == "UNAUTHENTICATED":
            _session_failure_count += 1
            print(f"SESSION 失效（连续第 {_session_failure_count} 轮）", flush=True)
            relogin_ok = try_relogin()
            # 重登成功即视为恢复；重登没配置/失败时连续 N 轮后邮件提醒一次
            if relogin_ok:
                _session_failure_count = 0
                _notice_sent = False
            elif (_session_failure_count >= SESSION_FAILURE_NOTICE_THRESHOLD
                  and not _notice_sent):
                session_expired_notice(_session_failure_count)
                _notice_sent = True
            return None
        _session_failure_count = 0
        _notice_sent = False
        return response_data["data"]
    else:
        return None


# 密码重登：失败（验证码门槛/凭据错误）时仅提示，等下一轮扫描用新 SESSION
def try_relogin():
    if not (ykt_name and ykt_password):
        print("未配置 YKT_NAME/YKT_PASSWORD，无法自动重登，请手动更新 SESSION", flush=True)
        return False
    try:
        new_session = login(ykt_name, ykt_password, ykt_login_type)
    except RuntimeError as error:
        print(f"自动重登失败: {error}", flush=True)
        return False
    set_session_id(new_session)
    print("自动重登成功，已更新 SESSION", flush=True)
    return True


# 获取正在进行的课堂并且签到、写日志 新的签到方法
def get_listening_classes_and_sign(filtered_courses: list):
    response = get_listening()
    name = get_user_name()

    # 短期存储查看PPT用的JWT、lessonId等信息
    on_lesson_list = []

    if response is None:
        return None
    else:
        classes = list(response["onLessonClassrooms"])
        if len(classes) == 0:
            print("\n无课")
            return
        else:
            print("\n发现上课")
            for item in classes:
                course_name = item["courseName"]
                lesson_id = item["lessonId"]
                response_sign = check_in_on_listening(lesson_id)

                if response_sign.status_code == 200:
                    status = "签到成功"

                    print(course_name, status)
                    data = response_sign.json()["data"]
                    socket_jwt = data["lessonToken"]

                    jwt = response_sign.headers["Set-Auth"]
                    # print(jwt)
                    identity_id = data["identityId"]

                    def queue_on_listening_task():
                        on_lesson_list.append({
                            "ppt_jwt": jwt,
                            "socket_jwt": socket_jwt,
                            "lesson_id": lesson_id,
                            "identity_id": identity_id,
                            "course_name": course_name
                        })

                    if len(filtered_courses) == 0:  # 无需过滤 全部进入监听队列
                        queue_on_listening_task()
                    else:  # 只监听符合条件的课程 其余的就签到
                        if course_name in filtered_courses:
                            queue_on_listening_task()

                    # 每节课只写一次签到日志，扫描循环重启后不重复追加
                    if not has_in_checked(lesson_id):
                        new_log = {
                            "id": lesson_id,
                            "title": course_name,
                            "name": course_name,
                            "time": get_now(),
                            "student": name,
                            "status": status,
                            "url": "https://changjiang.yuketang.cn/m/v2/lesson/student/" + str(lesson_id)
                        }
                        write_log(log_file_name, new_log)
                else:
                    print("失败", response_sign.status_code, response_sign.text)
            # 所有签到完成后，进行死循环巡查，检查是否出现答题
            start_all_sockets(on_lesson_list)

            # for item in on_lesson_list:
            #     start_socket_ppt(ppt_jwt=item["ppt_jwt"],socket_jwt=item["socket_jwt"], lesson_id=item["lesson_id"], identity_id=item["identity_id"])


# 获取正在进行的考试
def check_exam():
    response = get_listening()
    if response is None:
        return None
    else:
        exams = list(response["upcomingExam"])
        if len(exams) == 0:
            print("无考试")
            return
        else:
            print("发现考试")
            print(exams)
            # 发邮件提醒
            email_notice(subject="雨课堂考试提醒", content="请打开雨课堂")
            return


# 传入lessonId 签到
def check_in_on_listening(lesson_id):
    sign_data = {
        "source": check_in_sources["二维码"],
        "lessonId": str(lesson_id),
        "joinIfNotIn": True
    }

    response_sign = requests.post(host + api["sign_in_class"], headers=headers, json=sign_data)
    return response_sign


# 是否已经签过（写入日志）
def has_in_checked(lesson_id):
    logs = read_log(log_file_name)
    if logs and logs[-1]["id"] == lesson_id:
        print("已签过")
        return True
    else:
        return False


# 收到的课程列表前check_num个全部进行签到、写日志 旧的签到方法 弃用
def check_in_on_latest(check_num=1):
    data = {
        "size": check_num,
        "type": [],
        "beginTime": None,
        "endTime": None
    }

    name = get_user_name()
    # 检查收到消息
    response = requests.post(host + api["get_received"], headers=headers, json=data)
    if response.status_code == 200:
        response_data = response.json()
        # 提取 `data` 列表中的第一个元素信息
        if "data" in response_data and response_data["data"]:
            courseware_info = response_data["data"][0]
            courseware_id = courseware_info.get("coursewareId")
            courseware_title = courseware_info.get("coursewareTitle")
            course_name = courseware_info.get("courseName")

            if has_in_checked(courseware_id):
                return

            print("标题:", courseware_title)
            print("名称:", course_name)

            response_sign = check_in_on_listening(courseware_id)

            if response_sign.status_code == 200:
                status = "签到成功"

                print(name, status)

                # 将签到信息写入文件顶部
                new_log = {
                    "id": courseware_id,
                    "title": courseware_title,
                    "name": course_name,
                    "time": get_now(),
                    "student": name,
                    "status": status,
                    "url": "https://changjiang.yuketang.cn/m/v2/lesson/student/" + str(courseware_id)
                }
                write_log(log_file_name, new_log)
            else:
                print("失败", response_sign.status_code, response_sign.text)
        else:
            print("没有找到数据")
    else:
        print("请求失败:", response.status_code, response.text)
