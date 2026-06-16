"""
ReplyFlow — core business logic shared by the WebUI.
Local Feishu mail reply triage, AI drafting, review queue, CRM updates.
"""

import subprocess, json, re, os, tempfile, time, shutil
from pathlib import Path
from datetime import datetime

def _cfg(name: str, default: str = "") -> str:
    """Read config: REPLYFLOW_<name> preferred, legacy REPLYDESK_<name> as fallback."""
    return os.getenv("REPLYFLOW_" + name) or os.getenv("REPLYDESK_" + name) or default

# Data dir: ~/.replyflow; keep using legacy ~/.replydesk if it already has data.
_NEW_DIR = Path.home() / ".replyflow"
_OLD_DIR = Path.home() / ".replydesk"
APP_DATA_DIR = _OLD_DIR if (_OLD_DIR.exists() and not _NEW_DIR.exists()) else _NEW_DIR
APP_DATA_DIR.mkdir(parents=True, exist_ok=True)
REPO_CACHE_FILE = APP_DATA_DIR / "repo-cache.json"
REPO_CACHE_TTL  = 86400   # 24 小时

def _load_repo_disk_cache() -> dict:
    try:
        if REPO_CACHE_FILE.exists():
            data = json.loads(REPO_CACHE_FILE.read_text())
            now  = time.time()
            return {k: v for k, v in data.items()
                    if now - v.get("_ts", 0) < REPO_CACHE_TTL}
    except Exception:
        pass
    return {}

def _save_repo_disk_cache(cache: dict):
    try:
        REPO_CACHE_FILE.write_text(json.dumps(cache, ensure_ascii=False))
    except Exception:
        pass

# ── Config ──────────────────────────────────────────────────────────────────
BASE_TOKEN   = _cfg("BASE_TOKEN")
TABLE_ID     = _cfg("TABLE_ID")
FROM_ADDRESS = _cfg("FROM_ADDRESS")
VAULT_ROOT   = Path(_cfg("VAULT_ROOT", str(Path.cwd())))
BLOOME_IMG   = Path(_cfg("BLOOME_IMG", str(Path.home() / "Downloads/bloome.png")))
INBOX_LIMIT  = int(_cfg("INBOX_LIMIT", "200"))
INTERNAL_DOMAINS = tuple(
    d.strip().lower() for d in _cfg("INTERNAL_DOMAINS").split(",") if d.strip()
)

BLOOME_TOPICS = {
    "agent","skill","mcp","ai","llm","claude","gpt","awesome",
    "agent-skill","tool","plugin","extension","mcp-server","cli",
    "openai","langchain","chatgpt","automation","cursor","copilot",
}

# ── Utilities ────────────────────────────────────────────────────────────────

def _run_text(cmd: str) -> str:
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else ""

def _run_json(cmd: str):
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    if r.returncode != 0:
        return None
    try:
        return json.loads(r.stdout)
    except Exception:
        return None

def extract_email(s: str) -> "str | None":
    if not s:
        return None
    m = re.search(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}", s)
    return m.group(0).lower() if m else None

def extract_url(s: str) -> str:
    if not s:
        return ""
    m = re.search(r"https?://[^\s)>\]]+", s)
    return m.group(0) if m else s

def detect_platform(url: str) -> str:
    u = (url or "").lower()
    if "github.com" in u:
        return "github"
    if "youtube.com" in u or "youtu.be" in u:
        return "youtube"
    if "x.com" in u or "twitter.com" in u:
        return "x"
    return "other"

def get_github_handle(url: str) -> "str | None":
    m = re.search(r"github\.com/([^/?#\s]+)", url or "")
    h = m.group(1) if m else None
    return h if h and h not in {"orgs","repos","issues","topics","explore"} else None

# ── Step 1: 飞书表 ───────────────────────────────────────────────────────────

