# Changjiang-RainClassroom-Auto

> 长江雨课堂（changjiang.yuketang.cn）自动签到 + AI 随堂答题守护进程。
> Fork 自 [Chiu-xaH/Fuck-Yangtze-RainClassroom](https://github.com/Chiu-xaH/Fuck-Yangtze-RainClassroom)，经真实课堂验证后大幅改造。

---

## ⚠️ 免责声明

- 本项目**仅供学习与技术研究**（雨课堂 Web 协议分析、LLM 应用实践），请勿用于任何违反你所在学校规定或法律法规的用途。
- 使用自动答题功能**可能违反学校考试/课堂纪律规定**，由此产生的一切后果（包括但不限于成绩作废、纪律处分）由使用者自行承担。
- 本项目与雨课堂、清华大学及其相关方没有任何关联。
- 请勿将其用于替他人代答等商业用途。

---

## ✨ 功能特性

| 功能 | 说明 |
|---|---|
| 🔍 常驻扫描 | 每 5 分钟检查一次是否有正在进行的课堂（间隔可调） |
| ✅ 自动签到 | 发现课堂立即签到，扫描循环重启后幂等不重复 |
| 📡 题目监听 | 开课时预缓存全部 PPT 中的**随堂题**，WebSocket 轮询感知老师发题（约 10 秒内）；⚠️ 仅限随堂题，课后作业/考试不支持（见 FAQ） |
| 🤖 AI 作答 | 接入任意 OpenAI 兼容 API；文本题直接作答 |
| 👁️ 多模态视觉答题 | 图片题（电路图/图表等）把 slide 截图直接发给视觉模型读图作答；视觉不可用自动降级 PaddleOCR 文字识别 |
| 📝 按题型提交 | 单选/多选/投票/填空/主观题各自正确的 payload 格式（修复了上游填空/主观题提交必失败的问题） |
| 🎲 兜底策略 | AI 答不出时：选择题随机作答，文字题放弃提交（不留空白记录） |
| 📧 失效邮件提醒 | SESSION 连续 3 轮失效（约 15 分钟）发邮件提醒，一次失效只发一封 |
| 🔁 稳定性 | 多课并发每课单监听器（去重）、WebSocket 断线自动重连、下课自动退出 |

## 🔧 工作原理

```
┌──────────────┐   每5分钟    ┌─────────────┐   checkin   ┌──────────────┐
│  守护进程     │ ──────────> │ 发现正在上课  │ ─────────> │ 签到并换取双JWT │
└──────────────┘             └─────────────┘             └──────┬───────┘
                                                                │
                             ┌──────────────────────────────────┘
                             ▼
                    ┌───────────────────┐
                    │ WebSocket 连入课堂  │
                    │ (wss://.../wsapp/) │
                    └─────────┬─────────┘
                              │ hello 认证，拉取全部 PPT
                              ▼
                    ┌───────────────────┐
                    │ 题目全量缓存        │  老师点"发题"= 服务端解锁题目ID
                    └─────────┬─────────┘
                              │ 轮询 unlockedproblem（~10s）
                              ▼
                 ┌────────────────────────────────┐
                 │ 发现新题 → AI 作答 → 提交答案     │
                 │ 文本题: 直答                      │
                 │ 图片题: 视觉读图 (降级: OCR)      │
                 └────────────────────────────────┘
```

核心机制：雨课堂的题目嵌在 PPT 里，开课时即可全量拉取；老师"发布题目"只是服务端把题目标记为解锁。因此**读题提前完成，发题到作答的延迟只取决于一次轮询周期 + 模型推理**（通常 15~30 秒）。

## 🍪 获取 sessionid

1. 浏览器访问 [changjiang.yuketang.cn](https://changjiang.yuketang.cn/) 并登录（学校统一认证/微信扫码均可）
2. 按 `F12` 打开开发者工具 → **Application**（应用）→ **Cookies** → `https://changjiang.yuketang.cn`
3. 找到 `sessionid`（32 位字母数字），复制其 **Value**

> 提示：sessionid 有效期数天到两周不等。保持取值时的浏览器登录态，别在别处重复登录（可能顶掉会话）。

## ⚙️ 环境变量

### 必填

| 变量 | 说明 |
|---|---|
| `SESSION` | 雨课堂 sessionid（见上节） |
| `AI_KEY` | 你的大模型 API Key |

### AI 配置（可选）

| 变量 | 默认值 | 说明 |
|---|---|---|
| `AI_BASE_URL` | `https://api.chatanywhere.tech/v1` | 任意 OpenAI 兼容端点 |
| `AI_MODEL` | `gpt-4o-mini` | 模型名，建议用支持视觉的多模态模型 |
| `VISION_MODE` | `auto` | 图片题作答方式：`auto`（自动探测视觉能力）/ `1`（强制视觉）/ `0`（强制 OCR） |
| `SUBJECTIVE_MAX_CHARS` | `200` | 主观/填空题单条答案长度兜底上限 |
| `ENNCY_KEY` | 空 | 言溪题库 key（[获取](https://tk.enncy.cn/)）；不填则纯 AI 作答 |

### 课堂与通知（可选）

| 变量 | 默认值 | 说明 |
|---|---|---|
| `FILTERED_COURSES` | 空 | 需监听答题的课程名，英文逗号分隔；空 = 全部课程 |
| `SCAN_INTERVAL_SECONDS` | `300` | 守护进程内部的课堂扫描间隔（秒）；与 GHA 部署的 cron 触发间隔是两回事 |
| `EMAIL_USER` | 空 | 发件邮箱（如 QQ 邮箱） |
| `EMAIL_PASS` | 空 | 邮箱授权码（QQ 邮箱需在设置中开启 SMTP 并生成授权码，**不是 QQ 密码**） |
| `TO_EMAIL` | 空 | 收件邮箱（可与发件相同） |
| `EMAIL_HOST` | `smtp.qq.com` | SMTP 服务器 |
| `EMAIL_PORT` | `465` | SMTP 端口（SSL） |

### 自动重登（可选，作用有限）

| 变量 | 默认值 | 说明 |
|---|---|---|
| `YKT_NAME` | 空 | 雨课堂手机号或邮箱 |
| `YKT_PASSWORD` | 空 | 雨课堂密码 |
| `YKT_LOGIN_TYPE` | `phone` | `phone` / `email` |

> ⚠️ 实测：雨课堂登录接口被腾讯防水墙验证码保护，程序化密码登录基本会被拒（`300416`），**自动重登大概率失败**，仅作尽力尝试。SESSION 失效的可靠方案仍是邮件提醒 + 手动更新。

## 🚀 部署方式

### 方式 A：Zeabur（推荐，免服务器）

PaaS 部署，Docker 构建，容器常驻运行，控制台可看日志。

```bash
# 1. Fork 本仓库，克隆
git clone https://github.com/<你的用户名>/Fuck-Yangtze-RainClassroom.git
cd Fuck-Yangtze-RainClassroom

# 2. 安装 Zeabur CLI 并登录
npm install -g zeabur            # Linux 报 EACCES 时改用: npm install -g --prefix ~/.local zeabur
zeabur auth login

# 3. 部署（项目 ID 用 zeabur project list 查）
zeabur deploy --create --name rain-classroom --project-id <项目ID> -i=false

# 4. 配置环境变量（也可在网页控制台填写）
zeabur variable create --id <服务ID> --env-id <环境ID> -i=false -y \
  -k "SESSION=<你的sessionid>" \
  -k "AI_KEY=<你的key>" \
  -k "AI_BASE_URL=<端点>" \
  -k "AI_MODEL=<模型>"

# 5. 重启生效（变量只在部署时注入）
zeabur service restart --id <服务ID> --env-id <环境ID> -i=false
```

也可直接在 [Zeabur 控制台](https://zeabur.com) 网页上从 Git 创建服务，选本仓库即可（仓库自带 Dockerfile）。

> 注意：环境变量**只在部署时注入容器**，改完变量需 Redeploy/Restart 才生效。

### 方式 B：GitHub Actions（完全免费）

无需任何服务器，利用 GitHub 托管 runner 定时运行。

1. Fork 本仓库
2. 仓库 **Settings → Secrets and variables → Actions** 添加 Secrets：
   - `SESSION`（必填）、`AI_KEY`（必填）
   - 可选：`AI_BASE_URL`、`AI_MODEL`、`ENNCY_KEY`、`FILTERED_COURSES`、`EMAIL_USER`、`EMAIL_PASS`、`TO_EMAIL`
3. **Actions** 页启用 `Rain classroom auto answer (scheduled)` workflow

行为说明：

- cron 按学校上课时段（北京时间周一至五 7:00–20:00）**每 30 分钟**触发一次
- `start.py` 是常驻进程，workflow 设了 **60 分钟超时**：单次 run 覆盖一个课时段的前 60 分钟，到点退出后由下一次 cron 接力（课堂中段的题目靠已建立的 WebSocket 监听，不依赖新 run）
- `VISION_MODE` 读取的是仓库 **Variables**（Settings → Secrets and variables → Actions → Variables 标签页），其余配置读取 **Secrets**——两者位置不同，配错会读到空值
- **分钟数**：公开仓库（本项目的任何 fork）使用标准 runner 完全免费且不限量；私有克隆受 2000 分钟/月免费额度限制，本模式上课日全天约 13 小时 runner 时间会迅速超限——私有部署请选方式 A/C/D。监听/答题不产生额外分钟消耗（每个 run 固定跑满 60 分钟超时，与是否检测到课无关）
- **60 天自动停用**：仓库连续 60 天无 commit 后 GitHub 会自动停掉定时 workflow——长期使用需定期推送任意提交，或在 Actions 页发现停用后手动 Enable
- GitHub Actions 的 cron **不保证准时**（可能延迟数分钟甚至跳过），高峰时段定时任务会被限流；重要课程建议用方式 A/C/D
- 长课堂注意：单次 run 60 分钟超时后由下一次 cron 接力（间隙最长约 30 分钟）；间隙内发布的题不会丢（重进后自动补答未答题），但若题目在间隙内关闭则错过
- runner 在海外，网络到雨课堂服务端一般无碍，但 AI 端点需可公网访问
- 每次 run 安装依赖约 1~2 分钟（paddleocr 较大），属于正常等待

### 方式 C：Docker（自有服务器 / NAS）

```bash
git clone https://github.com/<你的用户名>/Fuck-Yangtze-RainClassroom.git
cd Fuck-Yangtze-RainClassroom

# 环境变量写进 .env 文件
cat > .env <<'EOF'
SESSION=你的sessionid
AI_KEY=你的key
AI_BASE_URL=https://api.deepseek.com/v1
AI_MODEL=deepseek-chat
FILTERED_COURSES=
# 失效/答题失败邮件提醒（用你自己的邮箱发送）
EMAIL_USER=你的邮箱@qq.com
EMAIL_PASS=邮箱SMTP授权码
TO_EMAIL=你的邮箱@qq.com
EOF

docker build -t rain-classroom .
docker run -d --name rain-classroom --restart unless-stopped --env-file .env rain-classroom

# 看日志
docker logs -f rain-classroom
```

> 邮件提醒通过你自己的邮箱 SMTP 发送（发件人 = 收件人 = 你）。QQ 邮箱需先在 **设置 → 账户 → POP3/SMTP 服务** 中开启并生成**授权码**（不是 QQ 密码）。SESSION 连续 3 轮失效或答题失败时你会收到带处理指引的邮件（Docker 部署的指引是"更新 .env 后 `docker restart`"）。

### 方式 D：本地运行（最简单）

```bash
git clone https://github.com/<你的用户名>/Fuck-Yangtze-RainClassroom.git
cd Fuck-Yangtze-RainClassroom

python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 方式 D-1：环境变量（推荐）
export SESSION=xxx AI_KEY=xxx AI_BASE_URL=xxx AI_MODEL=xxx
python start.py

# 方式 D-2：config.ini（设置 IS_LOCAL=1 后从文件读取）
export IS_LOCAL=1
# 编辑 config.ini（按行序：第1行 SESSION、第2行 AI_KEY、第3行 ENNCY_KEY、第4行 课程过滤）
python start.py
```

> 本地运行的电脑需在上课时段保持开机联网。

## 🧠 AI 端点配置示例

任何 OpenAI 兼容的 chat completions 端点均可：

| 提供商 | `AI_BASE_URL` | `AI_MODEL` 示例 | 视觉 |
|---|---|---|---|
| DeepSeek | `https://api.deepseek.com/v1` | `deepseek-chat` | ❌ |
| 智谱 GLM | `https://open.bigmodel.cn/api/paas/v4` | `glm-4v-flash` | ✅ |
| 通义千问 | `https://dashscope.aliyuncs.com/compatible-mode/v1` | `qwen-vl-plus` | ✅ |
| OpenRouter | `https://openrouter.ai/api/v1` | `openai/gpt-4o-mini` | ✅ |
| ChatAnywhere | `https://api.chatanywhere.tech/v1` | `gpt-4o-mini` | ❌ |

> 图片题（电路图等）需要视觉能力才能读图作答；纯文本模型会自动降级 OCR（只能识别图里的文字，看不懂图形结构）。**推荐搭配一个多模态模型**。

## ❓ FAQ

**Q: SESSION 多久失效？失效了怎么办？**
数天到两周不等。配置了邮箱提醒（`EMAIL_USER`/`EMAIL_PASS`/`TO_EMAIL`）后，连续 3 轮扫描失效会收到邮件；按邮件指引重新取 sessionid，更新到部署平台的环境变量并重启即可。手机上操作全流程约 5 分钟（浏览器登录 → F12 取值 → 平台控制台更新）。

**Q: 自动重登（YKT_NAME/YKT_PASSWORD）能用吗？**
实测被腾讯防水墙拦截（`300416 腾讯防水墙校验失败`），密码登录接口强制要求验证码 ticket，程序化登录基本不可行。配置了会在失效时自动尝试一次，失败则走邮件提醒，无副作用。

**Q: 图片题答空了是怎么回事？**
图片题的题干和选项内容往往在 slide 截图里。视觉模式下模型直接读图作答；若模型不支持视觉或图片下载失败，降级 OCR 提取文字。两者都拿不到有效信息时，选择题会随机作答、文字题放弃提交。

**Q: 日志在哪里看？**
Zeabur/Docker 看容器日志；本地直接看终端输出。关键日志行：`发现上课`（进入课堂）、`发现 N 道新题`（题目缓存）、`单选题/多选题/主观题 …`（开始作答）、`答题成功`/`答题失败`（提交结果）。

**Q: 支持普通版雨课堂（www.yuketang.cn）或其他学校版吗？**
本项目针对**长江雨课堂**（changjiang.yuketang.cn）。理论上改 `config.py` 里的 `host` 与 WebSocket 地址可适配其他部署（荷塘/黄河雨课堂），未验证。

**Q: 老师用"随机抽题"或课后回放发的题能答吗？**
不能。本项目依赖"开课时题目已在 PPT 中可预缓存"这一机制；发布时才生成/下发的题目（随机抽题、临时新建题）拿不到题面。

**Q: 能监听并自动写课后作业/试卷吗？**
不能，且这是**架构性边界**而非参数可调的问题。随堂题与作业是两套独立系统：

| | 随堂题（本项目支持） | 作业/考试（不支持） |
|---|---|---|
| 接口 | 课堂 WebSocket + `api/v3/lesson/problem/answer` | `mooc-api/v1/lms/exercise/*`（get_exercise_list / problem_apply） |
| 题目来源 | 嵌在课件 PPT 里，开课即可拉取 | 独立的作业条目（leaf_type=6）内，需逐课程轮询 |
| 题干保护 | 无 | **加密字体反爬**（题干字符经自定义字体映射混淆，需解密表才能读到文字） |
| 提交格式 | `result` 按题型结构化 | `answer` 字段，选择题是连写字母串（如 `"ABD"`） |

作业自动化的完整链路（课程列表 → 作业条目轮询 → 字体解密 → 答题 → 提交）与本项目现有代码几乎不共享，且加密字体解密本身是一个独立的对抗工程。有此需求建议参考专门做作业的项目（如 darkneu/yuketang-auto-script、heyblackC/yuketangHelper 等，自行评估其维护状态与安全性）。

## ⚠️ 已知限制

- 依赖题目预缓存机制，随机抽题/临时发题场景失效
- **仅支持课堂随堂题**：课后作业/试卷走独立的 mooc-api 接口体系（含加密字体反爬），不在本项目范围内（详见 FAQ）
- 自动答题行为（秒答、作答时间）可能被教学平台统计分析识别
- 雨课堂协议变更会导致功能失效（接口路径、WebSocket 消息格式）
- 高峰期雨课堂服务端偶发认证抖动（瞬时 `UNAUTHENTICATED` 后自愈），守护进程已做容忍

## 🛠️ 开发

```bash
# 运行测试（8 个用例：消息处理/去重/重连/答题格式等）
python -m unittest discover tests -v
```

代码结构：

```
start.py                     # 常驻守护进程入口（扫描循环）
config.py                    # 配置与环境变量
function/check_in.py         # 课堂发现、签到、失效检测与邮件通知
function/listening_socket.py # WebSocket 监听、题目缓存、答题提交
function/login.py            # 密码登录（RSA 加密，受防水墙限制）
util/ai.py                   # LLM 调用（文本/视觉双模式）
util/ocr.py                  # PaddleOCR 延迟加载
util/notice.py               # 邮件通知
tests/                       # 单元测试
```

## 📜 致谢与许可

- 上游项目：[Chiu-xaH/Fuck-Yangtze-RainClassroom](https://github.com/Chiu-xaH/Fuck-Yangtze-RainClassroom)（Apache-2.0）
- AI 部分参考：[tinyvan/SecondClass](https://github.com/tinyvan/SecondClass)
- 答题提交格式参考：[infstellar/RainClassroomAssistant](https://github.com/infstellar/RainClassroomAssistant)

本项目以 Apache-2.0 许可发布，见 [LICENSE](LICENSE)。
