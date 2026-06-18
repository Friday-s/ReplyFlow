# ReplyFlow 部署指南（同事自助版）

ReplyFlow 是**纯本地**应用：在你自己电脑上跑一个进程，连**你自己的**飞书账号和**你自己的** AI（Claude 或 Codex），所有邮件数据只存在本地，不经过任何共享服务器。每个人各装一套，互不影响。

跟着下面 5 步走，大约 15 分钟跑起来。

---

## 0. 前置（每样装一次）

- **Python 3.9+**（mac 自带）
- **lark-cli**：飞书官方命令行（收发邮件 + 读写多维表都靠它）
- **Claude CLI 或 Codex CLI**：二选一，用来生成回复（你本地已登录的那个就行）
- 可选：**gh**（GitHub CLI，部分外联模板给仓库打分时用）

```bash
git clone https://github.com/Friday-s/ReplyFlow.git
cd ReplyFlow
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt        # 唯一依赖：flask
```

---

## 1. 接入你自己的飞书（自建应用 + 授权）

ReplyFlow 通过 `lark-cli` 用**你自己的飞书账号**收发邮件。第一次要建一个你自己的飞书自建应用：

```bash
# ① 创建你自己的飞书自建应用（会自动开浏览器引导，拿到 App ID/Secret 并写进配置）
lark-cli config init --new

# ② 用你自己的飞书账号授权（扫码 / 点链接），开通邮件+多维表+通讯录权限
lark-cli auth login --domain mail,base,contact
```

- 授权页里把请求的权限**全部勾上同意**。
- **直发邮件**需要 `message:send` 权限，企业可能要**管理员审批**；没批也没关系——ReplyFlow 会**自动降级成"建草稿"**，你去飞书草稿箱点发送即可。审批通过后重新 `lark-cli auth login` 一次，新权限才生效。

验证：`lark-cli auth status` 能看到你的名字 + `tokenStatus: valid` 就对了。

---

## 2. 接入你自己的本地 AI（Claude 或 Codex，二选一）

回复生成支持三种引擎，**任选其一**装好登录即可（界面左下角可切换）：

- **Claude**：装好 `claude` CLI 并登录（`claude` 能正常对话即可）
- **Codex**：装好 `codex` CLI 并登录
- **DeepSeek**（可选）：把 key 写进 `~/.replyflow/deepseek.key`（`chmod 600`）

> 翻译/意图分诊默认用本机 `claude`，所以装 Claude 体验最完整。只用 Codex 也能跑，翻译会走 Claude 回退（没装 Claude 则翻译功能不可用，不影响回复生成）。

---

## 3. 配置（你自己的身份 + 团队表）

```bash
cp .env.example .env      # .env 已被 gitignore，不会提交
# 用编辑器填好 .env，然后：
set -a; source .env; set +a
```

`.env` 里**必须**填：

| 变量 | 填什么 |
|---|---|
| `REPLYFLOW_FROM_ADDRESS` | 你的飞书邮箱（发件人，如 `you@company.com`） |
| `REPLYFLOW_OWNER` | **你在飞书"负责人"字段里的名字**。应用只显示/统计你负责的行。⚠️ 必填，否则会看到全表所有人的行 |
| `REPLYFLOW_BASE_TOKEN` / `REPLYFLOW_TABLE_ID` | 团队 CRM 多维表的 token 和 table id（**找团队负责人私下要，别写进任何公开地方**） |
| `REPLYFLOW_INTERNAL_DOMAINS` | 内部域名，逗号分隔（如 `@company.com,@product.com`）——用来识别"我方"邮件 |

可选：`REPLYFLOW_GOLIVE_TABLE_ID`（"上线记录"表，启用「已上线」分组和「上线」流程）、`REPLYFLOW_BLOOME_IMG`（回复里附的官方介绍图路径）、`REPLYFLOW_INBOX_LIMIT`（一次拉多少封，默认 200，量大可调到 600）。

---

## 4. 跑起来

```bash
python3 webui.py          # 端口 5050，单实例
# 浏览器打开 http://localhost:5050
```

重启：`pkill -f webui.py` 后重跑。

---

## 5. 用之前要知道的几点

- **发送有 5 秒撤销**：点「发送」后 5 秒内可撤销；切到别的邮件 = 确认立刻发出。
- **批量必过审核闸门**：模板/AI 批量只"生成"进「📬 待发送审核」队列，你过目改稿后才真正发出——不会偷偷直发。
- **数据全在本地**：邮件元数据/正文/草稿存 `~/.replyflow/mail_store.sqlite3`，不进仓库、不上传。
- **改 AI 人设/话术**：界面里编辑（存 `~/.replyflow/prompts.json`），默认人设是示例，按你自己的产品/语气改。

---

## 常见问题

- **看到的全是别人的博主？** → `REPLYFLOW_OWNER` 没填或填错，填你飞书"负责人"里的准确名字。
- **发送一直变草稿？** → `message:send` 权限没批；找管理员审批后重新 `lark-cli auth login`。
- **翻译/生成报错？** → 检查 `claude` / `codex` 是否已登录可用。
- **多维表读不到？** → 确认 `lark-cli auth login` 带了 `base` 域、且你对那张团队表有访问权限。