def _parse_md_table(text: str) -> list[dict]:
    lines = [l for l in text.splitlines() if l.startswith("|")]
    if len(lines) < 2:
        return []
    header = [c.strip() for c in lines[0].split("|")[1:-1]]
    rows = []
    for line in lines[2:]:
        cells = [c.strip() for c in line.split("|")[1:-1]]
        if len(cells) < len(header):
            cells += [""] * (len(header) - len(cells))
        rows.append(dict(zip(header, cells)))
    return rows

def get_all_records() -> list:
    """返回飞书表全量记录（不过滤状态），附加 _email / _url 字段。
    用于邮件匹配——博主无论处于什么状态（达成合作/已上线…）都能对上号。"""
    all_rows: list = []
    offset = 0
    while True:
        text = _run_text(
            f"lark-cli base +record-list "
            f"--base-token {BASE_TOKEN} --table-id {TABLE_ID} "
            f"--limit 200 --offset {offset} --as user"
        )
        rows = _parse_md_table(text)
        all_rows.extend(rows)
        meta = re.search(r"has_more=(\w+)", text)
        if not meta or meta.group(1) != "true":
            break
        offset += 200
        time.sleep(0.5)
    for r in all_rows:
        r["_email"] = extract_email(r.get("联系方式", ""))
        r["_url"]   = extract_url(r.get("主页URL", ""))
    return all_rows

def get_active_records() -> list:
    """仅「已联系 / 沟通中」的记录（旧接口，run.py/app.py 仍在用）。"""
    return [r for r in get_all_records()
            if "已联系" in r.get("联系状态", "") or "沟通中" in r.get("联系状态", "")]

# ── Step 2: 收件箱 ───────────────────────────────────────────────────────────

def _parse_triage(text: str) -> list[dict]:
    messages = []
    lines = text.splitlines()
    header_idx = None
    for i, line in enumerate(lines):
        if "message_id" in line and "from" in line.lower():
            header_idx = i
            break
    if header_idx is None:
        return messages
    for line in lines[header_idx + 2:]:
        if not line.strip() or line.startswith("tip:"):
            continue
        parts = re.split(r"\s{2,}", line.strip())
        if len(parts) >= 3:
            messages.append({
                "date":       parts[0] if len(parts) > 0 else "",
                "from":       parts[1] if len(parts) > 1 else "",
                "message_id": parts[2] if len(parts) > 2 else "",
                "subject":    parts[3] if len(parts) > 3 else "",
            })
    return messages

def scan_inbox(known_emails: set, days: int = 7) -> dict:
    """返回 email → {message_id, subject, date, from_raw}（每个邮箱取最新一封）。"""
    text = _run_text(
        f"lark-cli mail +triage --as user --max {INBOX_LIMIT} --filter '{{\"folder\":\"INBOX\"}}' 2>&1"
    )
    messages = _parse_triage(text)
    reply_map: dict = {}
    for msg in messages:
        from_raw = msg.get("from", "")
        if (FROM_ADDRESS and FROM_ADDRESS in from_raw) or any(d in from_raw.lower() for d in INTERNAL_DOMAINS):
            continue
        email = extract_email(from_raw)
        if email and email in known_emails and email not in reply_map:
            reply_map[email] = {
                "message_id": msg["message_id"],
                "subject":    msg["subject"],
                "date":       msg["date"],
                "from_raw":   from_raw,
            }
    return reply_map

# ── Step 3: 更新状态 ─────────────────────────────────────────────────────────

def update_status(record_id: str) -> bool:
    """已联系 → 沟通中。"""
    payload = json.dumps({
        "record_id_list": [record_id],
        "patch": {"联系状态": "沟通中"}
    })
    r = subprocess.run(
        f"lark-cli base +record-batch-update "
        f"--base-token {BASE_TOKEN} --table-id {TABLE_ID} "
        f"--as user --json '{payload}'",
        shell=True, capture_output=True, text=True
    )
    return r.returncode == 0

# ── Step 4a: GitHub 仓库分析 ─────────────────────────────────────────────────

