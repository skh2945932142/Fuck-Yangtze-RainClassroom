# Zeabur 部署指南

本分支（`zeabur-deploy`）将原"cron 单次运行"的项目改造为**常驻守护进程**，适配 Zeabur 等容器平台。

## 与上游的差异

| 改动 | 原因 |
|---|---|
| `start.py` 改为常驻循环（默认每 5 分钟扫描一次） | Zeabur 服务是长驻进程，单次退出会导致重启循环 |
| 新增 `Dockerfile` | Zeabur 从本地目录部署时用 Docker 构建 |
| `AI_BASE_URL` / `AI_MODEL` 环境变量 | 外接任意 OpenAI 兼容 API（默认仍为 ChatAnywhere + gpt-4o-mini） |
| `YKT_NAME` / `YKT_PASSWORD` / `YKT_LOGIN_TYPE` | SESSION 失效时自动密码重登（见下方限制） |
| PaddleOCR 延迟加载 | 空闲时省约 1GB 内存，仅在遇到图片题时加载 |
| 修复多课并发时全局 headers 污染 | 原代码会串 JWT，导致答案提交到错误的课堂 |

## 环境变量

在 Zeabur 服务的 Variables 页面填写：

| 变量 | 必填 | 说明 |
|---|---|---|
| `SESSION` | ✅ | 雨课堂 sessionid（浏览器 F12 → Cookie 复制），过期后手动更新 |
| `AI_KEY` | ✅ | 你的大模型 API Key |
| `AI_BASE_URL` | ❌ | OpenAI 兼容端点，默认 `https://api.chatanywhere.tech/v1` |
| `AI_MODEL` | ❌ | 模型名，默认 `gpt-4o-mini` |
| `ENNCY_KEY` | ❌ | 言溪题库 key；**不填则跳过题库，纯大模型作答** |
| `FILTERED_COURSES` | ❌ | 需要监听答题的课程名，英文逗号分隔；空 = 全部课程监听 |
| `YKT_NAME` | ❌ | 自动重登用：手机号或邮箱 |
| `YKT_PASSWORD` | ❌ | 自动重登用：密码 |
| `YKT_LOGIN_TYPE` | ❌ | `phone`（默认）或 `email` |

### 外接大模型示例

```bash
# DeepSeek
AI_BASE_URL=https://api.deepseek.com/v1
AI_MODEL=deepseek-chat

# 通义千问（DashScope 兼容模式）
AI_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
AI_MODEL=qwen-plus

# 智谱
AI_BASE_URL=https://open.bigmodel.cn/api/paas/v4
AI_MODEL=glm-4-flash

# OpenRouter
AI_BASE_URL=https://openrouter.ai/api/v1
AI_MODEL=openai/gpt-4o-mini
```

## 自动重登的已知限制

雨课堂登录接口 `/pc/login/verify_pwd_login/` 的密码需要用官方公钥做 RSA 加密（本分支已实现），**但服务端有腾讯防水墙验证码风控**。程序化登录是否放行取决于出口 IP 的风险评分：

- 顺利时：返回新 sessionid，自动完成重登
- 被风控时：返回 `300416 腾讯防水墙校验失败`，此时守护进程会记录日志并保留旧 SESSION，**需要手动刷新 `SESSION` 环境变量**

因此 `YKT_NAME`/`YKT_PASSWORD` 是"尽力而为"的增强，不能保证替代手动更新 SESSION。

## 部署步骤（CLI）

```bash
# 1. 克隆本分支
git clone -b zeabur-deploy https://github.com/skh2945932142/Fuck-Yangtze-RainClassroom.git
cd Fuck-Yangtze-RainClassroom

# 2. 部署（需要 zeabur cli 已登录）
zeabur deploy --create --name rain-classroom --project-id <你的项目ID> -i=false

# 3. 配置环境变量（也可在网页端填写）
zeabur variable create --id <服务ID> --env-id <环境ID> -i=false -y \
  -k "SESSION=<你的sessionid>" \
  -k "AI_KEY=<你的key>" \
  -k "AI_BASE_URL=<端点>" \
  -k "AI_MODEL=<模型>"

# 4. 重启使变量生效
zeabur service restart --id <服务ID> --env-id <环境ID>
```

## 运行日志

在 Zeabur 网页端服务的 Logging 页查看。正常输出示例：

```
雨课堂监听守护进程启动
[2026-09-28 12:00:00] 扫描正在进行的课程...
无课
```

发现课堂并进入监听后会输出课程名、签到状态；老师发题时会打印题目内容、搜题结果与 LLM 回答。
