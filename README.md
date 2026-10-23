# QQ AI Bot

一个能真正跑在自己 QQ 上的 AI 聊天机器人：**NapCatQQ（OneBot v11）+ DeepSeek**，纯 Python，核心逻辑一千多行，没有重型框架。

```
你：@小D 今天好累啊
小D：又加班？先别管工作了，去洗个热水澡躺会儿吧。
```

## 它能做什么

- **私聊自动回复**，**群聊 @ 或叫名字才回复**（不会插嘴刷屏）
- **记得上下文**：SQLite 持久化，重启不丢，按群/按人隔离，超时自动过期
- **像真人说话**：内置人设 prompt，短句、口语化、不冒 Markdown 星号
- **群管理**：`/on`、`/off` 单独关某个群的回复，`/persona` 给某个群换人设
- **能看图**：发图片、发表情包都能看懂（可选，需要一个多模态模型 Key）
- **稳**：断线指数退避重连、请求重试、并发限制、冷却时间、长消息自动分条、CQ 码转义防注入
- **省钱**：并发上限、上下文轮数/字数上限、按会话冷却，都能配
- **两种接法**：正向 WS（推荐）和反向 WS（服务器部署、NapCat 在别处时用）

## 架构

```
QQ 手机/电脑客户端
        ↕ 腾讯协议
   NapCatQQ（登录你的 QQ，暴露 OneBot v11）
        ↕ WebSocket（正向 3001 / 反向随你）
   本程序  qqbot/
        ├── onebot.py  连接、收发、call_api、重连
        ├── bot.py     要不要回、拼上下文、调模型、发消息
        ├── commands.py 指令
        ├── memory.py  SQLite 记忆
        └── llm.py     OpenAI 兼容接口（默认 DeepSeek）
        ↕ HTTPS
   api.deepseek.com
```

## 快速开始（Windows）

> 本项目就在 `D:\qq-ai-bot`，下面的命令直接在这个目录里执行；想放到别处，整个文件夹复制走即可。

### 1. 装 Python