def analyze_github_repos(username: str) -> list:
    """拉 GitHub 仓库，评分排序，返回 top 3。优先读磁盘缓存（24h TTL）。"""
    disk = _load_repo_disk_cache()
    if username in disk:
        return disk[username]["repos"]

    raw = _run_json(
        f'gh api "users/{username}/repos?per_page=100&sort=stars" '
        f'--jq "[.[] | {{name:.name,stars:.stargazers_count,'
        f'pushed:.pushed_at,topics:.topics,desc:.description,url:.html_url}}]"'
    )
    if not raw:
        return []

    scored = []
    for repo in raw:
        score = min(repo.get("stars", 0) / 50, 20)
        pushed = repo.get("pushed", "")
        if pushed:
            try:
                dt = datetime.fromisoformat(pushed.replace("Z", "+00:00"))
                days_ago = (datetime.now(dt.tzinfo) - dt).days
                score += max(0, 10 - days_ago / 30)
            except Exception:
                pass
        topics = set(repo.get("topics") or [])
        score += len(topics & BLOOME_TOPICS) * 3

        name_l = repo["name"].lower()
        desc_l  = (repo.get("desc") or "").lower()
        if topics & {"agent-skill","skills-sh","mcp-server","mcp"}:
            hook = "skill"
        elif "awesome" in name_l or "awesome" in desc_l or "list" in name_l:
            hook = "awesome-list"
        elif topics & {"agent","multi-agent"} or "agent" in name_l:
            hook = "agent-team"
        else:
            hook = "general"

        scored.append({**repo, "score": round(score, 1), "hook": hook})

    # 过滤 < 300 star，取 top 3
    scored = [r for r in scored if r.get("stars", 0) >= 300]
    scored.sort(key=lambda x: x["score"], reverse=True)
    result = scored[:3]

    # 写入磁盘缓存
    disk[username] = {"repos": result, "_ts": time.time()}
    _save_repo_disk_cache(disk)
    return result

def hook_phrase(hook: str, repo_name: str) -> str:
    return {
        "skill":        f"run {repo_name} as a cloud agent — zero local setup, shareable with your team",
        "awesome-list": "run any skill from your list in the cloud — no setup, one click",
        "agent-team":   "run your agent team in the cloud — zero local setup, share it with teammates",
        "general":      f"use {repo_name} as a cloud agent — zero setup, runs in browser or mobile",
    }.get(hook, f"use {repo_name} via Bloome — zero setup")

# ── Step 4b: 起草邮件 ────────────────────────────────────────────────────────

def preview_github_text(repo: dict, handle: str) -> str:
    """返回 GitHub 草稿的可编辑纯文本（不创建草稿）。"""
    name   = handle or "there"
    phrase = hook_phrase(repo["hook"], repo["name"])
    stars  = repo.get("stars", 0)
    return (
        f"Hi {name} 👋\n\n"
        f"Thanks for getting back to me!\n\n"
        f"I took a look at your repos and {repo['name']} ({stars}⭐) stood out as a great fit.\n\n"
        f"The idea: add a short Bloome mention in the README so your users can {phrase}.\n\n"
        f"Something like this (happy to adjust wording / placement to your style):\n\n"
        f"☁️ Want to {phrase}?\n"
        f"Try Bloome ( https://bloome.im/login?ref=YOUR_REF ) — one click, no Node/local setup needed.\n\n"
        f"About the ref link: sign up / log in at bloome.im, grab your personal ref code from your account, "
        f"and replace YOUR_REF. Every user who comes through your link gets attributed to you.\n\n"
        f"We're happy to pay for the placement — let me know your preferred rate or format.\n\n"
        f"Best,\nIvor · Bloome\nwww.bloome.im"
    )

def preview_youtube_text(note: str = "") -> str:
    """返回 YouTube 草稿的可编辑纯文本（不创建草稿）。"""
    note_line = f"\n\n(Note: {note})" if note and len(note) < 200 else ""
    return (
        f"Hi there 👋\n\n"
        f"Thanks for getting back to me!\n\n"
        f"Great to hear you're open to a collaboration. We'd love to work with you on a Bloome sponsorship — "
        f"could you share your rate card or preferred pricing for a dedicated review or integration segment?{note_line}\n\n"
        f"Once we see the numbers we can take it from there. Looking forward to hearing from you!\n\n"
        f"Best,\nIvor · Bloome\nwww.bloome.im"
    )

def preview_direct_ad_text(name: str = "there", repo: dict = None) -> str:
    """直接广告植入模板：在指定仓库 README 加 Bloome 产品介绍区块。"""
    repo_line = ""
    if repo:
        repo_name = repo.get("name", "")
        repo_url  = repo.get("url", "")
        repo_line = (
            f"I came across your repo [{repo_name}]({repo_url}) and thought it'd be "
            f"a great fit for a Bloome placement given your audience.\n\n"
        )
    else:
        repo_line = "I'd love to explore adding a short Bloome section to your README.\n\n"

    return (
        f"Hi {name} 👋\n\n"
        f"Thanks for getting back to me!\n\n"
        f"{repo_line}"
        f"The idea: add a brief Bloome introduction block in the README — "
        f"just a product description for your readers, no need for it to tie into your repo's functionality.\n\n"
        f"Bloome is an AI-agent IM platform where you can build and run AI agent teams in the cloud — "
        f"zero setup, fully shareable, works on web and mobile. "
        f"Think of it as a group chat where your AI assistants are teammates you can @mention, "
        f"assign tasks to, and collaborate with.\n\n"
        f"About the ref link: sign up / log in at bloome.im, grab your personal ref code from your account, "
        f"and replace YOUR_REF. Every user who comes through your link gets attributed to you.\n\n"
        f"We're happy to pay for the placement — let me know your preferred rate.\n\n"
        f"Best,\nIvor · Bloome\nwww.bloome.im"
    )

def preview_github_text_zh(repo: dict, handle: str) -> str:
    """GitHub README 合作——中文版。"""
    name   = handle or "你好"
    phrase_map = {
        "skill":        f"无需本地配置、一键在云端运行 {repo['name']}",
        "awesome-list": "把清单里的工具零配置在云端跑起来",
        "agent-team":   "把你的 AI agent 团队搬上云端、无需本地配置",
        "general":      f"零配置在云端使用 {repo['name']}",
    }
    phrase = phrase_map.get(repo.get("hook","general"), phrase_map["general"])
    stars  = repo.get("stars", 0)
    return (
        f"你好 {handle} 👋\n\n"
        f"感谢你的回复！\n\n"
        f"看了一下你的项目，{repo['name']}（⭐{stars}）和我们非常契合。\n\n"
        f"想法是：在 README 里加一段 Bloome 的介绍，让你的用户可以{phrase}。\n\n"
        f"类似这样（具体措辞和位置可以按你的风格调整）：\n\n"
        f"☁️ 想要{phrase}？\n"
        f"试试 Bloome ( https://bloome.im/login?ref=YOUR_REF ) — 一键运行，无需本地配置。\n\n"
        f"关于 ref 链接：在 bloome.im 注册/登录后，在账户里获取你的专属 ref 码，替换 YOUR_REF 即可。"
        f"通过你的链接进来的用户都会归到你名下。\n\n"
        f"我们会为这次合作支付费用 —— 告诉我你的报价或合作形式即可。\n\n"
        f"期待合作！\n\nIvor · Bloome\nwww.bloome.im"
    )

def preview_direct_ad_text_zh(name: str = "你好", repo: dict = None) -> str:
    """README 直接广告植入——中文版。"""
    repo_line = (
        f"看到你的项目 {repo['name']} 受众很不错，想在 README 里加一段 Bloome 的产品介绍 —— "
        f"面向你的读者，不需要和你项目的功能强行挂钩。\n\n"
    ) if repo else "想在你的 README 里加一段 Bloome 的产品介绍。\n\n"
    return (
        f"你好 {name} 👋\n\n"
        f"感谢你的回复！\n\n"
        f"{repo_line}"
        f"Bloome 是一个 AI Agent IM 平台，可以在云端搭建和运行 AI agent 团队，零配置、可分享，"
        f"网页和手机都能用。就像把 AI 助手加到群聊里，@他们像@普通成员一样，可以分配任务和协作。\n\n"
        f"关于 ref 链接：在 bloome.im 注册/登录后获取你的专属 ref 码，替换 YOUR_REF 即可，"
        f"通过你链接进来的用户都会归到你名下。\n\n"
        f"我们会为此付费 —— 告诉我你的报价即可。\n\n"
        f"期待合作！\n\nIvor · Bloome\nwww.bloome.im"
    )