[python.org](https://www.python.org/downloads/) 下 3.10 以上版本，安装时务必勾选 **Add Python to PATH**。

### 2. 装依赖

```bat
cd /d D:\qq-ai-bot
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

装不动就用国内镜像：`pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple`

> 连 pip 都用不了（公司网络、没权限）？运行 `python scripts/offline_deps.py`，它会直接把 wheel 下载解包到 `.deps`，
> 然后 `set PYTHONPATH=.deps` 再启动即可（`start.bat` 会自动识别这个目录）。

### 3. 准备一个 QQ 小号

**强烈建议用小号**，别拿主号折腾。这个号就是机器人本体。

### 4. 装 NapCatQQ 并登录

1. 从 [NapCatQQ Releases](https://github.com/NapNeko/NapCatQQ/releases) 下载 Windows 一键包（`NapCat.Shell` 或 `NapCat.Win.Installer`），解压/安装后启动。
2. 启动日志里会打印 WebUI 地址和 token，形如
   `WebUi User Panel Url: http://127.0.0.1:6099/webui?token=xxxxx`
   （WebUI 默认端口 **6099**）。
3. 浏览器打开，先用 token 登录，按提示**改一次密码**，然后进「QQ 登录」扫码登录小号。

### 5. 在 NapCat 里开一个 WebSocket 服务端

NapCat WebUI → **网络配置** → **新建** → 选 **WebSocket 服务端**（这就是正向 WS）：

| 项目 | 填什么 |
| --- | --- |
| 名称 | `qqbot`（随便） |
| Host | `0.0.0.0` |
| 端口 | `3001` |
| 消息格式 | **array**（重要，别选 string） |
| Token | 自己设一个随机字符串，比如 `k8s2-qqbot-token` |
| 保存时启用 | **勾上** |

保存后，访问 `http://127.0.0.1:3001` 打不开是正常的（WebSocket 不响应普通网页请求）。

### 6. 填 DeepSeek API Key

到 [platform.deepseek.com/api_keys](https://platform.deepseek.com/api_keys) 创建一个 Key，然后：

```bat
copy .env.example .env
notepad .env
```

把 `DEEPSEEK_API_KEY=sk-xxxx` 换成你自己的，`NAPCAT_TOKEN` 填第 5 步设的 token。

### 7. 自检 + 启动

```bat
python run.py --check
python run.py
```

`--check` 会打印连接方式、模型、触发规则，配置写错会直接告诉你哪一行有问题。

看到

```
[12:00:00] INFO    qqbot.onebot | 已连接 OneBot：ws://127.0.0.1:3001?access_token=***
[12:00:00] INFO    qqbot.onebot | 当前登录账号：小D（123456789）
```

就成功了。私聊机器人，或者在群里 @ 它试试。

> **懒人模式**：双击 `一键启动.bat` —— 它会先检查 NapCat 是否已在运行（没运行就拉起来，弹 UAC 点「是」），
> 等 3001 端口就绪后再启动机器人。第一次装依赖时先双击 `install.bat`。
>
> 只启动机器人（NapCat 已开着）就双击 `start.bat`。**顺序永远是：先 NapCat，后机器人。**

## 配置说明

全部配置在 `config.yaml`，改完重启生效。`${VAR}` 会从 `.env` 或系统环境变量里取。

### 最重要的几项

| 配置 | 说明 |
| --- | --- |
| `bot.name` | 机器人名字，AI 用它自称，同时也是默认唤醒词 |
| `bot.admins` | 管理员 QQ 号列表，能 `/on` `/off` `/persona` `/status`。**留空时私聊里人人都是管理员**（自用够） |
| `bot.trigger.groups` | 只理这些群；`[]` = 所有群 |
| `bot.trigger.require_mention` | `true` 群里必须 @ 或叫名字；`false` 每句话都回（吵，费钱） |
| `bot.trigger.respond_to_images` | `true` 群里有人发图片/表情包也回（不用 @），有人图多的话会吵 |
| `bot.trigger.wake_words` | 唤醒词，包含即触发，不区分大小写 |
| `bot.trigger.prefix` | 前缀触发，比如设成 `ai`，则「ai 你好」也会回 |
| `llm.api_key` | DeepSeek Key，建议放 `.env` |
| `llm.model` | `deepseek-chat`（快、便宜）或 `deepseek-reasoner`（推理强、贵） |
| `llm.max_history_turns` | 带几轮上下文，默认 10 轮 |
| `llm.system_prompt` | **人设，最值得改的地方**，`{bot_name}` 和 `{current_info}` 会被自动替换 |
| `bot.concurrency` | 同时处理几条消息，控制并发和花费 |
| `bot.cooldown_seconds` | 同一会话两次回复的最小间隔，防刷屏 |

### 完整配置项

<details>
<summary>点开看全部字段</summary>

```yaml
qq:
  mode: forward            # forward / reverse
  ws_url: ws://127.0.0.1:3001
  access_token: ""         # 与 NapCat 里的 token 一致
  self_id: 0               # 0 = 自动获取
  reverse_host: 0.0.0.0    # 仅 reverse 模式
  reverse_port: 3001
  reverse_path: /onebot
  reconnect_min: 1.0
  reconnect_max: 30.0

bot:
  name: 小D
  command_prefix: "/"
  admins: []
  reply_with_at: true      # 群里回复时 @ 对方
  reply_with_quote: false  # 引用对方消息再回
  max_reply_chars: 1200    # 超过自动分条
  concurrency: 4
  cooldown_seconds: 3
  empty_mention_reply: "在的，怎么啦？"
  timezone: Asia/Shanghai
  deny_users: []
  trigger:
    private: true
    groups: []
    exclude_groups: []
    require_mention: true
    respond_to_at_all: false
    respond_to_images: false
    wake_words: ["小D"]
    prefix: ""

llm:
  base_url: https://api.deepseek.com
  api_key: ""
  model: deepseek-chat
  temperature: 0.8
  max_tokens: 1024
  timeout: 60
  max_history_turns: 10
  max_history_chars: 4000
  history_ttl_hours: 24
  system_prompt: |
    ...

vision:
  enabled: false
  base_url: https://api.siliconflow.cn/v1
  api_key: ""
  model: Qwen/Qwen3-VL-30B-A3B-Instruct
  timeout: 60
  max_images: 3
  max_bytes: 4194304
  prompt: |
    ...

memory:
  db_path: data/memory.db
  prune_interval_minutes: 30

log:
  level: INFO
  file: data/bot.log
```

</details>

> 配置项写错名字会直接报错并指出字段名，不会静默忽略。

## 指令

| 指令 | 谁能用 | 作用 |
| --- | --- | --- |
| `/help` | 所有人 | 指令表 |
| `/reset` | 所有人 | 清空当前会话记忆 |
| `/persona` | 所有人 | 查看当前人设 |
| `/persona <内容>` | 私聊本人 / 群管理员 | 修改当前会话人设 |
| `/persona reset` | 同上 | 恢复默认人设 |
| `/on` | 管理员 | 打开本群/本会话回复 |
| `/off` | 管理员 | 关闭本群/本会话回复 |
| `/status` | 管理员 | 运行时长、消息数、token 用量 |

指令只有在机器人「被叫到」时才会处理（私聊随时，群里要 @ 或叫名字），不会误伤正常聊天。

## 常见问题

**连不上 `ws://127.0.0.1:3001`**
NapCat 没启动、没登录成功，或者第 5 步的 WS 服务端没启用。看 NapCat 日志和 WebUI 里那个开关。端口被占用的话 NapCat 端口可能变成 3001→3002，改 `ws_url` 对应即可。

**发了消息没反应**
1. 群里必须 @ 它或叫名字（看 `wake_words`）；2. 群号在 `exclude_groups` 里或不在 `groups` 白名单里；3. 之前发过 `/off`；4. `self_id` 不对——`--check` 时看「机器人」那行。

**回复「我的 API Key 好像不对」**
`.env` 里 Key 错了、过期了，或者 `config.yaml` 里 `api_key` 覆盖成了空字符串。注意 `.env` 里不要带引号和空格。

**回复「钱包空了」**
DeepSeek 账户余额不足，充值即可。想省钱：调小 `max_history_turns`、`max_tokens`，调大 `cooldown_seconds`。

**它说话太长 / 太机器味**
改 `llm.system_prompt`。默认人设已经要求短句、不用 Markdown，你可以加自己的要求，比如「每次不超过 30 个字」。

**机器人会不会封号**
用的是非官方协议，有小号被风控的可能。建议：小号、别频繁群发、别做营销、别秒回所有人。真在意稳定性就用[官方 QQ 机器人](https://q.qq.com/)，那是另一套接口（需要公网回调），本项目没走那条路。

**怎么让它只回某个群**
```yaml
bot:
  trigger:
    groups: [123456789]
```

## 部署到服务器（Linux）

```bash
git clone <你的仓库> /opt/qq-ai-bot && cd /opt/qq-ai-bot
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env && vim .env      # 填 Key
.venv/bin/python run.py --check
```

NapCat 建议单独用 Docker 或一键脚本跑在同一个网络里，然后把 `qq.ws_url` 指向它的地址。

systemd 常驻（`deploy/qqbot.service` 里已写好模板）：

```bash
sudo cp deploy/qqbot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now qqbot
sudo journalctl -u qqbot -f
```

### 反向 WS 模式

如果 NapCat 在内网/另一台机器，不方便让本程序主动连，就反过来：

```yaml
qq:
  mode: reverse
  reverse_host: 0.0.0.0
  reverse_port: 3001
  reverse_path: /onebot
  access_token: "换成你的随机串"
```

然后在 NapCat WebUI 里新建 **WebSocket 客户端**，URL 填
`ws://你的服务器IP:3001/onebot?access_token=你的token`，token 处也填同一个值。

## 换模型

任何 OpenAI 兼容接口都行，只改三行：

```yaml
llm:
  base_url: "https://api.siliconflow.cn/v1"
  api_key: "${SILICONFLOW_API_KEY}"
  model: "deepseek-ai/DeepSeek-V3"
```

本地 Ollama：`base_url: http://127.0.0.1:11434/v1`、`api_key: ollama`、`model: qwen2.5:7b`。

## 让它看懂图片和表情包

DeepSeek 的 `deepseek-chat` 是**纯文本**模型，看不了图。所以这里做成「两个模型分工」：

```
图片/表情包 ──→ 视觉模型（眼睛）──→ 一句中文描述
                                      ↓
                          拼进用户消息：[图片：一只橘猫趴在键盘上]
                                      ↓
                         DeepSeek（大脑）接着按人设聊天
```

好处是聊天风格、人设、上下文记忆完全不变，只是多了一双眼睛。

### 1. 拿一个多模态模型的 Key

任选其一（都走 OpenAI 兼容格式）：

| 平台 | base_url | 推荐模型 | 说明 |
| --- | --- | --- | --- |
| [硅基流动](https://cloud.siliconflow.cn) | `https://api.siliconflow.cn/v1` | `Qwen/Qwen3-VL-30B-A3B-Instruct` | 注册送额度，便宜，默认选这个 |
| [智谱](https://open.bigmodel.cn) | `https://open.bigmodel.cn/api/paas/v4` | `glm-4v-flash` | 免费 |
| [通义千问](https://dashscope.aliyun.com) | `https://dashscope.aliyuncs.com/compatible-mode/v1` | `qwen-vl-max` | 效果最好，稍贵 |

### 2. 填 Key 并打开开关

`.env` 里：

```ini
VISION_API_KEY=你的key
```

`config.yaml` 里把 `enabled` 改成 `true`（换平台的话顺手改 `base_url` 和 `model`）：

```yaml
vision:
  enabled: true
  base_url: "https://api.siliconflow.cn/v1"
  model: "Qwen/Qwen3-VL-30B-A3B-Instruct"
```

### 3. 重启，然后发张图

直接发图片、发个表情包，或者「@它 + 图片 + 一句话」都行。日志里会多一行 `看图结果：...`。

细节：

- 图片会先下载并转成 `data:` 编码再发给视觉模型，**不依赖模型服务去抓腾讯图床**，国内更稳
- 单张图片超过 `max_bytes`（默认 4 MB）或识别失败，会安静降级成 `[图片]`，不会让这条消息失败
- 一条消息最多看 `max_images` 张（默认 3 张），避免又慢又贵
- 表情包会额外标注「这是一张表情包」，让模型知道要按梗来理解

## 让它发表情包

机器人可以直接发你本地的表情包图片。

1. 把图片（png / jpg / gif / webp）丢进 `D:\qq-ai-bot\stickers\`
2. **文件名就是触发词**：`开心.png` → `[图:开心]`
3. 重启机器人

系统提示里会自动列出可用名字，模型在觉得合适时会自己插 `[图:名字]`，程序把它换成图片发出去。

```yaml
bot:
  stickers_dir: "stickers"          # 表情包目录
  max_stickers_per_reply: 1         # 一条回复最多几张
```

细节：

- 文字和表情包会分成两条消息发（先文字，再图），QQ 客户端看着更自然
- 名字匹配是「精确 → 忽略大小写 → 包含」，模型写成 `[图:开心一点]` 也能命中 `开心.png`
- 名字对不上就静默丢掉这个标记，不会把 `[图:xxx]` 这串字发出去
- 单张超过 4 MB 会跳过
- 不用表情包就留空目录，功能自动关闭

> 另外 QQ 内置小黄脸是用 Unicode emoji 表达的（😏😂），模型本来就会用，不需要配置。

## 测试

```bash
# 单元测试：解析、切分、配置、记忆、触发规则、指令、看图（40 项）
python -m unittest discover -s tests -t . -v

# 端到端：本地假 NapCat + 假 DeepSeek，跑通整条链路（不联网）
python scripts/e2e_smoke.py

# 看图链路：本地假图床 + 假多模态模型（不联网、不花钱）
python scripts/e2e_vision.py
```

`e2e_smoke.py` 会验证：自动识别 QQ 号、私聊/群聊收发、群里没 @ 不回、@ 了 @ 回去、指令不走模型、记忆落库、反向连接 token 校验，以及 API Key 错误时能不能好好说话（不崩、不写脏数据）。

## 目录结构

```
qq-ai-bot/
├── config.yaml           # 全部配置，注释齐全
├── .env.example          # Key 和 token 模板
├── run.py                # 入口
├── start.bat / install.bat / start.sh
├── qqbot/
│   ├── __main__.py       # 命令行、启动横幅、优雅退出
│   ├── config.py         # YAML + 环境变量 + 严格校验
│   ├── onebot.py         # OneBot v11 连接层（正向/反向 WS、重连）
│   ├── events.py         # 事件与消息段模型、CQ 码解析
│   ├── bot.py            # 触发判断、上下文、调模型、发送
│   ├── llm.py            # OpenAI 兼容客户端、重试、错误翻译
│   ├── memory.py         # SQLite 会话记忆
│   ├── commands.py       # /help /reset /persona /on /off /status
│   ├── utils.py          # 转义、切分、文本清理
│   └── logger.py
├── scripts/
│   ├── e2e_smoke.py      # 端到端联调
│   └── offline_deps.py   # pip 用不了时的离线装依赖
├── deploy/qqbot.service
└── tests/
```

## 想继续加功能

代码结构就是照着「加功能只改一处」来拆的，几个常见方向：

- **看图说话**：`events.py` 里 `image` 段目前只变成 `[图片]`。接多模态模型时，把图片 URL（NapCat 的 `file` 字段或 `get_image` 接口）塞进 `content` 数组即可。
- **联网搜索**：在 `bot._respond()` 里调模型前先查一次搜索 API，把结果塞进 system 消息。
- **长期记忆**：现在只存对话，可以在 `memory.py` 加一张 `facts` 表，每次对话前检索。
- **多模型路由**：`llm.py` 里按群号/@ 与否选不同 `model`。
- **管理员工具**：`commands.py` 加 `cmd_xxx` 方法就会自动注册，`handle()` 用反射找 `cmd_` 前缀。

## 免责声明

- NapCatQQ 是非官方协议实现，使用风险自负，**请用小号**，并遵守腾讯的用户协议。
- 本项目不存储、不上传你的聊天记录到除模型 API 之外的地方；记忆只存在本地 `data/memory.db`。
- 模型输出由 DeepSeek 生成，内容不代表本项目立场。

## License

MIT