def preview_youtube_text_zh(note: str = "") -> str:
    """YouTube 询价——中文版。"""
    note_line = f"\n\n（备注：{note}）" if note and len(note) < 200 else ""
    return (
        f"你好 👋\n\n"
        f"感谢你的回复！\n\n"
        f"很高兴你有合作意向。想了解一下做 Bloome 赞助的报价 —— "
        f"独立评测或植入都可以，告诉我你的收费标准，我们再定细节。{note_line}\n\n"
        f"期待你的回复！\n\nIvor · Bloome\nwww.bloome.im"
    )

def preview_generic_text(name: str = "there") -> str:
    """通用回复模板（未匹配到表里博主的外部邮件）——英文版。"""
    return (
        f"Hi {name} 👋\n\n"
        f"Thanks for getting back to me!\n\n"
        f"Quick recap: Bloome is an AI-agent IM platform — build and run AI agent teams in the cloud, "
        f"zero setup, shareable, on web and mobile.\n\n"
        f"We'd love to work out a paid collaboration that fits your audience — "
        f"could you share what format works best for you (and your rate)?\n\n"
        f"Looking forward to hearing from you!\n\n"
        f"Best,\nIvor · Bloome\nwww.bloome.im"
    )

def preview_generic_text_zh(name: str = "你好") -> str:
    """通用回复模板——中文版。"""
    return (
        f"你好 👋\n\n"
        f"感谢你的回复！\n\n"
        f"简单介绍下：Bloome 是一个 AI Agent IM 平台——在云端搭建和运行 AI agent 团队，"
        f"零配置、可分享，网页和手机都能用。\n\n"
        f"我们希望以付费合作的形式推进——方便说下你倾向的合作形式和报价吗？\n\n"
        f"期待你的回复！\n\nIvor · Bloome\nwww.bloome.im"
    )

def _inject_before_div_close(html: str, extra: str) -> str:
    """把 extra HTML 插到最外层 </div> 之前；没有 div 就追加到末尾。"""
    if not extra:
        return html
    idx = html.rstrip().rfind("</div>")
    if idx == -1:
        return html + "\n" + extra
    return html[:idx] + extra + "\n" + html[idx:]

def _build_image_payload(with_image: bool, image_paths):
    """官方图(可选) + 上传图 → (inline_specs, extra_tags, tmp_paths)。
    官方图的 <img> 标签由正文（_plain_to_html/_build_html）已含，不放进 extra_tags（避免重复）；
    上传图的标签要在这里补进正文。所有图都复制进 VAULT_ROOT 临时名（lark-cli --inline 要 cwd 相对路径）。"""
    specs, extra_tags, tmps = [], [], []
    if with_image and BLOOME_IMG.exists():
        shutil.copy(BLOOME_IMG, VAULT_ROOT / "bloome-outreach-tmp.png")
        tmps.append(VAULT_ROOT / "bloome-outreach-tmp.png")
        specs.append({"cid": "bloomeimg", "file_path": "bloome-outreach-tmp.png"})
    for i, src in enumerate(image_paths or []):
        p = Path(src)
        if not p.exists():
            continue
        cid = "rimg%d" % i
        tmp_name = "bloome-reply-tmp-%d%s" % (i, (p.suffix or ".png"))
        shutil.copy(p, VAULT_ROOT / tmp_name)
        tmps.append(VAULT_ROOT / tmp_name)
        specs.append({"cid": cid, "file_path": tmp_name})
        extra_tags.append(
            '<p style="margin-top:14px;"><img src="cid:%s" alt="image" '
            'style="max-width:560px;width:100%%;border-radius:8px;"></p>' % cid
        )
    return specs, extra_tags, tmps

def _inline_arg(specs) -> str:
    return ("--inline '" + json.dumps(specs) + "'") if specs else ""

def send_reply_direct(message_id: str, html_body: str, with_image: bool = False,
                      image_paths=None) -> tuple:
    """直接发送回复（需要 message:send 权限）。返回 (ok, info)。
    with_image=附官方 Bloome 介绍图；image_paths=用户上传的任意图片绝对路径列表，全部内联进正文。
    info: 成功时为发送结果 dict；失败时为错误字符串（'missing_scope' 表示权限未批）。"""
    specs, extra_tags, tmps = _build_image_payload(with_image, image_paths)
    if extra_tags:
        html_body = _inject_before_div_close(html_body, "\n".join(extra_tags))
    inline = _inline_arg(specs)
    body_escaped = html_body.replace("'", "'\\''")
    cmd = (
        f"cd '{VAULT_ROOT}' && lark-cli mail +reply "
        f"--message-id '{message_id}' --as user --from '{FROM_ADDRESS}' --mailbox me "
        f"--body '{body_escaped}' {inline} --confirm-send"
    )
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=90)
    for t in tmps:
        if t.exists():
            t.unlink()
    # 错误时 lark-cli 输出可能在 stderr（+reply --confirm-send 实测如此），两边都解析
    result = None
    for raw in (r.stdout or "", r.stderr or ""):
        raw = raw.strip()
        if "{" in raw:
            try:
                result = json.loads(raw[raw.index("{"):])
                break
            except Exception:
                continue
    if result and result.get("ok"):
        return True, result.get("data", {})
    err = (result or {}).get("error") or {}
    if isinstance(err, dict) and err.get("type") == "missing_scope":
        return False, "missing_scope"
    msg = err.get("message") if isinstance(err, dict) else None
    if not msg:
        msg = (r.stderr or r.stdout or "unknown error").strip()
    return False, str(msg)[:200]

def mark_email_read(message_id: str) -> bool:
    """把飞书邮箱里的邮件标为已读（去掉 UNREAD 标签）。"""
    r = subprocess.run(
        f"lark-cli mail user_mailbox.messages modify --as user "
        f"--params '{{\"user_mailbox_id\":\"me\",\"message_id\":\"{message_id}\"}}' "
        f"--data '{{\"remove_label_ids\":[\"UNREAD\"]}}'",
        shell=True, capture_output=True, text=True, timeout=30
    )
    return r.returncode == 0

def _build_html(body_html: str, with_image: bool) -> str:
    """给正文 HTML 加外层 div，按 with_image 决定是否加 img 标签。"""
    img = (
        '<p style="margin-top:20px;"><img src="cid:bloomeimg" alt="Bloome" '
        'style="max-width:560px;width:100%;border-radius:8px;"></p>\n'
    ) if with_image else ""
    return (
        f'<div style="font-size:14px;line-height:1.7;color:#1f2329;">\n'
        f'{body_html}\n{img}</div>'
    )

def _create_reply_draft_opt(message_id: str, html_body: str, with_image: bool = True,
                            image_paths=None) -> tuple:
    """统一草稿创建入口：支持官方图 + 上传图内联（与 send_reply_direct 同一套图片逻辑）。"""
    specs, extra_tags, tmps = _build_image_payload(with_image, image_paths)
    if extra_tags:
        html_body = _inject_before_div_close(html_body, "\n".join(extra_tags))
    inline = _inline_arg(specs)
    with tempfile.NamedTemporaryFile(mode="w", suffix=".html", delete=False, encoding="utf-8") as f:
        f.write(html_body)
        tmp_html = f.name
    body_escaped = open(tmp_html, encoding="utf-8").read().replace("'", "'\\''")
    cmd = (
        f"cd '{VAULT_ROOT}' && lark-cli mail +reply "
        f"--message-id '{message_id}' --as user --from '{FROM_ADDRESS}' --mailbox me "
        f"--body '{body_escaped}' {inline}"
    )
    result = _run_json(cmd)
    os.unlink(tmp_html)
    for t in tmps:
        if t.exists():
            t.unlink()
    if result and result.get("ok"):
        data = result.get("data", {})
        return data.get("draft_id"), data.get("reference")
    return None, None

def draft_github_reply_opt(reply: dict, record: dict, repo: dict, with_image: bool = True) -> tuple:
    """GitHub 草稿，支持不带图模式（直接构建 HTML，不调 draft_github_reply）。"""
    handle = get_github_handle(record.get("_url", ""))
    name   = handle or "there"
    phrase = hook_phrase(repo["hook"], repo["name"])
    stars  = repo.get("stars", 0)
    inner = (
        f"<p>Hi {name} 👋</p>\n"
        f"<p>Thanks for getting back to me!</p>\n"
        f"<p>I took a look at your repos and <b>{repo['name']}</b> ({stars}⭐) stood out as a great fit.</p>\n"
        f"<p>The idea: add a short Bloome mention in the README so your users can <b>{phrase}</b>.</p>\n"
        f"<p>Something like this (happy to adjust wording / placement to your style):</p>\n"
        f"<pre style='background:#f6f8fa;padding:12px;border-radius:6px;font-size:13px;white-space:pre-wrap;'>"
        f"&#x2601;&#xfe0f; Want to {phrase}?\n"
        f"Try <a href='https://bloome.im/login?ref=YOUR_REF'>Bloome</a> — one click, no Node/local setup needed.</pre>\n"
        f"<p>About the <code>ref</code> link: sign up / log in at <a href='https://bloome.im/'>bloome.im</a>, "
        f"grab your personal ref code from your account, and replace <code>YOUR_REF</code>. "
        f"Every user who comes through your link gets attributed to you.</p>\n"
        f"<p>We're happy to <b>pay for the placement</b> — let me know your preferred rate or format.</p>\n"
        f"<p>Best,<br>Ivor · Bloome<br><a href='https://bloome.im'>www.bloome.im</a></p>"
    )
    html = _build_html(inner, with_image)
    return _create_reply_draft_opt(reply["message_id"], html, with_image)

def draft_youtube_reply_opt(reply: dict, record: dict, with_image: bool = True) -> tuple:
    """YouTube 草稿，支持不带图模式。"""
    note = record.get("备注（报价、合作形式等）", "").strip()
    note_html = f"<p><i>{note}</i></p>\n" if note and len(note) < 300 else ""
    inner = (
        f"<p>Hi there 👋</p>\n"
        f"<p>Thanks for getting back to me!</p>\n"
        f"<p>Great to hear you're open to a collaboration. We'd love to work with you on a Bloome sponsorship — "
        f"could you share your rate card or preferred pricing for a dedicated review or integration segment?</p>\n"
        f"{note_html}"
        f"<p>Once we see the numbers we can take it from there. Looking forward to hearing from you!</p>\n"
        f"<p>Best,<br>Ivor · Bloome<br><a href='https://bloome.im'>www.bloome.im</a></p>"
    )
    html = _build_html(inner, with_image)
    return _create_reply_draft_opt(reply["message_id"], html, with_image)

def _plain_to_html(text: str, with_image: bool = True) -> str:
    """把编辑后的纯文本转成带样式的 HTML，保留段落和链接。"""
    import re as _re
    paragraphs = text.strip().split("\n\n")
    html_parts = []
    for p in paragraphs:
        p = p.strip()
        if not p:
            continue
        # 把行内 URL 变成链接
        p = _re.sub(
            r'(https?://[^\s\)]+)',
            r'<a href="\1">\1</a>',
            p
        )
        p = p.replace("\n", "<br>")
        html_parts.append(f"<p>{p}</p>")
    body = "\n".join(html_parts)
    return _build_html(body, with_image)

def _create_reply_draft(message_id: str, html_body: str) -> tuple:
    """写 HTML body 到临时文件，调 +reply 建草稿，返回 (draft_id, reference_url)。"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".html", delete=False, encoding="utf-8") as f:
        f.write(html_body)
        tmp_html = f.name

    tmp_img_name = "bloome-outreach-tmp.png"
    tmp_img_vault = VAULT_ROOT / tmp_img_name
    has_img = BLOOME_IMG.exists()
    if has_img:
        shutil.copy(BLOOME_IMG, tmp_img_vault)
        inline = f'--inline \'[{{"cid":"bloomeimg","file_path":"{tmp_img_name}"}}]\''
    else:
        inline = ""

    body_escaped = open(tmp_html, encoding="utf-8").read().replace("'", "'\\''")
    cmd = (
        f"cd '{VAULT_ROOT}' && "
        f"lark-cli mail +reply "
        f"--message-id '{message_id}' "
        f"--as user --from '{FROM_ADDRESS}' --mailbox me "
        f"--body '{body_escaped}' "
        f"{inline}"
    )
    result = _run_json(cmd)

    os.unlink(tmp_html)
    if has_img and tmp_img_vault.exists():
        tmp_img_vault.unlink()

    if result and result.get("ok"):
        data = result.get("data", {})
        return data.get("draft_id"), data.get("reference")
    return None, None

def draft_github_reply(reply: dict, record: dict, repo: dict) -> tuple:
    handle = get_github_handle(record.get("_url", ""))
    name   = handle or "there"
    phrase = hook_phrase(repo["hook"], repo["name"])
    stars  = repo.get("stars", 0)

    html = f"""<div style="font-size:14px;line-height:1.7;color:#1f2329;">
<p>Hi {name} 👋</p>
<p>Thanks for getting back to me!</p>
<p>I took a look at your repos and <b>{repo['name']}</b> ({stars}⭐) stood out as a great fit.</p>
<p>The idea: add a short Bloome mention in the README so your users can <b>{phrase}</b>.</p>
<p>Something like this (happy to adjust wording / placement to your style):</p>
<pre style="background:#f6f8fa;padding:12px;border-radius:6px;font-size:13px;white-space:pre-wrap;">&#x2601;&#xfe0f; Want to {phrase}?
Try <a href="https://bloome.im/login?ref=YOUR_REF">Bloome</a> — one click, no Node/local setup needed.</pre>
<p>About the <code>ref</code> link: sign up / log in at <a href="https://bloome.im/">bloome.im</a>, grab your personal ref code from your account, and replace <code>YOUR_REF</code>. Every user who comes through your link gets attributed to you.</p>
<p>We're happy to <b>pay for the placement</b> — let me know your preferred rate or format.</p>
<p>Best,<br>Ivor · Bloome<br><a href="https://bloome.im">www.bloome.im</a></p>
<p style="margin-top:20px;"><img src="cid:bloomeimg" alt="Bloome" style="max-width:560px;width:100%;border-radius:8px;"></p>
</div>"""
    return _create_reply_draft(reply["message_id"], html)

def draft_youtube_reply(reply: dict, record: dict) -> tuple:
    note = record.get("备注（报价、合作形式等）", "").strip()
    note_html = f"<p><i>（内部备注：{note}）</i></p>" if note and len(note) < 300 else ""

    html = f"""<div style="font-size:14px;line-height:1.7;color:#1f2329;">
<p>Hi there 👋</p>
<p>Thanks for getting back to me!</p>
<p>Great to hear you're open to a collaboration. We'd love to work with you on a Bloome sponsorship — could you share your rate card or preferred pricing for a dedicated review or integration segment?</p>
{note_html}
<p>Once we see the numbers we can take it from there. Looking forward to hearing from you!</p>
<p>Best,<br>Ivor · Bloome<br><a href="https://bloome.im">www.bloome.im</a></p>
<p style="margin-top:20px;"><img src="cid:bloomeimg" alt="Bloome" style="max-width:560px;width:100%;border-radius:8px;"></p>
</div>"""
    return _create_reply_draft(reply["message_id"], html)
