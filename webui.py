#!/usr/bin/env python3
"""
Bloome 邮件回复助手 Web UI  v5（分栏式）
- 左列表 + 右阅读/回复面板（邮件客户端模式，零弹窗）
- 状态绑定每封邮件（message_id）：对方再次回信自动回到「待处理」
- 双向同步：看板查看→飞书已读；飞书手动回复→看板自动标「已回复」（扫描已发送文件夹）
- 📋 一键复制回复文本（手动去飞书粘贴发送的快捷流）
- 模板全部预生成；单封「批准并发送」= 后台直发（权限未批自动降级草稿）；🌐 中文翻译
- 批量（模板/AI）只"生成"进待发送队列 → 「📬 待发送审核」过目后才真正发出（安全闸门）
启动：python3 webui.py   →  http://localhost:5050
"""

import sys, os, json, re, subprocess, time, threading, hashlib
from pathlib import Path
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(__file__))
from core import (
    get_all_records, detect_platform, get_github_handle,
    extract_email, analyze_github_repos,
    update_status, _run_text, _run_json,
    preview_github_text, preview_github_text_zh,
    preview_youtube_text, preview_youtube_text_zh,
    preview_direct_ad_text, preview_direct_ad_text_zh,
    preview_generic_text, preview_generic_text_zh,
    _plain_to_html, _create_reply_draft_opt,
    send_reply_direct, send_draft, mark_email_read,
    BASE_TOKEN, TABLE_ID, FROM_ADDRESS,
)
from mail_store import APP_DATA_DIR, MailStore

try:
    from flask import Flask, jsonify, request, send_file
except ImportError:
    os.system(f"{sys.executable} -m pip install flask -q")
    from flask import Flask, jsonify, request, send_file

app = Flask(__name__)
APP_DATA_DIR.mkdir(parents=True, exist_ok=True)
_mail_store = MailStore()

INTERNAL_DOMAINS = tuple(
    d.strip().lower()
    for d in (os.getenv("REPLYFLOW_INTERNAL_DOMAINS")
              or os.getenv("REPLYDESK_INTERNAL_DOMAINS", "")).split(",")
    if d.strip()
)
EXCLUDE_FROM     = ("mailer-daemon",)   # 仅退信不进列表（退信单独处理→置无法合作）
NOTICE_FROM      = ("noreply", "no-reply", "donotreply", "do-not-reply", "do_not_reply",
                    "no_reply")   # noreply 类 → 归「🔔 通知」分组，不进待处理/全部
def _is_notice_from(fr: str) -> bool:
    return any(x in (fr or "").lower() for x in NOTICE_FROM)

# ── 持久化状态（按 message_id；含旧格式自动迁移） ───────────────────────────────
STATE_FILE = APP_DATA_DIR / "outreach-state.json"

def load_state() -> dict:
    try:
        raw = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}
    except Exception:
        raw = {}
    if "messages" not in raw:
        raw = {"messages": {}, "history": {}, "legacy": raw if isinstance(raw, dict) else {}}
    raw.setdefault("messages", {})
    raw.setdefault("history", {})
    raw.setdefault("legacy", {})
    raw.setdefault("farewell", {})   # email → 告别信发出时间（不合适博主的礼貌收尾）
    raw.setdefault("bounces", {})    # 已处理过的退信 message_id → 时间戳（防重复置「无法合作」）
    return raw

def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2))

def mark_farewell(email: str):
    s = load_state()
    s["farewell"][email] = datetime.now().strftime("%Y-%m-%d %H:%M")
    save_state(s)
    for it in (_cache.get("inbox") or []):
        if it["email"] == email:
            it["farewell_done"] = True

def mark_state(message_id: str, email: str, action: str, draft_url: str = ""):
    """状态按 message_id 存；沟通历史按 email 累积。"""
    s = load_state()
    m = s["messages"].get(message_id, {})
    s["messages"][message_id] = {
        "action":    action,
        "draft_url": draft_url or m.get("draft_url", ""),
        "ts":        datetime.now().strftime("%Y-%m-%d %H:%M"),
        "email":     email,
    }
    if action:
        s["history"].setdefault(email, []).append(
            {"action": action, "ts": datetime.now().strftime("%Y-%m-%d %H:%M")})
        s["history"][email] = s["history"][email][-12:]
    save_state(s)
    if _cache["inbox"]:
        for it in _cache["inbox"]:
            if it["message_id"] == message_id:
                it["action"]    = action
                it["draft_url"] = s["messages"][message_id]["draft_url"]
                it["action_ts"] = s["messages"][message_id]["ts"]
                it["history"]   = s["history"].get(email, [])
                break

# ── 缓存 ──────────────────────────────────────────────────────────────────────
_cache           = {"records": None, "inbox": None, "ts": 0}
_repo_cache      = {}
_draft_previews  = {}
_preload_status  = {"done": 0, "total": 0, "finished": False, "stage": ""}
_jobs            = {}
_send_state      = {"mode": "unknown"}   # unknown | direct | draft
_thread_cache    = {}   # thread_id → 完整消息列表（点开邮件零等待的关键）
_batch           = {"running": False, "total": 0, "done": 0, "ok": 0, "fail": 0, "results": [], "kind": ""}

# ── 待发送审核队列（批量操作的安全闸门）────────────────────────────────────────
# 批量（模板/AI）一律只"生成"进这里，用户在 WebUI 审核（可改文案）后经
# /api/pending-send 才真正发出。持久化到文件，重启不丢已生成内容。
PENDING_FILE = APP_DATA_DIR / "pending-reviews.json"

def _load_pending():
    try:
        return json.loads(PENDING_FILE.read_text())
    except Exception:
        return {}

def _save_pending():
    try:
        PENDING_FILE.write_text(json.dumps(_pending, ensure_ascii=False, indent=1))
    except Exception:
        pass

_pending = _load_pending()   # message_id → {email, text, kind, with_image, ts}

def _send_or_draft(message_id: str, html: str, with_image: bool, image_paths=None):
    """两步发送：先在飞书建回复草稿（飞书侧可见、留痕），再发布该草稿（原生 draft-send）。
    没有发送权限就停在草稿（用户去飞书草稿箱手动发）。"""
    draft_id, draft_url = _create_reply_draft_opt(message_id, html, with_image, image_paths)
    if not draft_id:
        return "error", "飞书草稿创建失败"
    if _send_state["mode"] == "draft":
        return "drafted", draft_url            # 已知无发送权限，停在草稿
    ok, info = send_draft(draft_id)
    if ok:
        _send_state["mode"] = "direct"
        return "sent", {}
    if info == "missing_scope":
        _send_state["mode"] = "draft"
        return "drafted", draft_url            # 降级：草稿已建好，去飞书草稿箱发
    return "error", f"{info}（草稿已建好，可去飞书草稿箱发送）"

# ── 收件箱（JSON 格式，含 thread_id / labels） ─────────────────────────────────
def _triage_json(folder: str, max_n: int = 100) -> list:
    """+triage --format json → list[{date,from,labels,message_id,subject,thread_id}]"""
    r = subprocess.run(
        f"lark-cli mail +triage --as user --max {max_n} --format json "
        f"--filter '{{\"folder\":\"{folder}\"}}'",
        shell=True, capture_output=True, text=True
    )
    try:
        data = json.loads(r.stdout)
        messages = data.get("messages", []) or []
        for msg in messages:
            msg.setdefault("from_raw", msg.get("from", ""))
            msg.setdefault("email", extract_email(msg.get("from", "")) or "")
        _mail_store.upsert_many(messages, folder=folder)
        return messages
    except Exception:
        return []

def _scan_sent_threads() -> dict:
    """扫描已发送 → {thread_id: 最新发送时间}。同一会话里我方有更晚消息 = 那封来信已被回复。"""
    threads = {}
    for msg in _triage_json("SENT", 100):
        tid  = msg.get("thread_id", "")
        date = (msg.get("date") or "")[:16]
        if tid and (tid not in threads or date > threads[tid]):
            threads[tid] = date
    return threads

def _clean_multi(v) -> str:
    """飞书选项/人员字段的原始值（'[\"已联系\"]'、'[{\"name\":..}]'）→ 可读文本。"""
    s = str(v or "").strip()
    if s.startswith("["):
        try:
            arr = json.loads(s)
            if isinstance(arr, list):
                out = []
                for x in arr:
                    out.append((x.get("name") or x.get("text") or "") if isinstance(x, dict) else str(x))
                s = " / ".join(o for o in out if o)
        except Exception:
            pass
    return s.replace("\\u0026", "&").strip()

FEISHU_BASE_URL = f"https://feishu.cn/base/{BASE_TOKEN}?table={TABLE_ID}"

def _feishu_profile(rec) -> dict:
    """档案卡数据：与该博主相关的飞书表字段（已清洗）。"""
    return {
        "status":  _clean_multi(rec.get("联系状态")),
        "tags":    _clean_multi(rec.get("标签")),
        "coop":    _clean_multi(rec.get("合作形式")),
        "lang":    _clean_multi(rec.get("语种")),
        "country": _clean_multi(rec.get("国家")),
        "channel": str(rec.get("频道ID") or "").strip(),
        "owner":   _clean_multi(rec.get("负责人")),
        "src":     _clean_multi(rec.get("资源来源")),
        "updated": str(rec.get("状态更新时间") or "")[:16],
        "base_url": FEISHU_BASE_URL,
    }

_reload_lock = {"running": False}

def load_data(force=False):
    """stale-while-revalidate：永远秒回当前缓存；过期/强刷时后台重建，建完自动换新。

    以前过期后请求会同步等全量重建（~40-60s），用户每次刷新都干等——这就是修掉的"加载久"。
    """
    now = time.time()
    have  = _cache["inbox"] is not None
    fresh = have and now - _cache["ts"] < 180
    if have:
        if (force or not fresh) and not _reload_lock["running"]:
            _reload_lock["running"] = True
            def _bg():
                try:
                    _build_inbox()
                except Exception as e:
                    print(f"[reload] 后台重建失败: {e}")
                finally:
                    _reload_lock["running"] = False
            threading.Thread(target=_bg, daemon=True).start()
        return _cache["inbox"]
    # 冷启动（仅首次）：预加载线程在建就先回空列表，否则阻塞建
    if _reload_lock["running"]:
        return []
    _reload_lock["running"] = True
    try:
        return _build_inbox()
    finally:
        _reload_lock["running"] = False

def _build_inbox():
    _old_snippets = {i["message_id"]: i["snippet"]
                     for i in (_cache.get("inbox") or []) if i.get("snippet")}
    # 清理超 2 小时的乐观已发送记录（此时真实 SENT 已同步进来，乐观条目作废，防积累）
    _cut = int(time.time() * 1000) - 2 * 3600 * 1000
    for _e in list(_sent_optimistic):
        _sent_optimistic[_e] = [s for s in _sent_optimistic[_e] if s["ts"] > _cut]
        if not _sent_optimistic[_e]:
            del _sent_optimistic[_e]
    # 三个重 IO 并行拉（串行 ~60s → 并行 ≈ 最慢一个）
    res = {}
    def _t1(): res["records"]  = get_all_records()
    def _t2(): res["messages"] = _triage_json("INBOX", 100)
    def _t3(): res["sent"]     = _scan_sent_threads()
    ths = [threading.Thread(target=f, daemon=True) for f in (_t1, _t2, _t3)]
    [t.start() for t in ths]
    [t.join()  for t in ths]
    records, messages, sent_threads = res.get("records") or [], res.get("messages") or [], res.get("sent") or {}
    email_map = {r["_email"]: r for r in records if r.get("_email")}
    cutoff   = datetime.now(timezone.utc) - timedelta(days=7)
    state    = load_state()
    state_dirty = False
    # 退信处理：把发不到的博主在 Base 里置「无法合作」（在按人归并前，因退信发件人会被 EXCLUDE_FROM 滤掉）
    try:
        _process_bounces(messages, email_map, state)
    except Exception as e:
        print(f"[bounce] 处理失败: {e}")
    # 按"人"归并：QQ 等邮箱的回信经常不进同一 thread（每封自成线程），
    # 所以先把同一发件人的全部消息聚起来：最新一封做列表入口，全部 thread_id 留作完整对话源。
    by_email, email_threads = {}, {}
    for msg in messages:
        fr = msg.get("from", "").strip()
        if any(x in fr.lower() for x in EXCLUDE_FROM):
            continue
        email = extract_email(fr)
        if not email or any(email.endswith(d) for d in INTERNAL_DOMAINS):
            continue
        if msg.get("thread_id"):
            email_threads.setdefault(email, set()).add(msg["thread_id"])
        cur = by_email.get(email)
        if cur is None or (msg.get("date") or "") > (cur.get("date") or ""):
            by_email[email] = msg
    _cache["email_threads"] = {e: sorted(s) for e, s in email_threads.items()}

    inbox = []
    for email, msg in by_email.items():
        fr = msg.get("from", "").strip()
        try:
            dt = datetime.strptime(msg["date"][:16], "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
            if dt < cutoff:
                continue
        except Exception:
            pass
        msg_id = msg["message_id"]
        # 状态查找（message_id）+ 旧格式迁移
        ms = state["messages"].get(msg_id)
        if ms is None and email in state["legacy"]:
            leg = state["legacy"].pop(email)
            ms = {"action": leg.get("action", ""), "draft_url": leg.get("draft_url", ""),
                  "ts": leg.get("ts", ""), "email": email}
            state["messages"][msg_id] = ms
            for h in leg.get("history", []):
                state["history"].setdefault(email, []).append(h)
            state_dirty = True
        ms = ms or {}
        action = ms.get("action", "")
        # ── 双向同步：同一会话线程里我方有更晚的发件 → 这封视为已回复 ──
        if action in ("", "drafted"):
            sent_at = sent_threads.get(msg.get("thread_id", ""), "")
            if sent_at and sent_at > msg["date"][:16]:
                action = "replied"
                state["messages"][msg_id] = {
                    "action": "replied", "draft_url": ms.get("draft_url", ""),
                    "ts": sent_at, "email": email}
                state["history"].setdefault(email, []).append(
                    {"action": "replied", "ts": sent_at})
                state["history"][email] = state["history"][email][-12:]
                ms = state["messages"][msg_id]
                state_dirty = True
        rec      = email_map.get(email)
        matched  = rec is not None
        platform = detect_platform(rec.get("_url", "")) if matched else "other"
        inbox.append({
            "email":      email,
            "from_raw":   fr,
            "message_id": msg_id,
            "thread_id":  msg.get("thread_id", ""),
            "subject":    msg["subject"],
            "date":       msg["date"][:16],
            "unread":     "UNREAD" in msg.get("labels", ""),
            "is_notice":  _is_notice_from(fr),
            "matched":    matched,
            "platform":   platform,
            "url":        rec.get("_url", "") if matched else "",
            "feishu_status": rec.get("联系状态", "") if matched else "",
            "record_id":  rec.get("_record_id", "") if matched else "",
            "note":       rec.get("备注（报价、合作形式等）", "") if matched else "",
            "feishu":     _feishu_profile(rec) if matched else None,
            "action":     action,
            "draft_url":  ms.get("draft_url", ""),
            "action_ts":  ms.get("ts", ""),
            "history":    state["history"].get(email, []),
            "farewell_done": email in state["farewell"],
            "previews":   _draft_previews.get(email, {}),
        })
    if state_dirty:
        save_state(state)
    for it in inbox:   # 摘要带过刷新（线程缓存重建前先用旧的，避免闪回主题行）
        if not it.get("snippet") and it["message_id"] in _old_snippets:
            it["snippet"] = _old_snippets[it["message_id"]]
    _thread_cache.clear()        # 换新一刻才清线程缓存（重建期间不打断浏览）
    _cache["records"] = records
    _cache["inbox"]   = inbox
    _cache["ts"]      = time.time()
    _warm_threads_async(inbox)   # 后台回热线程缓存 + 补摘要/意图（~1 分钟内恢复）
    return inbox

_warm_state = {"running": False}

def _warm_threads_async(inbox):
    """load_data 清掉线程缓存后，后台重新拉全部会话并补列表摘要。"""
    if _warm_state["running"]:
        return
    _warm_state["running"] = True

    def _run():
        try:
            for it in inbox:
                tids = set(_email_thread_ids(it["email"]))
                if it.get("thread_id"):
                    tids.add(it["thread_id"])
                for tid in tids:
                    _get_thread(tid)
                try:
                    pm = [p for p in _dialog_messages_all(it["email"], cached_only=True)
                          if p["role"] == "对方"]
                    if pm and not it.get("snippet"):
                        it["snippet"] = " ".join(pm[-1]["text"].split())[:110]
                    if pm and not it.get("action") and not it.get("intent"):
                        it["intent"] = _classify_intent(pm[-1]["text"])   # 待处理邮件做意图分诊
                except Exception:
                    pass
        finally:
            _warm_state["running"] = False

    threading.Thread(target=_run, daemon=True).start()

def _feishu_patch(record_id: str, patch: dict) -> bool:
    """写回飞书表（失败自动重试一次——连续快速写入偶发瞬时失败）。"""
    payload = json.dumps({"record_id_list": [record_id], "patch": patch})
    for attempt in (1, 2):
        r = subprocess.run(
            f"lark-cli base +record-batch-update --base-token {BASE_TOKEN} "
            f"--table-id {TABLE_ID} --as user --json '{payload}'",
            shell=True, capture_output=True, text=True
        )
        if r.returncode == 0:
            return True
        time.sleep(1.2)
    return False

def update_feishu_status(record_id: str, new_status: str) -> bool:
    return _feishu_patch(record_id, {"联系状态": new_status})

def update_feishu_note(record_id: str, note: str) -> bool:
    return _feishu_patch(record_id, {"备注（报价、合作形式等）": note})

# ── 退信处理：识别退信 → 解析失败收件人 → 对应记录置「无法合作」 ───────────────────
_BOUNCE_FROM = ("mailer-daemon", "postmaster", "mail-daemon", "mail delivery",
                "maildelivery", "mailerdaemon")
_BOUNCE_SUBJ = re.compile(
    r"(undeliver|delivery status notification|mail delivery (?:failed|subsystem)|"
    r"returned mail|delivery (?:has )?failed|failure notice|delivery incomplete|"
    r"address not found|退信|配信不可|無法投遞|无法投递|送信できません)", re.I)
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")

def _is_bounce(msg: dict) -> bool:
    fr = (msg.get("from") or "").lower()
    if any(x in fr for x in _BOUNCE_FROM):
        return True
    return bool(_BOUNCE_SUBJ.search(msg.get("subject") or ""))

def _process_bounces(messages: list, email_map: dict, state: dict) -> dict:
    """扫退信邮件：从退信正文里找回失败的收件人邮箱，匹配 Base 记录 → 联系状态置「无法合作」。
    返回本轮命中的 {email: record_id}。已处理的退信记进 state['bounces'] 防重复。"""
    done = state.setdefault("bounces", {})
    lower_map = {e.lower(): (e, rec) for e, rec in email_map.items() if e}
    hits, touched = {}, False
    for msg in messages:
        mid = msg.get("message_id")
        if not mid or mid in done or not _is_bounce(msg):
            continue
        # 只解析退信这一条消息自己的正文：群发同主题共享 thread_id（一个线程混几十个博主），
        # 若整线程抓邮箱会把没退信的人也误置「无法合作」。按 message_id 精确取退信那一条。
        own_body = ""
        try:
            for m in (_get_thread(msg.get("thread_id")) or []):
                if m.get("message_id") == mid:
                    own_body = m.get("body_plain_text") or ""
                    break
        except Exception:
            pass
        found = {e.lower() for e in _EMAIL_RE.findall(own_body)}
        found.discard(FROM_ADDRESS.lower())   # 排除自己（发件人）
        for fe in found:
            if fe in lower_map:
                orig_email, rec = lower_map[fe]
                rid = rec.get("_record_id")
                cur = rec.get("联系状态") or ""
                if rid and "无法合作" not in cur and "不合适" not in cur:
                    if update_feishu_status(rid, "无法合作"):
                        hits[orig_email] = rid
                        rec["联系状态"] = '["无法合作"]'   # 同步本地缓存，rebuild 也会再读
        done[mid] = int(time.time())   # 不论是否匹配都标记，避免每次重建反复抓正文
        touched = True
    if touched:
        save_state(state)
    if hits:
        print(f"[bounce] 退信置「无法合作」{len(hits)} 个：{list(hits)}")
    return hits

# ── 模板预生成 ────────────────────────────────────────────────────────────────
def _gen_previews_for(item: dict):
    """批量模板（通用/询价 × 中英）。单封回复已改为 AI 按上下文生成。"""
    email = item["email"]
    name_part = item["from_raw"].split("<")[0].strip()
    name = (name_part.split()[0] if name_part else "") or "there"
    note = item.get("note", "")
    _draft_previews[email] = {
        "en": {"generic": preview_generic_text(name), "pricing": preview_youtube_text(note)},
        "zh": {"generic": preview_generic_text_zh(name), "pricing": preview_youtube_text_zh(note)},
    }
    item["previews"] = _draft_previews[email]

def _preload_everything():
    time.sleep(1)
    try:
        _preload_status["stage"] = "扫描邮件"
        inbox = load_data(force=True)
        _preload_status["total"] = len(inbox)
        _preload_status["stage"] = "生成模板"
        for item in inbox:
            try:
                _gen_previews_for(item)
            except Exception as e:
                print(f"[preload] {item['email']} 失败: {e}")
            _preload_status["done"] += 1
        # 预拉全部会话线程（点开邮件零等待）——含同一发件人散落的全部线程
        _preload_status["stage"] = "缓存会话"
        tids = set()
        for i in inbox:
            if i.get("thread_id"):
                tids.add(i["thread_id"])
            tids.update(_email_thread_ids(i["email"]))
        tids = sorted(tids)
        _preload_status["total"] = len(tids)
        _preload_status["done"]  = 0
        for tid in tids:
            _get_thread(tid)
            _preload_status["done"] += 1
        _preload_status["finished"] = True
        print(f"[preload] 完成！{len(_draft_previews)} 封模板 + {len(tids)} 个会话已缓存")
        _preload_status["stage"] = "同步已读"
        for item in inbox:
            if item.get("action") and item.get("unread"):
                if mark_email_read(item["message_id"]):
                    item["unread"] = False
                time.sleep(0.3)
        # 双语预热：把每个人完整对话的译文提前翻好（内容哈希缓存，重启后基本零增量）
        _preload_status["stage"] = "预热译文"
        for item in inbox:
            try:
                msgs = _dialog_messages_all(item["email"], item.get("thread_id", ""))
                if msgs:
                    _translate_chain_blocks([p["text"] for p in msgs])
            except Exception:
                pass
        print("[preload] 双语译文预热完成")
        # 意图分诊：待处理邮件逐封分类（缓存按内容哈希，重启基本零增量）
        _preload_status["stage"] = "意图分析"
        for item in inbox:
            if item.get("action") or item.get("intent"):
                continue
            try:
                pm = [p for p in _dialog_messages_all(item["email"], item.get("thread_id", ""))
                      if p["role"] == "对方"]
                if pm:
                    item["intent"] = _classify_intent(pm[-1]["text"])
            except Exception:
                pass
        print("[preload] 意图分析完成")
    except Exception as e:
        print(f"[preload] 失败: {e}")
        _preload_status["finished"] = True

# ── 邮件正文 / 翻译 ───────────────────────────────────────────────────────────
def _strip_quotes(body: str) -> str:
    """截掉邮件引用链，只留这条消息本身的话。"""
    cut_points = []
    for marker in ["---- Replied Message ----", "\n在 ", "发件人：", "发件人:",
                   "------- 原始邮件 -------"]:
        idx = body.find(marker)
        if idx > 0:
            cut_points.append(idx)
    for pat in [r"\bOn .{5,80}?wrote:",
                r"(?im)^\s*From:\s*.{0,80}<[^>\s]+@[^>\s]+>",   # 引用头 From: 名字 <邮箱>（名字带不带引号都吃）
                r"(?im)^\s*(发件人|寄件者)\s*[:：]\s*.{0,80}<[^>\s]+@[^>\s]+>",
                r"-{2,}\s*原始邮件\s*-{2,}",          # QQ 邮箱：---原始邮件---
                r"\bOn \w{3}, .{5,60}?<[^>]+@[^>]+>"]:
        m = re.search(pat, body)
        if m and m.start() > 0:
            cut_points.append(m.start())
    if cut_points:
        head = body[:min(cut_points)].strip()
        if head:
            return head
    return body.strip()

import html as _html_mod

def _html_to_text(h: str) -> str:
    """HTML 正文 → 保留段落/列表结构的纯文本。

    飞书的 body_plain_text 会把所有换行压成空格（整封一坨），
    所以一律从 body_html 自己转，段落格式才与飞书客户端一致。
    """
    if not h:
        return ""
    h = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", "", h)

    def _fmt_link(m):
        url   = _html_mod.unescape(m.group(1)).strip()
        inner = _html_mod.unescape(re.sub(r"(?s)<[^>]+>", "", m.group(2))).strip()
        if url.startswith("mailto:"):
            return inner or url[7:]
        if not inner or inner == url or inner.rstrip("/") == url.rstrip("/"):
            return url
        # 文字链接 → markdown 形式，前端渲染成可点的"文字标签"（长链接/名片也不丢地址）
        return f"[{inner}]({url})"

    h = re.sub(r'(?is)<a\b[^>]*?href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', _fmt_link, h)
    h = re.sub(r"(?i)<br\s*/?>", "\n", h)
    h = re.sub(r"(?i)</p\s*>", "\n\n", h)
    h = re.sub(r"(?i)<li[^>]*>", "\n", h)
    h = re.sub(r"(?i)</(div|li|ul|ol|h[1-6]|blockquote|tr|table)\s*>", "\n", h)
    h = re.sub(r"(?is)<[^>]+>", "", h)
    h = _html_mod.unescape(h)
    h = re.sub(r"[ \t]+\n", "\n", h)
    h = re.sub(r"\n{3,}", "\n\n", h)
    return h.strip()

def _get_thread(thread_id: str) -> list:
    """拉会话全文（带缓存）；正文统一用 HTML 转的带格式纯文本。"""
    if not thread_id:
        return []
    if thread_id in _thread_cache:
        return _thread_cache[thread_id]
    result = _run_json(
        f"lark-cli mail +thread --thread-id '{thread_id}' --as user"
    )
    msgs = []
    if result and (result.get("ok") or result.get("data")):
        msgs = (result.get("data") or result).get("messages", []) or []
    if not msgs:
        cached = _mail_store.get_thread(thread_id)
        if cached:
            _thread_cache[thread_id] = cached
            return cached
    for m in msgs:
        txt = _html_to_text(m.get("body_html") or "")
        if txt:
            m["body_plain_text"] = txt
        head_from = m.get("head_from") or {}
        m.setdefault("email", (head_from.get("mail_address") or "").lower())
        m.setdefault("from_raw", head_from.get("name") or head_from.get("mail_address") or "")
        _mail_store.upsert_message(m, folder="THREAD")
        m.pop("body_html", None)   # 转完即弃，内存缓存只留纯文本；SQLite 已保留原始字段
    _thread_cache[thread_id] = msgs
    return msgs

def _peer_messages(thread_id: str, peer_email: str) -> list:
    """会话线程里对方的历次发言（已去引用，时间正序）→ [{date,text}]。"""
    msgs = _get_thread(thread_id)
    peer = [m for m in msgs
            if (m.get("head_from", {}) or {}).get("mail_address", "").lower() == peer_email.lower()]
    peer.sort(key=lambda m: m.get("internal_date") or m.get("date_formatted", ""))
    out = []
    for m in peer:
        said = _strip_quotes(m.get("body_plain_text") or "")
        if said:
            out.append({"date": (m.get("date_formatted") or "")[:16],
                        "text": said[:1200],
                        "_full": (m.get("body_plain_text") or "")[:3000]})
    return out

_QHDR = re.compile(
    r"((?:On [^\n]{5,90}?wrote:)"                          # On Thu, 11 Jun 2026 ... wrote:
    r"|(?:[^\n<>.,!?。，！？：:]{0,16}<[^>\s]{1,60}@[^>\s]{1,60}>\s*于[^\n]{2,40}?写道[：:])"  # 严胜南 <..> 于..写道：
    r"|(?:-{2,}\s*原始邮件\s*-{2,}\s*\n?\s*发件人[:：][^\n]{0,100})"   # QQ：---原始邮件--- 发件人:
    r"|(?:发件人[:：][^\n]{0,100})"
    r"|(?:From:\s*\"[^\"]{1,60}\"<[^>]{0,80}>))"
)
_QMETA = re.compile(r"^\s*((?:Date|Subject|To|Cc|发送时间|时间|收件人|主题)[:：][^\n]{0,120}\s*){1,4}")

def _parse_quote_chain(raw: str) -> list:
    """把含引用链的邮件原文拆成层级 → [{header, text}]，第 0 层 header 为空。"""
    parts = _QHDR.split(raw or "")
    chain = [{"header": "", "text": (parts[0] or "").strip()}]
    i = 1
    while i + 1 <= len(parts) - 1:
        header = parts[i].strip()
        text   = (parts[i+1] or "").strip()
        # From: 块后紧跟的 Date/Subject/To 行并入 header
        m = _QMETA.match(text)
        if m:
            header = header + "  " + " · ".join(
                x.strip() for x in m.group(0).split("\n") if x.strip())[:120]
            text = text[m.end():].strip()
        if text:
            chain.append({"header": header, "text": text})
        i += 2
    return chain[:7]

_SIG_MARKS = ("User Growth Department", "用户增长部门", "x.com/Bloome_im")

def _strip_my_signature(t: str) -> str:
    """裁掉我方邮件尾部的 HTML 签名块（logo/社媒链接转文字后是一坨噪音）。"""
    cut = len(t)
    for mk in _SIG_MARKS:
        i = t.find(mk)
        if i != -1:
            cut = min(cut, i)
    if cut == len(t):
        return t
    out = t[:cut].rstrip()
    # 收掉残留的落款散行（Best regards, / Bloome / Ivor 各占一行的尾巴）
    out = re.sub(r"(?:\s*(?:Best regards?,?|Best,|Bloome|Ivor))+\s*$", "", out, flags=re.I).rstrip()
    return (out + "\n\n— Ivor · Bloome") if out else t[:cut].rstrip()

def _dialog_messages(thread_id: str, peer_email: str) -> list:
    """双方对话消息（我 + 对方，时间正序，标注角色）→ [{role,date,text,_full}]。

    AI 生成回复必须能看到"我"说过什么（报价/合作形式/承诺），否则只照对方的话
    泛泛地接，上下文必然对不上（2026-06-11 的教训）。
    注意：同主题群发共享 thread，"我"的邮件要按收件人过滤，只留发给这位 peer 的。
    """
    msgs = _get_thread(thread_id)
    picked = []
    for m in msgs:
        addr = ((m.get("head_from") or {}).get("mail_address") or "").lower()
        if addr == peer_email.lower():
            role = "对方"
        elif addr and addr.endswith(tuple(d.lower() for d in INTERNAL_DOMAINS)):
            tos = [((t or {}).get("mail_address") or "").lower()
                   for t in (m.get("to") or []) if isinstance(t, dict)]
            if tos and peer_email.lower() not in tos:
                continue   # 群发线程里发给别人的，跳过
            role = "我"
        else:
            continue
        said = _strip_quotes(m.get("body_plain_text") or "")
        if role == "我":
            said = _strip_my_signature(said)
        if not said:
            continue
        try:
            ts = int(m.get("internal_date") or 0)
        except Exception:
            ts = 0
        atts = [{"id": a.get("id"), "filename": a.get("filename") or "附件",
                 "content_type": a.get("content_type") or ""}
                for a in (m.get("attachments") or [])
                if not a.get("is_inline") and a.get("id")]
        picked.append({"sort": (ts, m.get("date_formatted") or ""),
                       "mid":  m.get("message_id") or "",
                       "date": (m.get("date_formatted") or "")[:16],
                       "role": role,
                       "text": said[:1000],
                       "atts": atts,
                       "_full": (m.get("body_plain_text") or "")[:3000]})
    picked.sort(key=lambda x: x["sort"])
    return picked

def _email_thread_ids(email: str) -> list:
    return (_cache.get("email_threads") or {}).get(email, [])

# 乐观显示：刚通过本工具发出、飞书 SENT 还没索引到的"我方"消息，先在会话里显示
_sent_optimistic = {}   # email -> [{ts:int, date:str, text:str}]

def _record_sent(email: str, text: str):
    if not email or not text:
        return
    _sent_optimistic.setdefault(email, []).append({
        "ts": int(time.time() * 1000),
        "date": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "text": text[:1500],
    })

def _dialog_messages_all(email: str, extra_tid: str = "", cached_only: bool = False) -> list:
    """跨线程聚合：该邮箱全部往来（QQ 等回信常自成线程，必须合并才是完整对话）。"""
    tids = set(_email_thread_ids(email))
    if extra_tid:
        tids.add(extra_tid)
    if cached_only:
        tids = {t for t in tids if t in _thread_cache}
    out, seen_mid = [], set()
    for tid in tids:
        for p in _dialog_messages(tid, email):
            key = p["mid"] or (p["sort"], p["role"], p["text"][:60])
            if key in seen_mid:
                continue
            seen_mid.add(key)
            out.append(p)
    # 合并刚发出、真实数据还没同步进来的我方消息（去重：正文已在会话里则跳过）
    for s in _sent_optimistic.get(email, []):
        norm = " ".join(s["text"].split())[:60]
        if any(p["role"] == "我" and " ".join(p["text"].split())[:60] == norm for p in out):
            continue
        out.append({"sort": (s["ts"], s["date"]), "mid": "", "date": s["date"][:16],
                    "role": "我", "text": s["text"], "atts": [], "_full": s["text"]})
    out.sort(key=lambda x: x["sort"])
    return out

def _is_first_contact(thread_id: str, peer_email: str) -> bool:
    """是否"对方的第一封邮件"场景（Ivor 规则：只有这种才允许模板回复）。

    允许 = 对方在线程里只说过 1 次，且我方最多发过 1 封（冷启动外联）。
    只要聊开了（对方≥2 次或我方已回复过），必须 AI 结合上下文定制。
    """
    dlg = _dialog_messages(thread_id, peer_email)
    peer_n = sum(1 for p in dlg if p["role"] == "对方")
    my_n   = sum(1 for p in dlg if p["role"] == "我")
    return peer_n <= 1 and my_n <= 1

def _dialog_context(thread_id: str, peer_email: str):
    """→ (双方对话文本, 对方最新一封完整原文)。跨该邮箱的全部线程聚合。"""
    picked = _dialog_messages_all(peer_email, thread_id)
    seq = "\n\n".join(f"[{p['date']}] {p['role']}：{p['text']}" for p in picked[-14:])
    last_peer = next((p for p in reversed(picked) if p["role"] == "对方"), None)
    return seq, (last_peer["_full"] if last_peer else "")

def _fetch_email_body(message_id: str):
    result = _run_json(
        f"lark-cli mail +message --message-id '{message_id}' --as user"
    )
    if not (result and result.get("ok")):
        cached = _mail_store.get_message(message_id)
        if cached:
            raw = cached.get("body_plain_text") or _html_to_text(cached.get("body_html") or "")
            return _strip_quotes(raw), cached.get("date_formatted", ""), raw
        return None, None, None
    data = result.get("data", {})
    raw  = _html_to_text(data.get("body_html") or "") or data.get("body_plain_text", "")
    data["message_id"] = message_id
    data["body_plain_text"] = raw
    _mail_store.upsert_message(data, folder="MESSAGE")
    return _strip_quotes(raw), data.get("date_formatted", ""), raw

TRANSLATE_CACHE_FILE = APP_DATA_DIR / "translate-cache.json"
try:
    _translate_cache = json.loads(TRANSLATE_CACHE_FILE.read_text())
except Exception:
    _translate_cache = {}

def _save_translate_cache():
    try:
        TRANSLATE_CACHE_FILE.write_text(json.dumps(_translate_cache, ensure_ascii=False))
    except Exception:
        pass

# ── 原生双语：按引用链逐块翻译（替代沉浸式翻译扩展，切邮件零抖动） ───────────────
_CJK = re.compile(r"[一-鿿]")

def _blk_key(text: str) -> str:
    return "blk:" + hashlib.sha1(text.strip().encode("utf-8", "ignore")).hexdigest()[:20]

def _translate_chain_blocks(texts: list) -> list:
    """逐块翻译（内容哈希缓存，跨邮件复用——同一封外联原文只翻一次）。
    返回与输入等长的译文列表；空块/本身是中文的块返回 ""。"""
    def _mostly_cjk(t):
        seg = t[:600]
        cjk   = len(_CJK.findall(seg))
        latin = len(re.findall(r"[A-Za-z]", seg))
        return cjk >= 10 and cjk * 2 > latin   # 中文占主导才算中文邮件
    out, todo = [None] * len(texts), []
    for i, t in enumerate(texts):
        t = (t or "").strip()
        # 注意：不能"见到汉字就跳过"——外文邮件常用中文名称呼（Olá, 严胜南!）
        if not t or _mostly_cjk(t):
            out[i] = ""
            continue
        k = _blk_key(t)
        if k in _translate_cache:
            out[i] = _translate_cache[k]
        else:
            todo.append(i)

    def _one(src: str) -> str:
        r = subprocess.run(["claude", "--model", "haiku", "-p",
                            "把下面这段英文邮件内容翻译成简体中文，只输出译文，不要任何解释：\n\n" + src[:2200]],
                           capture_output=True, text=True, timeout=90)
        return (r.stdout or "").strip() if r.returncode == 0 else ""

    if todo:
        SEP = "=====BLK====="
        try:
            if len(todo) == 1:
                parts = [_one(texts[todo[0]])]
            else:
                src = ("\n" + SEP + "\n").join(texts[i][:2200] for i in todo)
                r = subprocess.run(["claude", "--model", "haiku", "-p",
                                    f"把下面几段英文邮件内容逐段翻译成简体中文。段与段之间用 {SEP} 分隔，"
                                    f"输出必须用同样的分隔符且保持段数一致。只输出译文，不要任何解释。\n\n" + src],
                                   capture_output=True, text=True, timeout=150)
                parts = [p.strip() for p in (r.stdout or "").split(SEP)]
                if r.returncode != 0 or len(parts) != len(todo):
                    parts = [_one(texts[i]) for i in todo]   # 分隔符没对齐 → 逐块兜底
            for j, i in enumerate(todo):
                out[i] = parts[j]
                if parts[j]:
                    _translate_cache[_blk_key(texts[i])] = parts[j]
            _save_translate_cache()
        except Exception:
            for i in todo:
                if out[i] is None:
                    out[i] = ""
    return out

# ── Reply engine: DeepSeek first (HTTP, ~2-5s), fall back to local `claude` CLI ──
# Key is read from a local file (chmod 600), never hardcoded in source.
#   primary:  ~/.replyflow/deepseek.key   legacy fallback: ~/.bloome-deepseek.key
DEEPSEEK_KEY_FILE = APP_DATA_DIR / "deepseek.key"
LEGACY_DEEPSEEK_KEY_FILE = Path.home() / ".bloome-deepseek.key"

def _deepseek_key() -> str:
    for path in (DEEPSEEK_KEY_FILE, LEGACY_DEEPSEEK_KEY_FILE):
        try:
            key = path.read_text().strip()
            if key:
                return key
        except Exception:
            pass
    return ""

def _llm_compose(prompt: str, engine: str = "deepseek", timeout: int = 90):
    """返回 (text, 实际使用的引擎)。engine ∈ deepseek / claude / codex。

    选中的引擎失败时按 deepseek → claude 顺序回退（跳过已试过的），保证总有产出。
    """
    def _try(eng: str) -> str:
        try:
            if eng == "deepseek":
                key = _deepseek_key()
                if not key:
                    return ""
                import urllib.request
                req = urllib.request.Request(
                    "https://api.deepseek.com/chat/completions",
                    data=json.dumps({
                        "model": "deepseek-chat",
                        "messages": [{"role": "user", "content": prompt}],
                        "temperature": 0.7, "max_tokens": 700, "stream": False,
                    }).encode("utf-8"),
                    headers={"Content-Type": "application/json",
                             "Authorization": "Bearer " + key})
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    data = json.loads(r.read())
                return ((data.get("choices") or [{}])[0].get("message", {}) or {}).get("content", "").strip()
            if eng == "claude":
                r = subprocess.run(["claude", "--model", "sonnet", "-p", prompt],
                                   capture_output=True, text=True, timeout=150)
                return (r.stdout or "").strip() if r.returncode == 0 else ""
            if eng == "codex":
                import tempfile
                fd, out_path = tempfile.mkstemp(suffix=".txt")
                os.close(fd)
                try:
                    r = subprocess.run(
                        ["codex", "exec", "-s", "read-only", "--ephemeral",
                         "--skip-git-repo-check", "--color", "never",
                         "-o", out_path, prompt],
                        capture_output=True, text=True, timeout=180, cwd="/tmp")
                    txt = ""
                    try:
                        txt = Path(out_path).read_text().strip()
                    except Exception:
                        pass
                    return txt if r.returncode == 0 and txt else ""
                finally:
                    try:
                        os.unlink(out_path)
                    except Exception:
                        pass
        except Exception as e:
            print(f"[{eng}] 生成失败：{e}")
        return ""

    engine = (engine or "deepseek").lower()
    order = [engine] + [e for e in ("deepseek", "claude") if e != engine]
    for eng in order:
        text = _try(eng)
        if text:
            return text, eng
    return "", ""

# ── 意图分析：对方最新邮件 → 同意/报价/提问/拒绝（列表徽章 + 智能排序用） ────────
def _intent_key(text: str) -> str:
    return "int:" + hashlib.sha1(text.strip().encode("utf-8", "ignore")).hexdigest()[:20]

def _classify_intent(last_peer_text: str) -> dict:
    k = _intent_key(last_peer_text)
    c = _translate_cache.get(k)
    if isinstance(c, dict):
        return c
    prompt = (
        "你在帮 KOL 商务负责人快速分诊邮件。对方（创作者）最新一封邮件内容：\n\n"
        + last_peer_text[:600] +
        "\n\n判断对方当前意图，只输出一行 JSON（不要解释）："
        '{"label":"同意|报价|提问|拒绝|待回应|寒暄","price":"对方提到的报价（如 250rmb/月、$100/once），没提则空"}\n'
        "说明：同意=接受合作/价格；报价=提出或讨论价格；提问=有具体问题等我答；"
        "拒绝=明确不合作；待回应=在等我方下一步；寒暄=无实质内容。"
    )
    out = {"label": "", "price": ""}
    try:
        text, _e = _llm_compose(prompt, "deepseek", timeout=45)
        m = re.search(r"\{.*?\}", text, re.S)
        if m:
            j = json.loads(m.group(0))
            out = {"label": str(j.get("label", ""))[:6], "price": str(j.get("price", ""))[:24]}
    except Exception:
        pass
    if out["label"]:
        _translate_cache[k] = out
        _save_translate_cache()
    return out

def _chain_for(message_id: str):
    """取该邮件的原文引用链（优先 thread 缓存）。"""
    item = next((i for i in (_cache.get("inbox") or []) if i["message_id"] == message_id), None)
    raw = None
    if item:
        for m in _get_thread(item.get("thread_id", "")):
            if m.get("message_id") == message_id:
                raw = m.get("body_plain_text", "")
                break
    if raw is None:
        _b, _d, raw = _fetch_email_body(message_id)
    return _parse_quote_chain(raw) if raw else []

# ── API ───────────────────────────────────────────────────────────────────────
# ── 每小时自动同步：回复了的博主 → 飞书状态 待联系/已联系 → 沟通中 ─────────────
_auto_sync = {"last": "", "updated_total": 0, "last_updated": 0}

def _status_sync_once():
    inbox = load_data(force=True)
    n = 0
    for item in inbox:
        if not item.get("matched") or not item.get("record_id"):
            continue
        st = item.get("feishu_status") or ""
        if "待联系" in st or "已联系" in st:
            if update_feishu_status(item["record_id"], "沟通中"):
                item["feishu_status"] = '["沟通中"]'
                n += 1
            time.sleep(0.4)
    _auto_sync["last"] = datetime.now().strftime("%m-%d %H:%M")
    _auto_sync["last_updated"] = n
    _auto_sync["updated_total"] += n
    print(f"[sync] 飞书状态推进 {n} 条（待联系/已联系 → 沟通中）")

def _status_sync_loop():
    while not _preload_status["finished"]:
        time.sleep(5)
    while True:
        try:
            _status_sync_once()
        except Exception as e:
            print(f"[sync] 失败: {e}")
        time.sleep(3600)   # 每 1 小时

@app.route("/api/preload-status")
def api_preload_status():
    return jsonify(_preload_status)

@app.route("/api/replies")
def api_replies():
    inbox = load_data(force=request.args.get("refresh") == "1")
    for item in inbox:
        if item["email"] not in _draft_previews and _preload_status["finished"]:
            try:
                _gen_previews_for(item)
            except Exception:
                pass
        # 列表摘要 + 意图徽章（缓存）+ 待跟进判定（只读已缓存线程，零网络/零 LLM）
        try:
            msgs = _dialog_messages_all(item["email"], cached_only=True)
            pm = [p for p in msgs if p["role"] == "对方"]
            if pm and not item.get("snippet"):
                item["snippet"] = " ".join(pm[-1]["text"].split())[:110]
            if pm and not item.get("action") and not item.get("intent"):
                c = _translate_cache.get(_intent_key(pm[-1]["text"]))
                if isinstance(c, dict):
                    item["intent"] = c
            # 二轮待回：我参与过对话、对方又回来了（批量处理谈判轮的精准筛选）
            if not item.get("action") and msgs:
                item["round2"] = any(p["role"] == "我" for p in msgs)
            # 矫正：对方在我方处理之后又来信 → 打回「待处理」（防状态残留漏看回复）
            if item.get("action") in ("sent", "drafted", "replied") and msgs \
                    and msgs[-1]["role"] == "对方" \
                    and msgs[-1]["date"] > (item.get("action_ts") or ""):
                item["action"] = ""
            # 待跟进：我方最后发言、对方满 24 小时没动静（每次请求重算——时间在流逝）
            if item.get("action") in ("sent", "replied") and msgs:
                item["followup"] = False
                if msgs[-1]["role"] == "我":
                    try:
                        dt = datetime.strptime(msgs[-1]["date"], "%Y-%m-%d %H:%M")
                        item["followup"] = (datetime.now() - dt) >= timedelta(hours=24)
                    except Exception:
                        pass
        except Exception:
            pass
    stats = {
        "total":        len(inbox),
        "unread":       sum(1 for i in inbox if i["unread"] and not i["action"]),
        "pending":      sum(1 for i in inbox if not i["action"]),
        "sent":         sum(1 for i in inbox if i["action"] == "sent"),
        "drafted":      sum(1 for i in inbox if i["action"] == "drafted"),
        "replied":      sum(1 for i in inbox if i["action"] == "replied"),
        "not_suitable": sum(1 for i in inbox if i["action"] == "not_suitable"),
    }
    return jsonify({"ok": True, "stats": stats, "items": inbox,
                    "send_mode": _send_state["mode"], "auto_sync": _auto_sync,
                    "status_options": get_status_options(),   # 下拉/颜色以飞书该字段为准
                    "refreshing": _reload_lock["running"]})

@app.route("/api/search")
def api_search():
    """按内容搜博主：在已缓存的完整往来正文里找 q，返回命中的 message_id 列表。
    依赖线程缓存（预热后全量在内存）；没缓存到的线程会跳过（前端仍有本地字段匹配兜底）。"""
    q = (request.args.get("q") or "").strip().lower()
    if not q:
        return jsonify({"ok": True, "q": q, "mids": []})
    inbox = _cache.get("inbox") or []
    et = _cache.get("email_threads") or {}
    mids = []
    for it in inbox:
        email = it.get("email", "")
        blob = " ".join([it.get("subject", "") or "", it.get("snippet", "") or "",
                         it.get("from_raw", "") or "", email]).lower()
        hit = q in blob
        if not hit:
            for tid in et.get(email, []):
                msgs = _thread_cache.get(tid)
                if not msgs:
                    continue
                if any(q in (m.get("body_plain_text") or "").lower() for m in msgs):
                    hit = True
                    break
        if hit:
            mids.append(it["message_id"])
    return jsonify({"ok": True, "q": q, "mids": mids})

@app.route("/api/email/<message_id>")
def api_email(message_id):
    item = next((i for i in (_cache.get("inbox") or []) if i["message_id"] == message_id), None)
    body = date = raw = None
    if item:
        # 先走 thread 缓存（零网络调用）
        for m in _get_thread(item.get("thread_id", "")):
            if m.get("message_id") == message_id:
                raw  = m.get("body_plain_text", "")
                body = _strip_quotes(raw)
                date = m.get("date_formatted", "")
                break
    if body is None:
        body, date, raw = _fetch_email_body(message_id)
    if body is None:
        return jsonify({"ok": False, "error": "读取失败"})
    cached_trans = _translate_cache.get(message_id, "")
    # 对话时间线（对方历次发言）
    conv = []
    item = next((i for i in (_cache.get("inbox") or []) if i["message_id"] == message_id), None)
    if item:
        try:
            conv = [{"date": m["date"], "text": m["text"]}
                    for m in _peer_messages(item.get("thread_id", ""), item["email"])]
        except Exception:
            conv = []
    if not conv:
        conv = [{"date": date or "", "text": body}]
    chain = _parse_quote_chain(raw)
    dialog = []
    if item:
        dialog = [{"role": p["role"], "date": p["date"], "text": p["text"],
                   "mid": p.get("mid", ""), "atts": p.get("atts", [])}
                  for p in _dialog_messages_all(item["email"], item.get("thread_id", ""))][-30:]
    def _mark():
        if mark_email_read(message_id) and _cache["inbox"]:
            for it in _cache["inbox"]:
                if it["message_id"] == message_id:
                    it["unread"] = False
                    break
    threading.Thread(target=_mark, daemon=True).start()
    return jsonify({"ok": True, "body": body[:3000], "date": date,
                    "conversation": conv, "chain": chain, "dialog": dialog,
                    "translation": cached_trans})

@app.route("/api/attachment")
def api_attachment():
    """点附件 → 取飞书临时下载直链（带鉴权 code，~1h 有效），302 跳过去下载/预览。"""
    mid = request.args.get("mid", "")
    aid = request.args.get("aid", "")
    if not mid or not aid:
        return "缺参数", 400
    params = json.dumps({"user_mailbox_id": "me", "message_id": mid,
                         "attachment_ids": [aid]})
    r = subprocess.run(
        ["lark-cli", "mail", "user_mailbox.message.attachments", "download_url",
         "--params", params, "--as", "user"],
        capture_output=True, text=True)
    try:
        for raw in (r.stdout, r.stderr):
            if not raw:
                continue
            d = json.loads(raw[raw.index("{"):])
            urls = (d.get("data") or d).get("download_urls") or []
            if urls and urls[0].get("download_url"):
                from flask import redirect
                return redirect(urls[0]["download_url"])
    except Exception as e:
        print(f"[attachment] {e}")
    return "附件链接获取失败（可能已过期，刷新重试）", 502

@app.route("/api/translate-texts", methods=["POST"])
def api_translate_texts():
    """通用批量翻译：对话时间线 / AI 生成回复的中文核验都走这里（内容哈希缓存）。"""
    texts = (request.json or {}).get("texts", [])
    if not isinstance(texts, list) or not texts:
        return jsonify({"ok": False, "error": "no texts"})
    return jsonify({"ok": True, "trans": _translate_chain_blocks([str(t) for t in texts[:40]])})

@app.route("/api/translate-chain/<message_id>")
def api_translate_chain(message_id):
    """原生双语：返回该邮件引用链逐块的中文译文（与 chain 等长，已预热则瞬时）。"""
    chain = _chain_for(message_id)
    if not chain:
        return jsonify({"ok": False, "error": "读取失败"})
    trans = _translate_chain_blocks([c["text"] for c in chain])
    return jsonify({"ok": True, "trans": trans})

@app.route("/api/translate/<message_id>")
def api_translate(message_id):
    if message_id in _translate_cache:
        return jsonify({"ok": True, "text": _translate_cache[message_id], "cached": True})
    item = next((i for i in (_cache.get("inbox") or []) if i["message_id"] == message_id), None)
    src_text = ""
    if item:
        conv = _peer_messages(item.get("thread_id", ""), item["email"])
        src_text = "\n\n".join(f"[{m['date']}]\n{m['text']}" for m in conv[-8:])
    if not src_text:
        body, _d, _r = _fetch_email_body(message_id)
        src_text = body or ""
    if not src_text:
        return jsonify({"ok": False, "error": "读取邮件失败"})
    prompt = (
        "下面是对方（海外创作者）发来的历次英文邮件（时间正序）。请输出两部分：\n"
        "第一行以「📌 意图：」开头，用一句话概括对方当前的意图/态度（如：询问报价 / 同意合作 / 砍价中 / 拒绝）。\n"
        "然后空一行，按时间逐条输出中文翻译，每条保留 [时间] 前缀。除此之外不要任何解释。\n\n" + src_text[:3000]
    )
    try:
        r = subprocess.run(["claude", "--model", "haiku", "-p", prompt],
                           capture_output=True, text=True, timeout=90)
        out = (r.stdout or "").strip()
        if r.returncode != 0 or not out:
            return jsonify({"ok": False, "error": "翻译失败"})
        _translate_cache[message_id] = out
        try:
            TRANSLATE_CACHE_FILE.write_text(json.dumps(_translate_cache, ensure_ascii=False))
        except Exception:
            pass
        return jsonify({"ok": True, "text": out})
    except subprocess.TimeoutExpired:
        return jsonify({"ok": False, "error": "翻译超时"})

# ── 合作落地 playbook：仅"已谈定/进入落地"的付费合作才套用，普通邮件忽略 ──────────
_COLLAB_PLAYBOOK = (
    "【合作落地背景知识 —— 仅当这确实是一桩付费推广合作、且已进入「谈定价格/安排落地」阶段时才参考；"
    "若对方只是初次咨询、闲聊、拒绝、还在纯砍价、或问与合作落地无关的问题，请完全忽略本节、按正常邮件回复，不要硬塞下面的步骤】\n"
    "- 合作形式：在对方的 GitHub 仓库 README 里插入 Bloome 入口（一个链接 + 一张官方图片）。\n"
    "- 推荐做法：请对方在 bloome.im 注册/登录后建一个 agent，把他自己的 GitHub 仓库链接直接粘贴给 agent 让它学习，"
    "跑通后会生成一个 bloome.im/join/... 分享链接——这个链接就是要放进 README 的那个。\n"
    "- CTA 链接必须用对方自建 agent 的 join 分享链接，绝不要用 ref 链接 / 邀请码 / 官网裸链。\n"
    "- README 里除了链接，还要放上我会随邮件附上的官方图片——要提醒对方一起放上去。\n"
    "- 价格谈定后的标准下一步：给出上述搭建指引 + 提醒带上附件里的官方图片 + 索要收款信息"
    "（海外用 PayPal 邮箱；走支付宝的要：真实姓名、手机号、居住省市、支付宝收款二维码）。\n"
    "- 若对方问「怎么配置 / 有没有教程」：用纯文本数字编号把搭建步骤一步步写清楚"
    "（注册登录 → 新建 agent → 把仓库链接粘贴给它学习 → 试跑一下 → 获取 bloome.im/join 分享链接 → 替换进 README 并附上官方图片）。\n"
    "- 上线后请对方把仓库链接发回确认，确认无误再付款。\n"
)

# ── 可后台编辑的提示词（存 vault 外文件，改完即时生效，不动代码）─────────────────
PROMPTS_FILE = APP_DATA_DIR / "prompts.json"
_PROMPT_DEFAULTS = {
    "persona": "你是 Ivor，Bloome（AI Agent IM 平台，bloome.im）的用户增长负责人，"
               "正在和一位创作者/开发者通过邮件洽谈付费推广合作。",
    "rules": (
        "- 对方最新一封里的每个具体点（问题、报价、链接、提议、顾虑）都要逐一明确回应，不允许无视任何一点\n"
        "- 严禁泛泛的产品介绍/模板话术（'Quick recap'、'Bloome is an AI-agent IM platform' 这类一律禁止，除非对方明确问产品是什么）\n"
        "- 【报价规则】只有当我的指示里明确给出了具体价格/数字（如\"答应 150/月\"\"按 200 报价\"）时，"
        "才把那个价格清楚准确地写进邮件；我没给具体数字时绝不自行编造或主动抛出任何价格——"
        "这种情况下若需要往下谈，只礼貌询问对方是否还有下调/灵活空间"
        "（is there any flexibility on the rate / room to come down），把报价权留给对方\n"
        "- 「我」已经说过的内容（自我介绍、已报的价、已问过的问题）绝不重复；对方已经给过的信息（报价/链接）绝不再要一遍\n"
        "- 若对话已有来回，回复必须读起来像谈判的自然下一步；只有对方是第一次来信且无具体内容时，才允许轻量的感谢+表达合作意向"
    ),
    "playbook": _COLLAB_PLAYBOOK,
    "farewell": (
        "- 委婉但明确：这次合作暂时无法推进，原因是双方在预算/价格上未能对齐\n"
        "- 真诚感谢对方投入的时间和分享的资料\n"
        "- 表达诚意：希望未来预算/时机合适时再合作，保持联系\n"
        "- 不要再讨价还价、不要留谈判口子、不要暗示对方降价就能成"
    ),
}

def _load_prompts():
    p = dict(_PROMPT_DEFAULTS)
    try:
        ov = json.loads(PROMPTS_FILE.read_text())
        for k, v in ov.items():
            if k in _PROMPT_DEFAULTS and isinstance(v, str) and v.strip():
                p[k] = v
    except Exception:
        pass
    return p

_prompts = _load_prompts()

def _build_reply_prompt(email, seq, last_full, note, lang, instruction, short, is_collab):
    """单封 + 批量共用的回复 prompt 构造器（要求/人设来自可编辑的 _prompts）。"""
    P = _prompts
    short_rule = ("- 极简风格：30~60 词，直奔主题，删掉一切可有可无的修饰；但开头必须保留称呼问候（如 Hi <对方名字>,），结尾保留落款\n"
                  if short else
                  "- 语气专业、友好、简洁，默认 80~150 词；但若这一步需要给出搭建步骤/配置教程，可用纯文本数字编号分步写清楚，长度不限\n")
    return (
        P["persona"] + "\n\n"
        + f"双方往来邮件（时间正序；「我」=Ivor 方，「对方」={email}）：\n{seq}\n\n"
        + (f"对方最新一封完整原文（含引用链，可还原谈判过程）：\n{last_full}\n\n" if last_full else "")
        + (f"内部备注：{note}\n\n" if note else "")
        + (P["playbook"] + "\n" if is_collab else "")
        + (f"我的指示：{instruction}\n\n" if instruction else "我的指示：根据上下文，写出最合适的下一步回复\n\n")
        + "要求（必须全部遵守）：\n" + P["rules"] + "\n"
        + short_rule
        + f"- 用对方的语言回复（对方语种=「{lang}」；若我的指示明确要求其它语言则从指示）\n"
        + "- 纯文本邮件：绝不使用任何 markdown 语法（不要 **加粗**、# 标题、`代码`、徽章图片）；数字编号步骤直接用 '1. 2. 3.' 纯文本即可\n"
        + "- 只输出邮件正文，不要主题行、不要任何解释\n"
        + "- 结尾落款：\nBest,\nIvor · Bloome"
    )

@app.route("/api/prompts", methods=["GET", "POST"])
def api_prompts():
    global _prompts
    if request.method == "GET":
        return jsonify({"ok": True, "prompts": _prompts, "defaults": _PROMPT_DEFAULTS})
    data = request.json or {}
    new = {k: str(data.get(k, "")).strip() for k in _PROMPT_DEFAULTS}
    # 空值回落默认，避免误清空导致 prompt 残缺
    for k in _PROMPT_DEFAULTS:
        if not new[k]:
            new[k] = _PROMPT_DEFAULTS[k]
    try:
        PROMPTS_FILE.write_text(json.dumps(new, ensure_ascii=False, indent=1))
        _prompts = new
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})

# ── AI 回复质量评分：打分+备注存 jsonl，供后续优化 prompt 参考 ───────────────────
RATINGS_FILE = APP_DATA_DIR / "ai-ratings.jsonl"

@app.route("/api/rate-reply", methods=["POST"])
def api_rate_reply():
    data = request.json or {}
    try:
        score = int(data.get("score") or 0)
    except Exception:
        score = 0
    if not (1 <= score <= 5):
        return jsonify({"ok": False, "error": "评分须 1~5"})
    rec = {"ts": datetime.now().strftime("%Y-%m-%d %H:%M"),
           "email": str(data.get("email", ""))[:120],
           "engine": str(data.get("engine", ""))[:20],
           "instruction": str(data.get("instruction", ""))[:300],
           "reply": str(data.get("reply", ""))[:1500],
           "score": score,
           "note": str(data.get("note", ""))[:500]}
    try:
        with open(RATINGS_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})

@app.route("/api/ratings")
def api_ratings():
    rows = []
    try:
        for line in RATINGS_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    except Exception:
        pass
    rows.reverse()   # 最新在前
    avg = round(sum(r.get("score", 0) for r in rows) / len(rows), 2) if rows else 0
    return jsonify({"ok": True, "rows": rows[:80], "total": len(rows), "avg": avg})

# ── 付款审批半自动预填：AI 抠对话里的付款信息，规则字段由代码确定 ───────────────
@app.route("/api/payment-draft", methods=["POST"])
def api_payment_draft():
    data = request.json or {}
    mid  = data.get("message_id", "")
    item = next((i for i in (_cache.get("inbox") or []) if i["message_id"] == mid), None)
    if not item:
        return jsonify({"ok": False, "error": "not found"})
    email = item["email"]
    seq, _last = _dialog_context(item.get("thread_id", ""), email)
    note = item.get("note", "")
    url  = item.get("url", "")
    prompt = (
        "从下面这位 KOL 与我（Bloome 增长负责人 Ivor）的完整邮件往来 + 内部备注中，"
        "提取『付款申请』所需信息。务必以双方最终谈定的结果为准（砍价过程中的旧数字忽略）。\n\n"
        f"对方邮箱：{email}\n往来（时间正序）：\n{seq}\n\n"
        + (f"内部备注：{note}\n" if note else "")
        + (f"对方主页：{url}\n" if url else "")
        + "\n只输出一行 JSON（不要解释、不要 markdown）：\n"
        '{"method":"付款方式，必须是 PayPal/支付宝/银行转账/Payoneer/Stripe 之一（银行类一律填 银行转账）",'
        '"amount":"金额数字，如 100",'
        '"currency":"币种代码，如 USD/CNY/EUR",'
        '"period":"合作周期，如 一年/一个月/一次性，没提到填 一次性",'
        '"payee":"收款账户：PayPal 填邮箱或付款链接；支付宝填账号/姓名等；银行填 IBAN/SWIFT 等；没有就空",'
        '"resource_link":"对方已上线的资源/仓库 README 链接（对方说已放好 Bloome 介绍的那个），没有就空"}'
    )
    extracted = {}
    try:
        out, _eng = _llm_compose(prompt, data.get("engine") or "deepseek", timeout=60)
        m = re.search(r"\{.*\}", out, re.S)
        if m:
            extracted = json.loads(m.group(0))
    except Exception as e:
        print(f"[payment-draft] 提取失败: {e}")
    method   = (extracted.get("method") or "").strip()
    amount   = str(extracted.get("amount") or "").strip()
    currency = (extracted.get("currency") or "").strip().upper() or "USD"
    period   = (extracted.get("period") or "一次性").strip()
    payee    = (extracted.get("payee") or "").strip()
    res_link = (extracted.get("resource_link") or "").strip() or url
    # 规则化字段（代码确定，不靠 AI）
    entity   = "中国大陆" if method == "支付宝" else "中国香港"
    platform = {"github": "GitHub", "youtube": "YouTube"}.get(item.get("platform"), "")
    amt_txt  = f"{amount}{currency}" if amount else ""
    reason   = (f"{platform} KOL合作付款" + (f" {amt_txt}/{period}" if amt_txt else "")).strip()
    if res_link:
        reason += "\n" + res_link
    return jsonify({"ok": True, "draft": {
        "urgent": "否（默认每周三付款）",
        "due_date": datetime.now().strftime("%Y-%m-%d"),
        "reason": reason,
        "category": "KOL推广",
        "project": "Bloome",
        "entity": entity,
        "method": method,
        "amount": amount,
        "currency": currency,
        "payee": payee,
        "has_invoice": "是（发票见附件）",
        "resource_link": res_link,
    }})

@app.route("/api/compose", methods=["POST"])
def api_compose():
    """AI 定制回复：读对方完整沟通上下文 + 用户中文指示 → claude 生成。"""
    data        = request.json or {}
    mid         = data.get("message_id", "")
    instruction = (data.get("instruction") or "").strip()
    short       = bool(data.get("short"))
    inbox = _cache.get("inbox") or []
    item  = next((i for i in inbox if i["message_id"] == mid), None)
    if not item:
        return jsonify({"ok": False, "error": "not found"})
    seq, last_full = _dialog_context(item.get("thread_id", ""), item["email"])
    if not seq:
        body, _d, _r = _fetch_email_body(mid)
        seq = body or "（无内容）"
    note = item.get("note", "")
    lang = _peer_lang(item)
    is_collab = bool(re.search(r"达成合作|推进制作", item.get("feishu_status") or ""))
    prompt = _build_reply_prompt(item["email"], seq, last_full, note, lang,
                                 instruction, short, is_collab)
    try:
        out, engine = _llm_compose(prompt, data.get("engine") or "deepseek")
        if not out:
            return jsonify({"ok": False, "error": "生成失败，请重试"})
        _mail_store.save_local_draft(mid, item["email"], out, f"ai-compose:{engine}")
        return jsonify({"ok": True, "text": out, "engine": engine})
    except Exception:
        return jsonify({"ok": False, "error": "生成超时，请重试"})

@app.route("/api/preview", methods=["POST"])
def api_preview():
    data       = request.json or {}
    email      = data.get("email", "")
    repo_idx   = int(data.get("repo_idx", 0))
    draft_type = data.get("draft_type", "readme")
    lang       = data.get("lang", "en")
    inbox = _cache.get("inbox") or []
    item  = next((i for i in inbox if i["email"] == email), None)
    if not item:
        return jsonify({"ok": False, "error": "not found"})
    cached = _draft_previews.get(email, {})
    repos  = cached.get("repos", [])
    name_part = item["from_raw"].split("<")[0].strip()
    name      = (name_part.split()[0] if name_part else "") or "there"
    zh = lang == "zh"
    if draft_type == "readme" and repos:
        repo   = repos[min(repo_idx, len(repos)-1)]
        handle = get_github_handle(item["url"])
        text   = preview_github_text_zh(repo, handle or name) if zh else preview_github_text(repo, handle or name)
    elif draft_type == "direct_ad":
        repo = repos[min(repo_idx, len(repos)-1)] if repos else None
        text = preview_direct_ad_text_zh(name, repo) if zh else preview_direct_ad_text(name, repo)
    elif draft_type == "pricing":
        text = preview_youtube_text_zh(item.get("note","")) if zh else preview_youtube_text(item.get("note",""))
    else:
        text = preview_generic_text_zh(name) if zh else preview_generic_text(name)
    return jsonify({"ok": True, "text": text})

def _status_text(raw) -> str:
    """联系状态原始值 '["沟通中"]' / '沟通中' → '沟通中'；空 → '（未填）'。"""
    if not raw:
        return "（未填）"
    try:
        v = json.loads(raw)
        if isinstance(v, list):
            return (str(v[0]) if v else "（未填）")
        return str(v)
    except Exception:
        return str(raw).strip().strip('[]"') or "（未填）"

@app.route("/api/stats")
def api_stats():
    """完整统计：读全量 Base 记录，按联系状态分类计数（不受收件箱 7 天限制）。"""
    try:
        records = get_all_records()
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)[:160]})
    counts, total = {}, 0
    for r in records:
        if not r.get("_email"):
            continue   # 只统计有联系方式（邮箱）的博主行
        total += 1
        st = _status_text(r.get("联系状态"))
        counts[st] = counts.get(st, 0) + 1
    order = status_option_names() + ["（未填）"]
    ordered = [{"status": s, "count": counts.pop(s)} for s in order if s in counts]
    ordered += [{"status": s, "count": c} for s, c in counts.items()]   # 兜底：顺序外的状态
    return jsonify({"ok": True, "total": total, "counts": ordered})

REPLY_IMG_DIR = Path.home() / ".bloome-reply-images"   # 用户随回复上传的图片（数据目录外，不入库）
_IMG_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}

@app.route("/api/upload-image", methods=["POST"])
def api_upload_image():
    """回复里要发的图片：上传 → 存到 ~/.bloome-reply-images → 返回 id（发送时按 id 取回内联）。"""
    import uuid
    f = request.files.get("image")
    if not f or not f.filename:
        return jsonify({"ok": False, "error": "没有文件"})
    ext = os.path.splitext(f.filename)[1].lower()
    if ext not in _IMG_EXTS:
        return jsonify({"ok": False, "error": "仅支持 png/jpg/gif/webp"})
    f.seek(0, os.SEEK_END)
    if f.tell() > 12 * 1024 * 1024:
        return jsonify({"ok": False, "error": "图片超过 12MB"})
    f.seek(0)
    REPLY_IMG_DIR.mkdir(exist_ok=True)
    img_id = uuid.uuid4().hex + ext
    f.save(str(REPLY_IMG_DIR / img_id))
    return jsonify({"ok": True, "id": img_id, "name": f.filename})

@app.route("/api/reply-image/<img_id>")
def api_reply_image(img_id):
    """回显上传图片的缩略图（前端芯片预览用）。"""
    p = REPLY_IMG_DIR / os.path.basename(img_id)
    if not p.exists():
        return "not found", 404
    return send_file(str(p))

def _resolve_reply_images(image_ids):
    """前端传的 id 列表 → 实际绝对路径列表（只保留存在的、防目录穿越）。"""
    out = []
    for i in (image_ids or []):
        p = REPLY_IMG_DIR / os.path.basename(str(i))
        if p.exists():
            out.append(str(p))
    return out

@app.route("/api/approve", methods=["POST"])
def api_approve():
    data       = request.json or {}
    message_id = data.get("message_id", "")
    final_body = data.get("final_body", "").strip()
    with_image = bool(data.get("with_image", False))
    image_paths = _resolve_reply_images(data.get("images"))
    if not final_body:
        return jsonify({"ok": False, "error": "正文为空"})
    inbox = _cache.get("inbox") or []
    item  = next((i for i in inbox if i["message_id"] == message_id), None)
    if not item:
        return jsonify({"ok": False, "error": "not found — 请先刷新"})
    job_id = f"{int(time.time()*1000)}_{item['email'][:8]}"
    _jobs[job_id] = {"status": "sending"}
    snap = {k: item.get(k) for k in ("email", "message_id", "feishu_status", "record_id")}
    _mail_store.save_local_draft(message_id, snap["email"], final_body, "approve")

    def _do():
        try:
            html = _plain_to_html(final_body, with_image)
            rtype, payload = _send_or_draft(snap["message_id"], html, with_image, image_paths)
            if rtype == "sent":
                mark_state(snap["message_id"], snap["email"], "sent")
                mark_email_read(snap["message_id"])
                _record_sent(snap["email"], final_body)   # 乐观显示在会话里
                if any(s in (snap["feishu_status"] or "") for s in ("待联系","已联系")) and snap["record_id"]:
                    update_status(snap["record_id"])
                for it in (_cache.get("inbox") or []):
                    if it["message_id"] == snap["message_id"]:
                        it["unread"] = False
                        break
                _jobs[job_id] = {"status": "sent"}
            elif rtype == "drafted":
                mark_state(snap["message_id"], snap["email"], "drafted", payload)
                mark_email_read(snap["message_id"])
                if any(s in (snap["feishu_status"] or "") for s in ("待联系","已联系")) and snap["record_id"]:
                    update_status(snap["record_id"])
                _jobs[job_id] = {"status": "drafted", "draft_url": payload,
                                 "note": "send 权限未批，已转草稿，去飞书点发送"}
            else:
                _jobs[job_id] = {"status": "error", "error": f"失败: {payload}"}
        except Exception as e:
            _jobs[job_id] = {"status": "error", "error": str(e)}

    threading.Thread(target=_do, daemon=True).start()
    return jsonify({"ok": True, "job_id": job_id})

@app.route("/api/job/<job_id>")
def api_job(job_id):
    return jsonify(_jobs.get(job_id, {"status": "unknown"}))

@app.route("/api/batch-approve", methods=["POST"])
def api_batch_approve():
    """模板批量：只把每封的模板文案放进待发送队列，绝不直接发送。

    审核闸门：用户在「📬 待发送审核」面板里过目/修改后，
    经 /api/pending-send 才真正出门。模板是现成的，这里同步完成即返回。
    """
    data       = request.json or {}
    mids       = set(data.get("message_ids", []))
    lang       = data.get("lang", "en")
    ttype      = data.get("template_type", "generic")
    with_image = bool(data.get("with_image", False))
    inbox      = _cache.get("inbox") or []
    targets    = [i for i in inbox if i["message_id"] in mids]
    if not targets:
        return jsonify({"ok": False, "error": "没有可处理的邮件"})
    queued, skipped, skipped_ctx = 0, [], []
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    label = ("💬通用" if ttype == "generic" else "💰询价") + "/" + lang
    for t in targets:
        email = t["email"]
        # Ivor 规则（2026-06-11）：模板只允许回复"对方的第一封邮件"。
        # 对话已有来回的，模板必然对不上上下文 → 强制拦截，改用 AI 生成。
        if not _is_first_contact(t.get("thread_id", ""), email):
            skipped_ctx.append(email)
            continue
        pv    = _draft_previews.get(email, {})
        text  = pv.get(lang, {}).get(ttype, "") or pv.get("en", {}).get(ttype, "")
        if not text:
            skipped.append(email)
            continue
        _pending[t["message_id"]] = {"email": email, "text": text, "kind": label,
                                     "with_image": with_image, "ts": now}
        queued += 1
    _save_pending()
    return jsonify({"ok": True, "queued": queued, "skipped": skipped,
                    "skipped_ctx": skipped_ctx})

@app.route("/api/batch-compose", methods=["POST"])
def api_batch_compose():
    """AI 批量：逐封读上下文 → sonnet 生成回复 → 进待发送队列（不直接发送）。"""
    if _batch["running"]:
        return jsonify({"ok": False, "error": "已有批量任务在跑"})
    data  = request.json or {}
    mids  = set(data.get("message_ids", []))
    engine = data.get("engine") or "deepseek"
    short  = bool(data.get("short"))
    batch_instr = (data.get("instruction") or "").strip()
    inbox = _cache.get("inbox") or []
    targets = [i for i in inbox if i["message_id"] in mids]
    if not targets:
        return jsonify({"ok": False, "error": "没有可处理的邮件"})
    _batch.update({"running": True, "total": len(targets), "done": 0,
                   "ok": 0, "fail": 0, "results": [], "kind": "compose"})
    snaps = [{**{k: t.get(k) for k in
              ("email", "message_id", "thread_id", "note", "feishu_status")}, "lang": _peer_lang(t)}
             for t in targets]

    def _run():
        for snap in snaps:
            email, mid = snap["email"], snap["message_id"]
            try:
                seq, last_full = _dialog_context(snap.get("thread_id", ""), email)
                if not seq:
                    b, _d, _r = _fetch_email_body(mid)
                    seq = b or "（无内容）"
                instr = (f"（批量统一指示，措辞要贴各自往来）{batch_instr}" if batch_instr else "")
                is_collab = bool(re.search(r"达成合作|推进制作", snap.get("feishu_status") or ""))
                prompt = _build_reply_prompt(email, seq, last_full, snap.get("note", ""),
                                             snap["lang"], instr, short, is_collab)
                text, _eng = _llm_compose(prompt, engine, timeout=90)
                if not text:
                    raise RuntimeError("AI 生成失败")
                _pending[mid] = {"email": email, "text": text, "kind": f"🤖AI·{_eng}",
                                 "with_image": False,
                                 "ts": datetime.now().strftime("%Y-%m-%d %H:%M")}
                _mail_store.save_local_draft(mid, email, text, f"batch-compose:{_eng}")
                _save_pending()
                _batch["results"].append({"email": email, "status": "queued"})
                _batch["ok"] += 1
            except Exception as e:
                _batch["results"].append({"email": email, "status": "error", "error": str(e)[:100]})
                _batch["fail"] += 1
            finally:
                _batch["done"] += 1
        _batch["running"] = False

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"ok": True, "total": len(targets)})

_CJK_TXT = re.compile(r"[一-鿿]")

def _peer_lang(item) -> str:
    """告别信语种：飞书「语种」字段优先，否则从对方邮件内容探测，兜底英语。"""
    f = item.get("feishu") or {}
    lang = (f.get("lang") or "").strip()
    if lang:
        return lang.split("/")[0].strip()
    try:
        pm = _peer_messages(item.get("thread_id", ""), item["email"])
        txt = " ".join(m["text"] for m in pm[-3:])
        if _CJK_TXT.search(txt):
            return "中文"
    except Exception:
        pass
    return "英语"

@app.route("/api/batch-farewell", methods=["POST"])
def api_batch_farewell():
    """批量告别信：飞书标了不合适的博主 → 按对方语种生成礼貌收尾邮件 → 进待发送队列。"""
    if _batch["running"]:
        return jsonify({"ok": False, "error": "已有批量任务在跑"})
    data   = request.json or {}
    mids   = set(data.get("message_ids", []))
    engine = data.get("engine") or "deepseek"
    short  = bool(data.get("short"))
    inbox  = _cache.get("inbox") or []
    # 防御：我自己在工具里标的不合适（action=not_suitable）不发告别信——只对"领导判定"的发
    targets = [i for i in inbox if i["message_id"] in mids and i.get("action") != "not_suitable"]
    if not targets:
        return jsonify({"ok": False, "error": "没有可处理的邮件（自己标的不合适不发告别）"})
    _batch.update({"running": True, "total": len(targets), "done": 0,
                   "ok": 0, "fail": 0, "results": [], "kind": "compose"})
    snaps = []
    for t in targets:
        snaps.append({"email": t["email"], "message_id": t["message_id"],
                      "thread_id": t.get("thread_id", ""), "lang": _peer_lang(t)})

    def _run():
        for snap in snaps:
            email, mid, lang = snap["email"], snap["message_id"], snap["lang"]
            try:
                seq, last_full = _dialog_context(snap["thread_id"], email)
                prompt = (
                    "你是 Ivor，Bloome（AI Agent IM 平台，bloome.im）的用户增长负责人。"
                    "内部已决定不与这位创作者推进合作（主要原因：报价/价格未能达成一致）。\n\n"
                    f"双方往来邮件（时间正序；「我」=Ivor 方，「对方」={email}）：\n{seq or '（无往来记录）'}\n\n"
                    + (f"对方最新一封完整原文：\n{last_full}\n\n" if last_full else "")
                    + f"请写一封礼貌的收尾邮件，要求（必须全部遵守）：\n"
                    f"- **整封邮件必须用「{lang}」写**（包括落款前的告别语；署名固定为 Ivor · Bloome）\n"
                    + _prompts["farewell"] + "\n"
                    + ("- 极简（约 40~60 个英文词的信息量）" if short
                       else "- 简短（约 60~100 个英文词的信息量）")
                    + "；纯文本，绝不使用 markdown 语法\n"
                    "- 只输出邮件正文，不要主题行、不要任何解释"
                )
                text, _eng = _llm_compose(prompt, engine, timeout=90)
                if not text:
                    raise RuntimeError("AI 生成失败")
                _pending[mid] = {"email": email, "text": text,
                                 "kind": f"👋告别·{lang}·{_eng}",
                                 "with_image": False, "farewell": True,
                                 "ts": datetime.now().strftime("%Y-%m-%d %H:%M")}
                _mail_store.save_local_draft(mid, email, text, f"batch-farewell:{_eng}")
                _save_pending()
                _batch["results"].append({"email": email, "status": "queued"})
                _batch["ok"] += 1
            except Exception as e:
                _batch["results"].append({"email": email, "status": "error", "error": str(e)[:100]})
                _batch["fail"] += 1
            finally:
                _batch["done"] += 1
        _batch["running"] = False

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"ok": True, "total": len(targets)})

@app.route("/api/batch-status")
def api_batch_status():
    return jsonify({**_batch, "mode": _send_state["mode"]})

# ── 待发送队列：审核后才出门 ──────────────────────────────────────────────────
@app.route("/api/pending")
def api_pending():
    items = [{"message_id": k, **v} for k, v in _pending.items()]
    items.sort(key=lambda x: (x.get("ts", ""), x.get("email", "")))
    return jsonify({"items": items, "mode": _send_state["mode"]})

@app.route("/api/local-store")
def api_local_store():
    return jsonify({"ok": True, **_mail_store.stats()})

@app.route("/api/local-store/messages")
def api_local_store_messages():
    try:
        limit = min(max(int(request.args.get("limit", "100")), 1), 500)
    except Exception:
        limit = 100
    rows = _mail_store.list_messages(limit=limit)
    return jsonify({"ok": True, "items": rows})

@app.route("/api/pending-context/<message_id>")
def api_pending_context(message_id):
    """审核面板"看上下文"：返回该会话对方历次发言 + 中文译文（判断 AI 稿该不该发）。"""
    p = _pending.get(message_id)
    if not p:
        return jsonify({"ok": False, "error": "不在队列"})
    email = p["email"]
    item  = next((i for i in (_cache.get("inbox") or []) if i["message_id"] == message_id), None)
    tid   = item.get("thread_id", "") if item else ""
    peer  = [m for m in _dialog_messages_all(email, tid) if m["role"] == "对方"][-4:]
    texts = [m["text"] for m in peer]
    trans = _translate_chain_blocks(texts) if texts else []
    msgs  = [{"date": m["date"], "text": m["text"], "trans": trans[i] if i < len(trans) else "",
              "mid": m.get("mid", ""), "atts": m.get("atts", [])}
             for i, m in enumerate(peer)]
    return jsonify({"ok": True, "email": email, "messages": msgs})

@app.route("/api/pending-discard", methods=["POST"])
def api_pending_discard():
    mids = (request.json or {}).get("message_ids", [])
    for m in mids:
        _pending.pop(m, None)
    _save_pending()
    return jsonify({"ok": True, "left": len(_pending)})

@app.route("/api/pending-send", methods=["POST"])
def api_pending_send():
    """把审核通过的队列项真正发出（支持发送前的人工改稿）。"""
    if _batch["running"]:
        return jsonify({"ok": False, "error": "已有批量任务在跑"})
    items = (request.json or {}).get("items", [])   # [{message_id, text}]
    targets = []
    for it in items:
        mid = it.get("message_id")
        p   = _pending.get(mid)
        if not p:
            continue
        targets.append({"mid": mid, "email": p["email"],
                        "text": (it.get("text") or p["text"]).strip(),
                        "with_image": bool(p.get("with_image", False)),
                        "farewell": bool(p.get("farewell", False))})
    if not targets:
        return jsonify({"ok": False, "error": "没有可发送的邮件"})
    inbox_by_mid = {i["message_id"]: i for i in (_cache.get("inbox") or [])}
    _batch.update({"running": True, "total": len(targets), "done": 0,
                   "ok": 0, "fail": 0, "results": [], "kind": "send"})

    def _run():
        for t in targets:
            mid, email = t["mid"], t["email"]
            try:
                _mail_store.save_local_draft(mid, email, t["text"], "pending-send")
                html = _plain_to_html(t["text"], t["with_image"])
                rtype, payload = _send_or_draft(mid, html, t["with_image"])
                if rtype in ("sent", "drafted"):
                    mark_state(mid, email, rtype, payload if rtype == "drafted" else "")
                    mark_email_read(mid)
                    if rtype == "sent":
                        _record_sent(email, t["text"])   # 乐观显示在会话里
                    if t.get("farewell"):
                        mark_farewell(email)   # 出列「待告别」
                    snap = inbox_by_mid.get(mid) or {}
                    if any(s in (snap.get("feishu_status") or "") for s in ("待联系", "已联系")) \
                            and snap.get("record_id"):
                        update_status(snap["record_id"])
                    _pending.pop(mid, None)
                    _save_pending()
                    _batch["results"].append({"email": email, "status": rtype})
                    _batch["ok"] += 1
                else:
                    _batch["results"].append({"email": email, "status": "error", "error": str(payload)})
                    _batch["fail"] += 1
            except Exception as e:
                _batch["results"].append({"email": email, "status": "error", "error": str(e)[:100]})
                _batch["fail"] += 1
            finally:
                _batch["done"] += 1
                time.sleep(0.4)
        _batch["running"] = False

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"ok": True, "total": len(targets)})

# 联系状态选项：直接从飞书该字段动态拉（顺序+颜色都以飞书为准，永不漂移），拉失败才回退到下面这份
_STATUS_FALLBACK = ["待联系", "已联系", "沟通中", "无法合作", "达成合作",
                    "不合适", "需审核", "待定", "已上线", "推进制作"]
_STATUS_FIELD_NAME = "联系状态"
_status_field_id = {"id": "fldQfIwTpZ"}   # 默认值；按字段名查到后更新（字段重建 id 会变、名字不变）
_HUE_CSS = {"Blue": "#3a7bd5", "Orange": "#e08a1e", "Wathet": "#3aa3cf", "Carmine": "#cf4f7a",
            "Green": "#3aaf6e", "Red": "#cf4d3f", "Yellow": "#c79a3e", "Lime": "#7aa93e",
            "Purple": "#9a6de0", "Turquoise": "#3aada0", "Violet": "#8a6de0", "Pink": "#d8618c",
            "Indigo": "#6d7cff", "Gray": "#8a8f99", "Grey": "#8a8f99"}
_status_opts = {"list": None, "ts": 0}   # 缓存 [{name,color}]

def _resolve_status_field_id():
    """按字段名「联系状态」查 field_id（比硬编码 id 稳）；查不到就用上次/默认值。"""
    try:
        r = _run_json(f"lark-cli base +field-list "
                      f"--base-token {BASE_TOKEN} --table-id {TABLE_ID} --as user")
        for f in (((r or {}).get("data") or {}).get("items") or []):
            if (f.get("field_name") or f.get("name")) == _STATUS_FIELD_NAME:
                fid = f.get("field_id") or f.get("id")
                if fid:
                    _status_field_id["id"] = fid
                    break
    except Exception:
        pass
    return _status_field_id["id"]

def get_status_options(force=False):
    """从飞书拉「联系状态」字段的真实选项（含顺序+颜色），缓存 10 分钟；失败回退 _STATUS_FALLBACK。"""
    now = time.time()
    if not force and _status_opts["list"] and now - _status_opts["ts"] < 600:
        return _status_opts["list"]
    out = []
    try:
        fid = _resolve_status_field_id()
        r = _run_json(
            f"lark-cli base +field-search-options "
            f"--base-token {BASE_TOKEN} --table-id {TABLE_ID} "
            f"--field-id {fid} --as user"
        )
        for o in (((r or {}).get("data") or {}).get("options") or []):
            nm = (o.get("name") or "").strip()
            if nm:
                out.append({"name": nm, "color": _HUE_CSS.get(o.get("hue"), "#8a8f99")})
    except Exception:
        out = []
    if out:
        _status_opts.update(list=out, ts=now)
    elif not _status_opts["list"]:
        _status_opts["list"] = [{"name": n, "color": "#8a8f99"} for n in _STATUS_FALLBACK]
    return _status_opts["list"]

def status_option_names():
    return [o["name"] for o in get_status_options()]

FEISHU_STATUS_OPTIONS = _STATUS_FALLBACK   # 兼容旧引用（仅回退默认；实际校验/渲染走动态选项）

@app.route("/api/feishu-update", methods=["POST"])
def api_feishu_update():
    """WebUI 直改飞书表：联系状态（限已有选项，select 字段写未知值会报错）+ 备注。"""
    data = request.json or {}
    mid  = data.get("message_id", "")
    item = next((i for i in (_cache.get("inbox") or []) if i["message_id"] == mid), None)
    if not item or not item.get("record_id"):
        return jsonify({"ok": False, "error": "未匹配到飞书记录"})
    rid = item["record_id"]
    if "status" in data:
        st = str(data["status"]).strip()
        if st not in status_option_names():
            return jsonify({"ok": False, "error": "未知状态选项"})
        if not update_feishu_status(rid, st):
            return jsonify({"ok": False, "error": "写入飞书失败"})
        item["feishu_status"] = st
        if item.get("feishu"):
            item["feishu"]["status"]  = st
            item["feishu"]["updated"] = datetime.now().strftime("%Y-%m-%d %H:%M")
        # 联动本地处理状态：标成不合适 = 这个人不用回了 → 本地也标 ❌；改回其他状态则解除
        if st in ("不合适", "无法合作"):
            if item.get("action") != "not_suitable":
                mark_state(mid, item["email"], "not_suitable")
        elif item.get("action") == "not_suitable":
            mark_state(mid, item["email"], "")
    if "note" in data:
        nt = str(data["note"]).strip()
        if not update_feishu_note(rid, nt):
            return jsonify({"ok": False, "error": "备注写入失败"})
        item["note"] = nt
    return jsonify({"ok": True, "feishu_status": item.get("feishu_status", ""),
                    "note": item.get("note", "")})

@app.route("/api/mark", methods=["POST"])
def api_mark():
    data       = request.json or {}
    message_id = data.get("message_id", "")
    action     = data.get("action", "")
    inbox  = _cache.get("inbox") or []
    item   = next((i for i in inbox if i["message_id"] == message_id), None)
    if not item:
        return jsonify({"ok": False, "error": "not found"})
    feishu_ok = True
    if action == "not_suitable":
        if item.get("record_id"):
            feishu_ok = update_feishu_status(item["record_id"], "不合适")
    elif action == "pending":
        if item.get("action") == "not_suitable" and item.get("record_id"):
            feishu_ok = update_feishu_status(item["record_id"], "沟通中")
    mark_state(message_id, item["email"], action if action != "pending" else "")
    if action in ("replied", "not_suitable", "ignored") and item.get("unread"):
        def _mk():
            if mark_email_read(message_id):
                item["unread"] = False
        threading.Thread(target=_mk, daemon=True).start()
    return jsonify({"ok": True, "feishu_ok": feishu_ok})

# ── HTML ──────────────────────────────────────────────────────────────────────
HTML = r"""<!DOCTYPE html>
<html lang="zh" data-theme="dark">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>📮 Bloome 邮件回复助手</title>
<style>
:root[data-theme="dark"]{
  --bg:#0c0f16; --bg-2:#10141d; --card:#141925; --card-hover:#1a2130; --card-sel:#1c2336;
  --border:#1f2735; --border-2:#2a3447;
  --text:#e8ecf4; --text-2:#9aa4b8; --text-3:#5d6778;
  --accent:#6d7cff; --accent-hover:#7e8bff; --on-accent:#fff;
  --c-blue:#60a5fa; --c-red:#f87171; --c-green:#34d399; --c-amber:#fbbf24;
  --c-indigo:#818cf8; --c-gray:#94a3b8;
  --input-bg:#0a0e17;
}
:root[data-theme="light"]{
  --bg:#f4f5f9; --bg-2:#fbfbfd; --card:#ffffff; --card-hover:#f7f8fe; --card-sel:#eef0ff;
  --border:#e6e9f2; --border-2:#d4d9e6;
  --text:#1a2233; --text-2:#5a6680; --text-3:#9aa3b8;
  --accent:#5b6cff; --accent-hover:#4a5bf0; --on-accent:#fff;
  --c-blue:#2563eb; --c-red:#dc2626; --c-green:#059669; --c-amber:#d97706;
  --c-indigo:#4f46e5; --c-gray:#64748b;
  --input-bg:#f4f5f9;
}
*{box-sizing:border-box}
html,body{margin:0;padding:0;height:100%}
body{background:var(--bg);color:var(--text);font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC",sans-serif;font-size:14px;display:flex;flex-direction:row;overflow:hidden}
::-webkit-scrollbar{width:5px;height:5px}
::-webkit-scrollbar-thumb{background:var(--border-2);border-radius:3px}

/* 飞书邮箱式左侧导航栏 */
.sidebar{width:208px;flex-shrink:0;background:var(--bg-2);display:flex;flex-direction:column;padding:12px 10px 10px;gap:10px;min-height:0;overflow:hidden}
.sb-brand{font-size:14px;font-weight:700;padding:0 6px;white-space:nowrap}
.icon-btn{width:28px;height:28px;border-radius:8px;border:1px solid var(--border);background:var(--card);color:var(--text-2);cursor:pointer;display:inline-flex;align-items:center;justify-content:center;font-size:13px;transition:all .15s;flex-shrink:0}
.icon-btn:hover{border-color:var(--accent);color:var(--accent)}
#search{background:var(--card);border:1px solid var(--border);border-radius:8px;padding:7px 11px;font-size:12px;color:var(--text);outline:none;width:100%;box-sizing:border-box;transition:border-color .15s}
#search:focus{border-color:var(--accent)}
#sync-info{color:var(--text-3);font-size:10.5px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.sb-list{flex:1;overflow-y:auto;display:flex;flex-direction:column;gap:1px;min-height:0}
.sb-item{display:flex;align-items:center;gap:7px;padding:6.5px 10px;border-radius:8px;font-size:12.8px;color:var(--text-2);cursor:pointer;transition:background .12s;user-select:none;white-space:nowrap;overflow:hidden}
.sb-item:hover{background:var(--card)}
.sb-item.active{background:var(--accent);color:var(--on-accent);font-weight:600}
.sb-item .cnt{margin-left:auto;font-size:10.5px;color:var(--text-3);font-weight:600}
.sb-item.active .cnt{color:inherit;opacity:.85}
.sb-sec{font-size:10.5px;color:var(--text-3);padding:11px 10px 4px;font-weight:600;letter-spacing:.5px}
.sb-foot{display:flex;flex-direction:column;gap:8px;flex-shrink:0}
.sb-tools{display:flex;align-items:center;gap:6px}
.main{flex:1;display:flex;flex-direction:column;min-width:0;min-height:0}
.tsep{width:1px;height:15px;background:var(--border-2);margin:0 6px;flex-shrink:0}
.batchbar{display:none;gap:7px;align-items:center;padding:7px 16px;background:color-mix(in srgb,var(--accent) 7%,var(--bg-2));border-bottom:1px solid var(--border);font-size:12px;flex-shrink:0;overflow-x:auto}
.batchbar.show{display:flex}
.batchbar select{background:var(--card);color:var(--text);border:1px solid var(--border);border-radius:7px;padding:4px 8px;font-size:12px}
#batch-progress{margin-left:auto;color:var(--text-2);white-space:nowrap}
#batch-progress a{color:var(--accent)}
/* 待发送审核面板 */
/* 待发送审核 = 右侧 dock（队列导航，点一条→中间聊天区跳到该博主+草稿进回复框，衔接上下文） */
.review-dock{width:300px;flex-shrink:0;background:var(--bg-2);border-left:1px solid var(--border);display:none;flex-direction:column;min-height:0}
.review-dock.show{display:flex}
.rv-head{display:flex;align-items:center;gap:7px;padding:11px 13px;font-weight:600;font-size:13px;border-bottom:1px solid var(--border);flex-shrink:0;flex-wrap:wrap}
.rv-tip{padding:7px 13px;font-size:10.5px;color:var(--text-3);border-bottom:1px solid var(--border);flex-shrink:0;line-height:1.5}
.rv-list{overflow-y:auto;padding:8px;display:flex;flex-direction:column;gap:7px}
.rv-row{border:1px solid var(--border);border-radius:9px;padding:8px 10px;background:var(--card);cursor:pointer;transition:all .12s}
.rv-row:hover{border-color:var(--border-2)}
.rv-row.active{border-color:var(--accent);box-shadow:inset 2px 0 0 var(--accent);background:var(--card-sel)}
.rv-row .rv-em{font-size:12px;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.rv-row .rv-kd{font-size:10px;color:var(--text-3);margin-top:1px}
.rv-row .rv-pv{font-size:11px;color:var(--text-2);margin-top:4px;line-height:1.45;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}

.btn{padding:6px 13px;border-radius:8px;font-size:12.5px;font-weight:600;cursor:pointer;border:none;transition:all .15s;white-space:nowrap;display:inline-flex;align-items:center;gap:5px}
.btn:disabled{opacity:.45;cursor:not-allowed}
.btn-primary{background:var(--accent);color:var(--on-accent)}
.btn-primary:hover:not(:disabled){background:var(--accent-hover)}
.btn-send{background:var(--c-green);color:#fff}
.btn-send:hover:not(:disabled){filter:brightness(1.08)}
.btn-soft{background:var(--card);color:var(--text-2);border:1px solid var(--border)}
.btn-soft:hover:not(:disabled){color:var(--text);border-color:var(--border-2)}
.btn-danger{background:color-mix(in srgb,var(--c-red) 12%,transparent);color:var(--c-red)}
.btn-danger:hover:not(:disabled){background:color-mix(in srgb,var(--c-red) 20%,transparent)}
.btn-ok{background:color-mix(in srgb,var(--c-green) 12%,transparent);color:var(--c-green)}
.btn-sm{padding:4px 9px;font-size:11.5px}

.split{flex:1;display:flex;min-height:0}
.maillist{width:392px;min-width:200px;max-width:60vw;overflow-y:auto;background:var(--bg);flex-shrink:0}
.resizer{width:7px;flex-shrink:0;cursor:col-resize;position:relative;background:transparent}
.resizer::after{content:'';position:absolute;left:3px;top:0;bottom:0;width:1px;background:var(--border);transition:all .15s}
.resizer:hover::after,.resizer.active::after{left:2px;width:3px;background:var(--accent)}
body.resizing{user-select:none;cursor:col-resize}
.pane{flex:1;overflow:hidden;background:var(--bg-2);min-width:0}   /* 整体不滚：聊天区内滚，回复框钉底常驻 */

/* 飞书邮箱式列表行：发件人+时间 / 徽章行 / 摘要 */
.mrow{display:flex;gap:9px;padding:11px 14px 11px 11px;border-bottom:1px solid var(--border);cursor:pointer;transition:background .12s;align-items:flex-start}
.mrow:hover{background:var(--card-hover)}
.mrow.sel{background:var(--card-sel);box-shadow:inset 3px 0 0 var(--accent)}
.mrow input[type=checkbox]{accent-color:var(--accent);width:14px;height:14px;margin-top:2px;flex-shrink:0;cursor:pointer}
.mrow .ck-ph{display:inline-block;width:14px;flex-shrink:0}
.mrow-main{flex:1;min-width:0}
.mrow-1{display:flex;align-items:center;gap:6px;min-width:0}
.dot{width:7px;height:7px;border-radius:50%;background:var(--accent);flex-shrink:0}
.mname{font-weight:600;font-size:13.2px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.mrow.is-unread .mname{font-weight:700}
.mtime{color:var(--text-3);font-size:10.5px;flex-shrink:0;margin-left:auto}
.msub{color:var(--text-3);font-size:11.8px;margin-top:4px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;line-height:1.5}
.mrow.is-unread .msub{color:var(--text-2)}
.mrow-3{display:flex;align-items:center;gap:5px;margin-top:5px;flex-wrap:wrap}
.badge{display:inline-flex;align-items:center;padding:1.5px 8px;border-radius:5px;font-size:10px;font-weight:600;flex-shrink:0;background:color-mix(in srgb,currentColor 10%,transparent)}
.b-gh{color:var(--c-gray)} .b-yt{color:var(--c-gray)} .b-other{color:var(--c-gray)}
.b-pending{color:var(--c-amber)} .b-sent{color:var(--c-green)}
.b-drafted{color:var(--c-indigo)} .b-replied{color:var(--c-blue)} .b-ns{color:var(--c-red)}
.mrow.dim{opacity:.45}

.pane-empty{display:flex;flex-direction:column;align-items:center;justify-content:center;height:100%;color:var(--text-3);gap:10px;font-size:13.5px}
.pane-main{padding:16px 30px;height:100%;display:flex;flex-direction:column;box-sizing:border-box;max-width:920px;margin:0 auto}
.sec-in{flex:1;min-height:30vh;display:flex;flex-direction:column;margin-bottom:10px}
.sec-in .conv{flex:1;max-height:none;min-height:0}   /* min-height:0 关键：长邮件不把底部输入条顶出屏 */
.sec-re{flex-shrink:0;margin-bottom:0;border-top:1px solid var(--border);padding-top:12px}
.p-head{display:flex;align-items:flex-start;gap:12px;margin-bottom:10px;flex-shrink:0}
.p-head-info{flex:1;min-width:0}
.p-sender{font-size:15px;font-weight:700}
.p-addr{color:var(--accent);font-size:12.5px;margin-top:2px;user-select:all}
.p-quick{display:flex;gap:6px;flex-shrink:0;flex-wrap:wrap;justify-content:flex-end}
/* 回复待发送的图片芯片 */
.reply-imgs{display:flex;flex-wrap:wrap;gap:8px;margin:8px 0 0}
.reply-imgs:empty{display:none}
.rimg-chip{position:relative;width:64px;height:64px;border-radius:8px;border:1px solid var(--border);overflow:hidden;background:var(--bg-2);flex-shrink:0}
.rimg-chip img{width:100%;height:100%;object-fit:cover;display:block}
.rimg-chip .rm{position:absolute;top:2px;right:2px;width:18px;height:18px;border:none;border-radius:50%;background:rgba(0,0,0,.6);color:#fff;font-size:12px;line-height:18px;text-align:center;cursor:pointer;padding:0}
.rimg-chip.up{display:flex;align-items:center;justify-content:center;font-size:10px;color:var(--text-3)}
/* 飞书档案卡：该博主在多维表格里的状态/标签/备注，一处看全 */
.f-card{display:flex;flex-wrap:wrap;align-items:center;gap:6px;margin:2px 0 12px;padding:8px 12px;background:var(--card);border:1px solid var(--border);border-radius:10px;font-size:11.5px;color:var(--text-2);flex-shrink:0}
.f-chip{display:inline-flex;align-items:center;gap:5px;padding:2.5px 9px;border-radius:99px;background:var(--bg-2);border:1px solid var(--border);white-space:nowrap}
.f-chip b{color:var(--text);font-weight:600}
.f-chip .k{color:var(--text-3)}
.f-dot{width:7px;height:7px;border-radius:50%;flex-shrink:0}
.f-note{flex-basis:100%;color:var(--text-2);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;margin-top:2px}
.f-card a{color:var(--accent);text-decoration:none;white-space:nowrap}
.f-status-sel{background:transparent;color:var(--text);border:none;outline:none;font-size:11.5px;font-weight:600;cursor:pointer;padding:0;max-width:90px}
#f-note-edit{display:flex;gap:6px;align-items:center}
#f-note-input{flex:1;background:var(--input-bg);border:1px solid var(--border);border-radius:7px;padding:5px 10px;font-size:11.5px;color:var(--text);outline:none}
#f-note-input:focus{border-color:var(--accent)}
.f-card .f-empty{color:var(--text-3)}
.sec{margin-bottom:16px}
.sec-title{font-size:12px;font-weight:700;color:var(--text-2);margin-bottom:7px;display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.email-body{background:var(--input-bg);border:1px solid var(--border);border-radius:10px;padding:12px 14px;font-size:12.8px;color:var(--text-2);line-height:1.75;white-space:pre-wrap;max-height:170px;overflow-y:auto}
.email-body.trans{border-color:color-mix(in srgb,var(--accent) 45%,transparent);color:var(--text)}
.conv{background:transparent;border:none;border-radius:0;padding:4px 2px;max-height:340px;overflow-y:auto}
.conv a{color:var(--accent);word-break:break-all;text-decoration:underline;text-underline-offset:2px}
.cmsg{background:var(--card);border:1px solid var(--border);border-radius:9px;padding:8px 11px}
.cmsg.latest{border-color:color-mix(in srgb,var(--accent) 50%,transparent)}
.cmsg-t{font-size:10.5px;color:var(--text-3);margin-bottom:3px}
.cmsg-x{font-size:12.8px;color:var(--text);line-height:1.7;white-space:pre-wrap;word-break:break-word}
.qmain{font-size:14px;color:var(--text);line-height:1.85;white-space:pre-wrap;word-break:break-word}
.qlvl{border-left:2px solid var(--border-2);padding-left:13px;margin-top:14px;opacity:.92}
.qhdr{font-size:11px;color:var(--text-3);margin-bottom:5px}
.qtxt{font-size:12.8px;color:var(--text-2);line-height:1.75;white-space:pre-wrap;word-break:break-word}
/* 原生双语译文（仿沉浸式翻译：译文紧跟原文，稍淡） */
.qtrans{display:none;font-size:13.2px;color:var(--text-2);line-height:1.85;white-space:pre-wrap;word-break:break-word;margin-top:8px;padding-top:8px;border-top:1px dashed var(--border)}
.qtxt + .qtrans{font-size:12.4px}
/* 微信式聊天气泡：对方靠左、我靠右、最新在底部 */
.brow{display:flex;margin-bottom:10px}
.brow.me{justify-content:flex-end}
.bub{max-width:78%;padding:8px 12px;border-radius:12px;background:var(--card);border:1px solid var(--border)}
.brow.me .bub{background:color-mix(in srgb,var(--accent) 13%,var(--card));border-color:color-mix(in srgb,var(--accent) 28%,var(--border))}
.bub-t{font-size:12.6px;color:var(--text);line-height:1.62;white-space:pre-wrap;word-break:break-word}
.bub-meta{margin-top:5px;font-size:10px;color:var(--text-3);text-align:right}
.brow.peer .bub-meta{text-align:left}
.bub .qtrans{font-size:12px;line-height:1.62;margin-top:6px;padding-top:6px}
/* 附件芯片 */
.atts{display:flex;flex-wrap:wrap;gap:6px;margin-top:8px}
.att{display:inline-flex;align-items:center;gap:6px;max-width:240px;padding:5px 10px;background:var(--bg-2);border:1px solid var(--border);border-radius:8px;font-size:11.5px;color:var(--text);text-decoration:none;transition:border-color .15s}
.att:hover{border-color:var(--accent);color:var(--accent)}
.att .fn{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
#p-edit-trans{display:none;margin-top:8px;padding:9px 13px;background:var(--input-bg);border:1px dashed var(--border-2);border-radius:10px;font-size:12.2px;color:var(--text-2);line-height:1.65;white-space:pre-wrap;max-height:11vh;overflow-y:auto}
/* AI 回复打分小框 */
.rate-bar{display:none;align-items:center;gap:8px;margin-top:6px;font-size:11.5px;color:var(--text-3);flex-wrap:wrap}
.rate-bar.show{display:flex}
.rate-star{cursor:pointer;font-size:15px;filter:grayscale(1);opacity:.5;transition:all .1s}
.rate-star.on{filter:none;opacity:1}
.rate-bar input{flex:1;min-width:90px;background:var(--input-bg);border:1px solid var(--border);border-radius:7px;padding:4px 9px;font-size:11.5px;color:var(--text);outline:none}
/* 设置弹窗（提示词编辑 + 评分记录） */
.modal-mask{display:none;position:fixed;inset:0;background:rgba(0,0,0,.5);z-index:70;align-items:center;justify-content:center}
.modal-mask.show{display:flex}
.modal-box{background:var(--bg-2);border:1px solid var(--border);border-radius:14px;width:min(760px,94vw);max-height:88vh;display:flex;flex-direction:column;overflow:hidden;box-shadow:0 18px 60px rgba(0,0,0,.4)}
.modal-head{display:flex;align-items:center;gap:10px;padding:12px 16px;border-bottom:1px solid var(--border);flex-shrink:0}
.modal-tab{padding:5px 12px;border-radius:8px;font-size:12.5px;cursor:pointer;color:var(--text-2)}
.modal-tab.active{background:var(--accent);color:var(--on-accent);font-weight:600}
.modal-body{overflow-y:auto;padding:14px 16px}
.pf-field{margin-bottom:14px}
.pf-field label{display:block;font-size:12px;font-weight:600;color:var(--text-2);margin-bottom:5px}
.pf-field textarea{width:100%;background:var(--input-bg);border:1px solid var(--border);border-radius:9px;padding:9px 12px;font-size:12px;color:var(--text);line-height:1.6;resize:vertical;box-sizing:border-box;outline:none;font-family:inherit}
.pf-field textarea:focus{border-color:var(--accent)}
.rt-row{border:1px solid var(--border);border-radius:9px;padding:9px 11px;margin-bottom:8px;background:var(--card)}
.rt-row .rt-h{font-size:11.5px;color:var(--text-3);margin-bottom:4px}
.rt-row .rt-rp{font-size:12px;color:var(--text);line-height:1.5;white-space:pre-wrap;max-height:80px;overflow-y:auto}
.rt-row .rt-nt{font-size:11.5px;color:var(--c-amber);margin-top:4px}
.rv-trans{display:none;margin-top:6px;padding:8px 10px;background:var(--bg);border:1px dashed var(--border);border-radius:8px;font-size:12px;color:var(--text-2);line-height:1.6;white-space:pre-wrap}
.rv-ctx{margin:4px 0 9px;padding:9px 11px;background:var(--bg);border:1px solid var(--border);border-left:3px solid var(--accent);border-radius:8px;max-height:240px;overflow-y:auto}
.rv-ctx-h{font-size:11px;font-weight:600;color:var(--accent);margin-bottom:7px}
.rv-ctx-m{margin-bottom:9px;padding-bottom:9px;border-bottom:1px solid var(--border)}
.rv-ctx-m:last-child{margin-bottom:0;padding-bottom:0;border-bottom:none}
.rv-ctx-d{font-size:10px;color:var(--text-3);margin-bottom:3px}
.rv-ctx-t{font-size:12px;color:var(--text);line-height:1.6;white-space:pre-wrap;word-break:break-word}
.rv-ctx-tr{font-size:11.5px;color:var(--text-2);line-height:1.55;white-space:pre-wrap;margin-top:4px}
.rv-ctx a,.rv-ctx-tr a{color:var(--accent);word-break:break-all}
.t-btn{padding:4px 11px;border-radius:7px;font-size:12px;font-weight:500;cursor:pointer;border:1px solid var(--border);background:transparent;color:var(--text-2);transition:all .15s}
.t-btn:hover{color:var(--text)}
.t-btn.active{background:var(--accent);color:var(--on-accent);border-color:var(--accent);font-weight:600}
/* 微信式发消息框：工具线 + 单框 + 生成/发送 */
.chat-tools{display:flex;align-items:center;gap:6px;margin-bottom:7px;flex-wrap:wrap}
.chat-input{display:flex;gap:8px;align-items:flex-end}
.chat-btns{display:flex;flex-direction:column;gap:6px;flex-shrink:0}
.chat-btns .btn{padding:7px 14px}
.p-edit{flex:1;min-height:52px;max-height:24vh;background:var(--input-bg);border:1px solid var(--border);border-radius:12px;padding:10px 13px;font-size:12.8px;color:var(--text);line-height:1.65;resize:none;font-family:inherit;outline:none;box-sizing:border-box}
.p-edit:focus{border-color:var(--accent)}
.pane select{background:var(--input-bg);color:var(--text);border:1px solid var(--border);border-radius:8px;padding:6px 10px;font-size:12.5px;width:100%;outline:none}
.pane select.eng-sel{width:auto;padding:3px 8px;font-size:11.5px;border-radius:7px;color:var(--text-2);font-weight:400}
.checklab{display:inline-flex;align-items:center;gap:6px;font-size:12px;color:var(--text-2);cursor:pointer;user-select:none}
.checklab input{accent-color:var(--accent);width:14px;height:14px}
.hist{font-size:11px;color:var(--text-3);padding:1px 0 1px 10px;border-left:2px solid var(--border-2)}
.empty{color:var(--text-3);text-align:center;padding:60px 0;font-size:13px}
.alert{padding:5px 12px;border-radius:0;border-left:3px solid var(--c-amber);font-size:11.5px;background:transparent;color:var(--text-2)}

#preload-bar{display:none;align-items:center;gap:10px;padding:6px 16px;font-size:12px;background:color-mix(in srgb,var(--accent) 14%,var(--bg));color:var(--accent);border-bottom:1px solid var(--border);flex-shrink:0}
#preload-bar.show{display:flex}
.spin{animation:spin .9s linear infinite;display:inline-block}
@keyframes spin{to{transform:rotate(360deg)}}
/* 自适应：窄窗口下各栏默认收缩（手动拖过的以拖拽值为准） */
@media (max-width:1250px){ .sidebar{width:178px} }
@media (max-width:1050px){ .maillist{width:320px} }
@media (max-width:900px){ .sidebar{width:158px} .maillist{width:280px} #sync-info{display:none} .pane-main{padding:14px 18px} }
</style>
</head>
<body>

<div class="sidebar">
  <div class="sb-brand">📮 邮件回复助手</div>
  <input id="search" placeholder="🔍 搜索邮箱 / 名字 / 主题 / 对话内容" oninput="onSearchInput()">
  <div class="sb-list">
    <div class="sb-item active" onclick="setFilter(this,'all')">📥 全部<span class="cnt" id="c-all"></span></div>
    <div class="sb-item" onclick="setFilter(this,'unread')">● 未读<span class="cnt" id="c-unread"></span></div>
    <div class="sb-item" onclick="setFilter(this,'pending')">⏳ 待处理<span class="cnt" id="c-pending"></span></div>
    <div class="sb-item" onclick="setFilter(this,'replied')">✅ 已回复<span class="cnt" id="c-replied"></span></div>
    <div class="sb-item" onclick="setFilter(this,'sent')">🚀 已发送<span class="cnt" id="c-sent"></span></div>
    <div class="sb-item" onclick="setFilter(this,'drafted')">📝 草稿<span class="cnt" id="c-drafted"></span></div>
    <div class="sb-item" onclick="setFilter(this,'not_suitable')">❌ 不合适<span class="cnt" id="c-ns"></span></div>
    <div class="sb-sec">智能分组</div>
    <div class="sb-item" onclick="setFilter(this,'reviewing')" title="飞书状态=需审核：已提交内部/领导审核，正在等结果，此时不用回对方。领导审完把状态改掉后，对方未回的会自动回到「待处理」">⏳ 审核中<span class="cnt" id="c-reviewing"></span></div>
    <div class="sb-item" onclick="setFilter(this,'round2')" title="我回过、对方又回我了的待处理邮件——全选后用 AI 批量生成（可配批量指示），逐人按完整往来定制">🔁 二轮待回<span class="cnt" id="c-round2"></span></div>
    <div class="sb-item" onclick="setFilter(this,'coop')" title="飞书联系状态 = 达成合作 / 推进制作">🤝 已合作<span class="cnt" id="c-coop"></span></div>
    <div class="sb-item" onclick="setFilter(this,'followup')" title="我回完之后，对方满 24 小时没动静的——点开后用「⏰ 跟进」快捷指令生成提醒（注：对方回了等我处理的在「待处理」）">⏰ 待跟进<span class="cnt" id="c-followup"></span></div>
    <div class="sb-item" onclick="setFilter(this,'farewell')" title="飞书表里被标 不合适/无法合作、但不是我自己在工具里标的（≈领导判定）、且还没发告别信的——勾选后用「告别批量」按对方语种生成礼貌收尾。我自己标的不合适不进这里">👋 待告别<span class="cnt" id="c-farewell"></span></div>
    <div class="sb-sec">平台</div>
    <div class="sb-item" onclick="setFilter(this,'github')">GitHub<span class="cnt" id="c-github"></span></div>
    <div class="sb-item" onclick="setFilter(this,'youtube')">YouTube<span class="cnt" id="c-youtube"></span></div>
    <div class="sb-item" onclick="setFilter(this,'other')">其他<span class="cnt" id="c-other"></span></div>
    <div class="sb-item" onclick="setFilter(this,'notice')" title="noreply / no-reply 类自动邮件（平台通知、付款/发票确认等）——单独归这里，不进待处理/全部">🔔 通知<span class="cnt" id="c-notice"></span></div>
  </div>
  <div class="sb-foot">
    <button class="btn btn-send btn-sm" id="pending-btn" onclick="openReview()" style="display:none;width:100%;justify-content:center">📬 待发送审核 (0)</button>
    <div class="sb-tools">
      <span id="sync-info"></span>
      <span style="margin-left:auto;display:inline-flex;gap:6px">
        <button class="icon-btn" onclick="openStats()" title="按联系状态完整统计（读飞书全表）">📊</button>
        <button class="icon-btn" onclick="openSettings()" title="设置：改 AI 提示词 / 看回复评分">⚙️</button>
        <button class="icon-btn" id="theme-btn" onclick="toggleTheme()" title="切换主题">🌙</button>
        <button class="icon-btn" onclick="loadReplies(true)" title="刷新（含飞书已发送同步）"><span id="ri">⟳</span></button>
      </span>
    </div>
  </div>
</div>

<div class="resizer" id="sb-resizer" title="拖拽调整侧栏宽度 · 双击恢复默认"></div>

<div class="main">

<div id="preload-bar">
  <span class="spin">⟳</span>
  <span id="preload-text">加载中...</span>
  <span id="preload-pct" style="margin-left:auto;font-family:ui-monospace,monospace"></span>
</div>

<div class="batchbar" id="batchbar">
  <button class="btn btn-soft btn-sm" onclick="selectAllPending()">全选</button>
  <button class="btn btn-soft btn-sm" onclick="clearSelection()">清空</button>
  <span class="tsep"></span>
  <input id="batch-instr" placeholder="批量指示（可选），如：都按对方报价砍 20%（每封仍按各自往来定制）"
    style="flex:1;min-width:130px;max-width:460px;background:var(--card);border:1px solid var(--border);border-radius:7px;padding:4px 10px;font-size:12px;color:var(--text);outline:none">
  <button class="btn btn-primary btn-sm" id="batch-ai-btn" onclick="batchCompose()" disabled>🤖 AI 批量生成 (0)</button>
  <button class="btn btn-soft btn-sm" id="batch-fw-btn" onclick="batchFarewell()" disabled title="给飞书标了不合适的博主生成礼貌收尾邮件：说明价格未谈拢、致谢、留未来合作口子；按对方语种写">👋 告别批量 (0)</button>
  <span id="batch-progress"></span>
  <select id="batch-lang" style="display:none"><option value="en">EN</option></select>
  <select id="batch-type" style="display:none"><option value="generic">generic</option></select>
  <button id="batch-btn" style="display:none"></button>
</div>

<div class="split">
  <div class="maillist" id="maillist"></div>
  <div class="resizer" id="resizer"></div>
  <div class="pane" id="pane">
    <div class="pane-empty" id="pane-empty">
      <div style="font-size:40px">📬</div>
      <div>点击左侧邮件，在这里查看和回复</div>
      <div style="font-size:11.5px;color:var(--text-3)">勾选多封 → 顶部批量生成 → 待发送审核里过目后发出 · 在飞书手动回复后点刷新会自动标记</div>
    </div>
    <div class="pane-main" id="pane-main" style="display:none">

      <div class="p-head">
        <div class="p-head-info">
          <div class="p-sender" id="p-sender"></div>
          <div class="p-addr" id="p-addr" title="点击全选，可直接复制"></div>
        </div>
        <div class="p-quick" id="p-quick"></div>
      </div>

      <div class="f-card" id="f-card"></div>

      <div class="sec sec-in">
        <div class="sec-title">💬 完整往来
          <span id="p-date" style="font-weight:400;color:var(--text-3)"></span>
          <button class="t-btn" id="conv-trans-btn" onclick="toggleConvTrans()" style="margin-left:auto" title="把整段往来翻译成中文（再点切回原文）">🌐 翻译</button>
        </div>
        <div class="conv" id="p-incoming">加载中...</div>
      </div>

      <div class="sec sec-re">
        <div class="chat-tools">
          <select id="p-engine" class="eng-sel" onchange="localStorage.setItem('bloome-engine',this.value)" title="生成引擎（批量也用这里选的）">
            <option value="deepseek">⚡ DeepSeek</option>
            <option value="claude">Claude</option>
            <option value="codex">Codex</option>
          </select>
          <label class="checklab" title="开 = 30~60 词直奔主题；关 = 80~150 词标准长度。批量/告别也用这个开关">
            <input type="checkbox" id="p-short" onchange="localStorage.setItem('bloome-short',this.checked?'1':'0')">✂️
          </label>
          <span class="tsep"></span>
          <button class="t-btn" onclick="quickInstr('同意对方的条件，推进合作')">✅ 同意</button>
          <button class="t-btn" onclick="quickInstr('价格太高了，礼貌地往下砍价')">💰 砍价</button>
          <button class="t-btn" onclick="quickInstr('婉拒这次合作，保持友好，留下以后合作的可能')">🙏 婉拒</button>
          <button class="t-btn" onclick="quickInstr('询问对方的报价和具体合作形式')">❓ 问价</button>
          <button class="t-btn" onclick="quickInstr('对方几天没有回复了，写一封轻量友好的跟进提醒：换一个角度补充一个具体的行动点或低门槛的下一步，绝不重复之前说过的话，不催促不施压')">⏰ 跟进</button>
          <span style="margin-left:auto;display:inline-flex;gap:8px;align-items:center">
            <button class="t-btn" onclick="openPayment()" title="AI 从对话预填付款申请，照着去飞书「运营推广付款申请」提交">💰 付款申请</button>
            <button class="t-btn" id="p-copy" onclick="copyReply()" title="复制正文，去飞书手动粘贴发送">📋</button>
            <button class="t-btn" onclick="document.getElementById('p-img-file').click()" title="上传图片随这封回复一起发出（也可直接在正文框里粘贴图片）">🖼️ 图片</button>
            <input type="file" id="p-img-file" accept="image/*" multiple style="display:none" onchange="attachImages(this.files); this.value=''">
            <label class="checklab" title="发送时在邮件末尾附 Bloome 介绍图"><input type="checkbox" id="p-img">官方图</label>
          </span>
        </div>
        <div id="p-imgs" class="reply-imgs"></div>
        <div class="chat-input">
          <textarea class="p-edit" id="p-edit" oninput="fitReply()" rows="2"
            onpaste="onReplyPaste(event)"
            onkeydown="if((event.metaKey||event.ctrlKey)&&event.key==='Enter'){event.preventDefault();approveSend();}"
            placeholder="像微信一样：输入中文指示（如：答应150/月）点「生成」，AI 写好英文稿就地替换；也可直接手写正文。⌘+回车 = 发送"></textarea>
          <div class="chat-btns">
            <button class="btn btn-primary" id="p-gen" onclick="composeReply()" title="把框里的中文当指示，AI 读完整往来后写英文稿（留空 = AI 自行判断）">🤖 生成</button>
            <button class="btn btn-send" id="p-approve" onclick="approveSend()" title="把框里的内容直接发出（⌘+Enter）">🚀 发送</button>
          </div>
        </div>
        <div id="p-edit-trans"></div>
        <div id="p-status" style="font-size:11.5px;color:var(--text-2);margin-top:5px"></div>
        <div class="rate-bar" id="rate-bar">
          <span>这条 AI 稿质量：</span>
          <span class="rate-star" data-s="1" onclick="setRate(1)">⭐</span>
          <span class="rate-star" data-s="2" onclick="setRate(2)">⭐</span>
          <span class="rate-star" data-s="3" onclick="setRate(3)">⭐</span>
          <span class="rate-star" data-s="4" onclick="setRate(4)">⭐</span>
          <span class="rate-star" data-s="5" onclick="setRate(5)">⭐</span>
          <input id="rate-note" placeholder="备注哪里不好（可选），存起来后续优化" onkeydown="if(event.key==='Enter')saveRate()">
          <button class="t-btn" onclick="saveRate()">存评分</button>
        </div>
      </div>

    </div>
  </div>
  <div class="review-dock" id="review-dock">
    <div class="rv-head">📬 待发送 <span id="rv-count" style="color:var(--text-3);font-weight:400"></span>
      <span style="margin-left:auto;display:inline-flex;gap:6px">
        <button class="btn btn-send btn-sm" id="rv-sendall-btn" onclick="rvSendAll()" title="不逐条审、把队列里全部直接发出">全部发送</button>
        <button class="btn btn-soft btn-sm" onclick="closeReview()">✕</button>
      </span>
    </div>
    <div class="rv-tip">点一条 → 中间跳到与该博主的完整对话，草稿自动进下方回复框，看着上下文改了再「🚀 发送」。发出即从队列移除。</div>
    <div class="rv-list" id="rv-list"></div>
  </div>
</div>

</div><!-- /.main -->

<div class="modal-mask" id="settings-mask" onclick="if(event.target===this)closeSettings()">
  <div class="modal-box">
    <div class="modal-head">
      <b style="font-size:14px">⚙️ 设置</b>
      <span class="modal-tab active" id="tab-prompts" onclick="switchSettingsTab('prompts')">AI 提示词</span>
      <span class="modal-tab" id="tab-ratings" onclick="switchSettingsTab('ratings')">回复评分记录</span>
      <span style="margin-left:auto;display:inline-flex;gap:7px">
        <button class="btn btn-primary btn-sm" id="prompts-save-btn" onclick="savePrompts()">保存提示词</button>
        <button class="btn btn-soft btn-sm" onclick="closeSettings()">✕ 关闭</button>
      </span>
    </div>
    <div class="modal-body" id="settings-body"></div>
  </div>
</div>

<div class="modal-mask" id="stats-mask" onclick="if(event.target===this)closeStats()">
  <div class="modal-box" style="width:min(460px,94vw)">
    <div class="modal-head">
      <b style="font-size:14px">📊 联系状态统计</b>
      <span id="stats-total" style="color:var(--text-3);font-size:12px"></span>
      <span style="margin-left:auto;display:inline-flex;gap:7px">
        <button class="btn btn-soft btn-sm" onclick="openStats()" title="重新读飞书全表">🔄 刷新</button>
        <button class="btn btn-soft btn-sm" onclick="closeStats()">✕ 关闭</button>
      </span>
    </div>
    <div class="modal-body" id="stats-body"></div>
  </div>
</div>

<div class="modal-mask" id="payment-mask" onclick="if(event.target===this)closePayment()">
  <div class="modal-box">
    <div class="modal-head">
      <b style="font-size:14px">💰 付款申请预填</b>
      <span style="margin-left:auto;display:inline-flex;gap:7px">
        <button class="btn btn-primary btn-sm" onclick="copyPaymentAll()">📋 复制全部</button>
        <button class="btn btn-soft btn-sm" onclick="closePayment()">✕ 关闭</button>
      </span>
    </div>
    <div class="rv-tip">AI 已从对话自动预填，逐项核对/补充后照着去飞书「运营推广付款申请」填。发票 PDF 和资源附件在飞书里上传。规则字段（日期=今天、类别=KOL推广、主体按付款方式）已自动确定。</div>
    <div class="modal-body" id="payment-body"></div>
  </div>
</div>

<script>
/* ── 主题 ── */
function applyTheme(t){
  document.documentElement.setAttribute('data-theme', t);
  document.getElementById('theme-btn').textContent = t==='dark' ? '🌙' : '☀️';
  localStorage.setItem('bloome-theme', t);
}
function toggleTheme(){
  applyTheme(document.documentElement.getAttribute('data-theme')==='dark' ? 'light' : 'dark');
}
applyTheme(new URLSearchParams(location.search).get('theme') || localStorage.getItem('bloome-theme') || 'dark');

/* ── 全局状态 ── */
let allItems = [], currentFilter = 'all';
let STATUS_OPTIONS = [];   // 联系状态选项 [{name,color}]，来自飞书该字段（/api/replies 灌入）
function statusColorOf(name){ const o=STATUS_OPTIONS.find(x=>x.name===name); return o&&o.color; }
let _selMid = '';
let _type='', _lang='en';
let _selected = new Set();
const TYPE_LABELS = {readme:'📦 README 合作', direct_ad:'📢 直接广告', pricing:'💰 询价', generic:'💬 通用'};
const ACTION_TXT  = {sent:'已发送', drafted:'草稿待发', replied:'已回复', not_suitable:'不合适', ignored:'已忽略'};

/* ── 数据 ── */
async function loadReplies(force=false){
  const ri = document.getElementById('ri');
  ri.classList.add('spin');
  try{
    const res = await fetch('/api/replies'+(force?'?refresh=1':''));
    const d = await res.json();
    window.__refreshing = !!d.refreshing;
    allItems = d.items||[];
    if(d.status_options && d.status_options.length) STATUS_OPTIONS = d.status_options;
    updateStats(d.stats||{});
    if(d.auto_sync && d.auto_sync.last){
      const si = document.getElementById('sync-info');
      si.textContent = `表格已同步 ${(d.auto_sync.last||'').slice(5)}`;
      si.title = `每小时自动同步飞书表 · 上次 ${d.auto_sync.last} · 本次推进 ${d.auto_sync.last_updated} 条`;
    }
    renderList();
    if(_selMid){
      const cur = allItems.find(i=>i.message_id===_selMid);
      if(!cur) closePane();
      else openItemHeaderRefresh(cur);
    }
    // ?open=done：自动打开第一封已处理邮件（截图/调试用，不碰未读）
    if(!_selMid && new URLSearchParams(location.search).get('open')==='done'){
      const it = allItems.find(i=>i.action);
      if(it) openItem(it.message_id);
    }
    // 摘要覆盖率低（线程缓存刚被刷新清掉）→ 50 秒后自动再取一次，等后台回热
    if(allItems.length && allItems.filter(i=>i.snippet).length < allItems.length*0.5 && !window.__snipRetry){
      window.__snipRetry = true;
      setTimeout(()=>{ loadReplies(false); window.__snipRetry = false; }, 50000);
    }
  }catch(e){
    document.getElementById('maillist').innerHTML='<p class="empty">加载失败</p>';
  }finally{
    // stale-while-revalidate：旧数据已秒出；后台还在重建就保持转圈、5 秒后再取一次换新
    if(window.__refreshing){
      ri.classList.add('spin');
      if(!window.__rfPoll){
        window.__rfPoll = true;
        setTimeout(()=>{ window.__rfPoll=false; loadReplies(false); }, 5000);
      }
    } else ri.classList.remove('spin');
  }
}
function updateStats(s){
  const set=(id,v)=>{const el=document.getElementById(id); if(el) el.textContent = v??0;};
  const nn = allItems.filter(i=>!i.is_notice);   // 全部/未读/平台等计数都不含通知，与列表一致
  set('c-all', nn.length); set('c-unread', nn.filter(i=>i.unread&&!i.action).length);
  set('c-notice', allItems.filter(i=>i.is_notice).length);
  set('c-pending', allItems.filter(needsReply).length);   // 排除需审核/寒暄收尾/通知后的真待处理
  set('c-sent',s.sent); set('c-drafted',s.drafted); set('c-replied',s.replied); set('c-ns',s.not_suitable);
  set('c-github', nn.filter(i=>i.platform==='github').length);
  set('c-youtube',nn.filter(i=>i.platform==='youtube').length);
  set('c-other',  nn.filter(i=>i.platform!=='github'&&i.platform!=='youtube').length);
  set('c-coop',   allItems.filter(i=>/达成合作|推进制作/.test(i.feishu_status||'')).length);
  set('c-farewell',allItems.filter(i=>/不合适|无法合作/.test(i.feishu_status||'') && i.action!=='not_suitable' && !i.farewell_done).length);
  set('c-followup',allItems.filter(i=>i.followup).length);
  set('c-reviewing',allItems.filter(isReviewing).length);
  set('c-round2', allItems.filter(i=>needsReply(i) && i.round2).length);
}
function calcStats(){
  return {total:allItems.length,
    unread:allItems.filter(i=>i.unread&&!i.action).length,
    pending:allItems.filter(i=>!i.action).length,
    sent:allItems.filter(i=>i.action==='sent').length,
    drafted:allItems.filter(i=>i.action==='drafted').length,
    replied:allItems.filter(i=>i.action==='replied').length,
    not_suitable:allItems.filter(i=>i.action==='not_suitable').length};
}

/* ── 列表 ── */
function setFilter(btn,f){
  currentFilter=f;
  document.querySelectorAll('.sb-item').forEach(b=>b.classList.remove('active'));
  btn.classList.add('active');
  renderList();
}
// 真正需要我回的：没处理过 + 不在等领导审核 + 不是"我回过后对方只是寒暄收尾"
function needsReply(i){
  if(i.is_notice) return false;                               // noreply 类通知不用回
  if(i.action) return false;
  if(/需审核/.test(i.feishu_status||'')) return false;        // 等领导审核，不用回（→审核中）
  if((i.intent||{}).label==='寒暄' && i.round2) return false; // 我回过、对方只是 thanks 收尾
  return true;
}
function isReviewing(i){ return !i.action && /需审核/.test(i.feishu_status||''); }
function filteredItems(){
  const f = currentFilter;
  // noreply 类通知只在「🔔 通知」分组出现，其余所有视图（含全部）都排除，避免淹没主区
  let items = (f==='notice') ? allItems.filter(i=>i.is_notice) : allItems.filter(i=>!i.is_notice);
  if(f==='notice')        items=[...items];
  else if(f==='unread')   items=items.filter(i=>i.unread&&!i.action);
  else if(f==='pending')  items=items.filter(needsReply);
  else if(f==='github')   items=items.filter(i=>i.platform==='github');
  else if(f==='youtube')  items=items.filter(i=>i.platform==='youtube');
  else if(f==='other')    items=items.filter(i=>i.platform!=='github'&&i.platform!=='youtube');
  else if(f==='sent')     items=items.filter(i=>i.action==='sent');
  else if(f==='drafted')  items=items.filter(i=>i.action==='drafted');
  else if(f==='replied')  items=items.filter(i=>i.action==='replied');
  else if(f==='not_suitable')items=items.filter(i=>i.action==='not_suitable');
  else if(f==='coop')     items=items.filter(i=>/达成合作|推进制作/.test(i.feishu_status||''));
  else if(f==='farewell') items=items.filter(i=>/不合适|无法合作/.test(i.feishu_status||'') && i.action!=='not_suitable' && !i.farewell_done);
  else if(f==='followup') items=items.filter(i=>i.followup);
  else if(f==='reviewing')items=items.filter(isReviewing);
  else if(f==='round2')   items=items.filter(i=>needsReply(i) && i.round2);
  const q = (document.getElementById('search')?.value||'').trim().toLowerCase();
  if(q){
    const useContent = (_contentQ===q);   // 后端内容命中只在 query 一致时采用，避免输入过程中串味
    items = items.filter(i =>
      i.email.toLowerCase().includes(q) ||
      (i.from_raw||'').toLowerCase().includes(q) ||
      (i.subject||'').toLowerCase().includes(q) ||
      (i.snippet||'').toLowerCase().includes(q) ||
      (useContent && _contentHits.has(i.message_id)));
  }
  // 当前打开的会话永远保留在列表里——回复后状态变了也不从当前筛选里消失（微信式连续感）
  if(_selMid && !items.some(i=>i.message_id===_selMid)){
    const cur = allItems.find(i=>i.message_id===_selMid);
    if(cur) items = [...items, cur];
  }
  // 排序：「全部」= 纯按对方最新邮件时间（微信会话式，不随我回复与否跳位置）；
  // 待处理类分组 = 意图优先级（同意>报价>提问>待回应>寒暄>拒绝）再按时间
  if(f==='all'){
    items = [...items].sort((a,b)=>(b.date||'').localeCompare(a.date||''));
  } else {
    const W={'同意':0,'报价':1,'提问':2,'待回应':3,'寒暄':4,'拒绝':5};
    items = [...items].sort((a,b)=>{
      const ap=a.action?1:0, bp=b.action?1:0;
      if(ap!==bp) return ap-bp;
      if(!ap){
        const aw=W[(a.intent||{}).label]??3, bw=W[(b.intent||{}).label]??3;
        if(aw!==bw) return aw-bw;
      }
      return (b.date||'').localeCompare(a.date||'');
    });
  }
  return items;
}
function intentBadge(it){
  const i=it.intent;
  if(!i || !i.label || it.action) return '';
  const map={'同意':['var(--c-green)','🟢 同意'],
             '报价':['var(--c-amber)','💰 '+(i.price||'报价')],
             '提问':['var(--c-blue)','❓ 提问'],
             '拒绝':['var(--c-red)','🔴 拒绝'],
             '待回应':['var(--c-indigo)','⏳ 待回应'],
             '寒暄':['var(--c-gray)','寒暄']};
  const m=map[i.label];
  return m ? `<span class="badge" style="color:${m[0]}">${escHTML(m[1])}</span>` : '';
}
function platformBadge(p, matched){
  if(p==='youtube') return '<span class="badge b-yt">YouTube</span>';
  if(p==='github')  return '<span class="badge b-gh">GitHub</span>';
  return '<span class="badge b-other">'+(matched?'其他':'未入库')+'</span>';
}
function actionBadge(a){
  if(a==='sent')         return '<span class="badge b-sent">已发送</span>';
  if(a==='drafted')      return '<span class="badge b-drafted">草稿</span>';
  if(a==='replied')      return '<span class="badge b-replied">已回复</span>';
  if(a==='not_suitable') return '<span class="badge b-ns">不合适</span>';
  if(a==='ignored')      return '<span class="badge" style="color:var(--c-gray)">🙈 已忽略</span>';
  return '<span class="badge b-pending">待处理</span>';
}
function escHTML(s){return (s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}
function attIcon(ct,fn){
  const s=(ct+' '+fn).toLowerCase();
  if(s.includes('pdf'))return '📄'; if(/image|png|jpe?g|gif|webp/.test(s))return '🖼️';
  if(/zip|rar|7z/.test(s))return '🗜️'; if(/word|doc/.test(s))return '📝';
  if(/sheet|excel|xls|csv/.test(s))return '📊'; if(/video|mp4|mov/.test(s))return '🎬';
  return '📎';
}
function attsHTML(mid, atts){
  if(!atts || !atts.length || !mid) return '';
  return '<div class="atts">'+atts.map(a=>
    `<a class="att" href="/api/attachment?mid=${encodeURIComponent(mid)}&aid=${encodeURIComponent(a.id)}" target="_blank" rel="noopener" title="${escHTML(a.filename)}（点击下载/预览）">`
    +`<span>${attIcon(a.content_type,a.filename)}</span><span class="fn">${escHTML(a.filename)}</span></a>`
  ).join('')+'</div>';
}
function linkify(escaped){
  // ① [文字](url) → 文字标签式链接（长链接只显示文字）；② 裸 URL → 可点击（尾部标点不吞）
  let s = (escaped||'').replace(/\[([^\]\n]{1,90})\]\((https?:[^)\s]{1,800})\)/g,
    (_,label,url)=>`<a href="${url}" target="_blank" rel="noopener">${label} ↗</a>`);
  return s.replace(/(^|[^"'>])(https?:\/\/[^\s<>"']+)/g, (m,pre,u)=>{
    const t = u.match(/[).,;:!?]+$/);
    const url = t ? u.slice(0, -t[0].length) : u;
    return `${pre}<a href="${url}" target="_blank" rel="noopener">${url}</a>` + (t?t[0]:'');
  });
}
const AV_COLORS=['#6d7cff','#d8618c','#3aa3cf','#46b58a','#c79a3e','#9a6de0','#5fae5f','#cf736a'];
function avatarHTML(item){
  const name=((item.from_raw||'').split('<')[0].trim()||item.email||'?');
  const ch=((name.replace(/[^A-Za-z0-9一-鿿]/g,'')[0])||'?').toUpperCase();
  let h=0; for(const c of (item.email||'')) h=(h*31+c.charCodeAt(0))>>>0;
  return `<span class="avatar" style="background:${AV_COLORS[h%AV_COLORS.length]}">${escHTML(ch)}</span>`;
}

// 一键翻译：打开邮件不再自动翻，点按钮把整段往来译成中文（命中缓存即时），再点切回原文
let _convDlg = [], _convMid = '', _convTransOn = false, _convTransLoaded = false;
function toggleConvTrans(){
  const btn = document.getElementById('conv-trans-btn');
  if(!_convDlg.length){ return; }
  _convTransOn = !_convTransOn;
  const blocks = document.querySelectorAll('#p-incoming .qtrans');
  if(!_convTransOn){                                  // 切回原文
    blocks.forEach(el=>el.style.display='none');
    btn.textContent = '🌐 翻译';
    return;
  }
  if(_convTransLoaded){                               // 已翻过，直接显示
    blocks.forEach(el=>{ if(el.innerHTML) el.style.display='block'; });
    btn.textContent = '🌐 原文';
    return;
  }
  const mid = _convMid;
  btn.textContent = '🌐 翻译中…';
  fetch('/api/translate-texts',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({texts: _convDlg.map(m=>m.text)})}).then(r=>r.json()).then(t=>{
    if(_selMid!==mid){ return; }
    if(!t.ok){ btn.textContent='🌐 翻译'; _convTransOn=false; return; }
    const inc = document.getElementById('p-incoming');
    const atBottom = inc.scrollHeight - inc.scrollTop - inc.clientHeight < 60;
    (t.trans||[]).forEach((tr,i)=>{
      const el = document.getElementById('qt-'+i);
      if(el && tr){ el.innerHTML = linkify(escHTML(tr)); el.style.display='block'; }
    });
    if(atBottom) inc.scrollTop = inc.scrollHeight;
    _convTransLoaded = true;
    btn.textContent = '🌐 原文';
  }).catch(()=>{ btn.textContent='🌐 翻译'; _convTransOn=false; });
}

// 按对话内容搜索：本地字段先即时过滤，后端再去翻完整往来正文，命中合并进来
let _contentHits = new Set(), _contentQ = '', _searchTimer = null;
function onSearchInput(){
  renderList();   // 本地（邮箱/名字/主题/摘要）即时过滤
  clearTimeout(_searchTimer);
  const q = (document.getElementById('search').value||'').trim().toLowerCase();
  if(q.length < 2){ _contentHits = new Set(); _contentQ = ''; return; }
  _searchTimer = setTimeout(async ()=>{
    try{
      const r = await fetch('/api/search?q='+encodeURIComponent(q));
      const d = await r.json();
      if(d.ok && d.q===q){ _contentHits = new Set(d.mids||[]); _contentQ = q; renderList(); }
    }catch(e){}
  }, 250);
}

function renderList(){
  const items = filteredItems();
  const el = document.getElementById('maillist');
  if(!items.length){ el.innerHTML='<p class="empty">📭 当前筛选无邮件</p>'; return; }
  el.innerHTML = items.map(item=>{
    const showUnread = item.unread && !item.action;
    const senderName = escHTML((item.from_raw||'').split('<')[0].trim() || item.email);
    const checkbox = `<input type="checkbox" ${_selected.has(item.message_id)?'checked':''} onclick="event.stopPropagation()" onchange="toggleSelect('${item.message_id}',this.checked)">`;
    const cls = ['mrow',
      item.message_id===_selMid?'sel':'',
      showUnread?'is-unread':'',
      item.action==='not_suitable'?'dim':''].filter(Boolean).join(' ');
    const ib = intentBadge(item);
    const fw = item.followup ? ' <span class="badge" style="color:var(--c-indigo)">⏰ 待跟进</span>' : '';
    const cp = /达成合作|推进制作/.test(item.feishu_status||'') ? ' <span class="badge" style="color:var(--c-green)">🤝 合作</span>' : '';
    return `
<div class="${cls}" onclick="openItem('${item.message_id}')">
  ${checkbox}
  <div class="mrow-main">
    <div class="mrow-1">
      ${showUnread?'<span class="dot"></span>':''}
      <span class="mname">${senderName}</span>
      <span class="mtime">${item.date.slice(5)}</span>
    </div>
    <div class="mrow-3">${ib}${actionBadge(item.action)} ${platformBadge(item.platform,item.matched)}${cp}${fw}</div>
    <div class="msub">${escHTML(item.snippet||item.subject||'')}</div>
  </div>
</div>`;
  }).join('');
}

/* ── 右面板 ── */
function closePane(){
  _selMid='';
  document.getElementById('pane-empty').style.display='flex';
  document.getElementById('pane-main').style.display='none';
}
function curItem(){ return allItems.find(i=>i.message_id===_selMid); }

async function openItem(mid){
  const item = allItems.find(i=>i.message_id===mid);
  if(!item) return;
  if(_noteEditing !== mid){ _noteEditing=''; _noteDraft=null; }   // 切人即退出上一条的备注编辑
  _selMid = mid; _lang='en';
  renderList();

  document.getElementById('pane-empty').style.display='none';
  document.getElementById('pane-main').style.display='flex';
  document.getElementById('pane').scrollTop = 0;

  openItemHeaderRefresh(item);
  document.getElementById('p-addr').textContent = item.email;
  renderFCard(item);
  refreshQuick(item);

  document.getElementById('p-date').textContent='';
  document.getElementById('p-incoming').innerHTML='<span style="color:var(--text-3);font-size:12px">加载中...</span>';
  _convDlg=[]; _convMid=''; _convTransOn=false; _convTransLoaded=false;   // 重置上一封的翻译状态
  { const tb=document.getElementById('conv-trans-btn'); if(tb) tb.textContent='🌐 翻译'; }

  resetReplyBox();
  document.getElementById('p-gen').disabled=false;
  document.getElementById('p-gen').textContent='🤖 生成';
  document.getElementById('p-status').textContent='';
  const ap = document.getElementById('p-approve');
  ap.disabled=false;
  ap.textContent = '🚀 发送';
  document.getElementById('p-copy').textContent='📋';

  try{
    const res = await fetch('/api/email/'+encodeURIComponent(mid));
    const d = await res.json();
    if(_selMid!==mid) return;
    if(d.ok){
      const dlg = d.dialog||[];
      const inc = document.getElementById('p-incoming');
      if(dlg.length){
        // 微信式聊天：时间正序、最新在底部、自动滚到底（回复框就在下方，像聊天输入框）
        inc.innerHTML = dlg.map((m,i)=>`
          <div class="brow ${m.role==='我'?'me':'peer'}">
            <div class="bub">
              <div class="bub-t">${linkify(escHTML(m.text))}</div>
              <div class="qtrans" id="qt-${i}"></div>
              ${attsHTML(m.mid, m.atts)}
              <div class="bub-meta">${m.role==='我'?'我':'对方'} · ${escHTML(m.date)}</div>
            </div>
          </div>`).join('');
        inc.scrollTop = inc.scrollHeight;
        document.getElementById('p-date').textContent = `共 ${dlg.length} 条往来`;
        _convDlg = dlg; _convMid = mid;   // 不再自动翻译；点「🌐 翻译」按钮按需翻（已预热则瞬时）
      } else if((d.chain||[]).length){
        // 兜底：没聚合到对话时退回单封引用链
        const chain = d.chain;
        let quoted = '';
        for(let i=chain.length-1; i>=1; i--){
          quoted = `<div class="qlvl"><div class="qhdr">${escHTML(chain[i].header)}</div>`
                 + `<div class="qtxt">${linkify(escHTML(chain[i].text))}</div>${quoted}</div>`;
        }
        inc.innerHTML = `<div class="qmain">${linkify(escHTML(chain[0].text))}</div>` + quoted;
        inc.scrollTop = 0;
        document.getElementById('p-date').textContent = d.date||'';
      } else {
        inc.textContent = d.body || '（空）';
        document.getElementById('p-date').textContent = d.date||'';
      }
    } else {
      document.getElementById('p-incoming').textContent='读取失败';
    }
    if(item.unread){ item.unread=false; updateStats(calcStats()); renderList(); }
  }catch(e){
    document.getElementById('p-incoming').textContent='读取失败';
  }
}
function openItemHeaderRefresh(item){
  const senderName = (item.from_raw||'').split('<')[0].trim() || item.email;
  document.getElementById('p-sender').innerHTML =
    escHTML(senderName)+' &nbsp;'+platformBadge(item.platform,item.matched)+' '+actionBadge(item.action);
}
function refreshQuick(item){
  const mid = item.message_id;
  const quick = [];
  // 「不合适」走档案卡状态下拉、「已回复」靠飞书同步自动标——右上角不再放这俩冗余按钮
  if(item.action==='drafted' && item.draft_url)
    quick.push(`<a href="${item.draft_url}" target="_blank" class="btn btn-soft btn-sm">打开草稿</a>`);
  if(!item.action)   // 不想回的：纯本地标记移出待处理，不写飞书、随时可撤销
    quick.push(`<button class="btn btn-soft btn-sm" onclick="doMark('${mid}','ignored')" title="看过了不想回：移出待处理，不改飞书状态，可撤销">🙈 不回</button>`);
  if(item.action)
    quick.push(`<button class="btn btn-soft btn-sm" onclick="doMark('${mid}','pending')">↩ 撤销标记</button>`);
  document.getElementById('p-quick').innerHTML = quick.join('');
}

/* ── 飞书档案卡 ── */
function fStatusColor(s){
  if(!s) return 'var(--c-gray)';
  const c = statusColorOf(s);              // 优先用飞书该选项的真实颜色
  if(c) return c;
  if(s.includes('不合适')||s.includes('拒绝')||s.includes('无法')) return 'var(--c-red)';
  if(s.includes('沟通中')) return 'var(--c-amber)';
  if(s.includes('达成')||s.includes('上线')||s.includes('合作')) return 'var(--c-green)';
  if(s.includes('已联系')) return 'var(--c-blue)';
  return 'var(--c-gray)';
}
function renderFCard(item){
  const el = document.getElementById('f-card');
  const f  = item.feishu;
  const chips = [];
  if(item.action){   // 本工具的处理记录（hover 看历史）
    const hist=(item.history||[]).slice(-8).reverse()
      .map(h=>`${ACTION_TXT[h.action]||h.action} ${h.ts}`).join('\n');
    chips.push(`<span class="f-chip" title="${escHTML(hist)}" style="cursor:default"><span class="k">处理</span><b>${ACTION_TXT[item.action]||item.action}</b><span class="k">${(item.action_ts||'').slice(5)}</span></span>`);
  }
  if(!f){
    chips.push('<span class="f-empty">未入库 — 该发件人不在飞书表里</span>');
  } else {
    const optNames = STATUS_OPTIONS.length ? STATUS_OPTIONS.map(o=>o.name)
      : ['待联系','已联系','沟通中','无法合作','达成合作','不合适','需审核','待定','已上线','推进制作'];
    // 当前状态若不在选项里（飞书有但还没拉到/被改名）也保留，避免下拉里看不到当前值
    if(f.status && !optNames.includes(f.status)) optNames.push(f.status);
    const opts = optNames
      .map(o=>`<option value="${o}" ${o===f.status?'selected':''}>${o}</option>`).join('');
    chips.push(`<span class="f-chip"><span class="f-dot" style="background:${fStatusColor(f.status)}"></span><span class="k">状态</span>`
      + `<select class="f-status-sel" onchange="fcardStatus('${item.message_id}', this)" title="直接改飞书表的联系状态">${opts}</select>`
      + `${f.updated?`<span class="k">${escHTML(f.updated.slice(5))}</span>`:''}</span>`);
    if(f.tags)    chips.push(`<span class="f-chip"><span class="k">标签</span><b>${escHTML(f.tags)}</b></span>`);
    if(f.coop)    chips.push(`<span class="f-chip"><span class="k">合作</span>${escHTML(f.coop)}</span>`);
    if(f.lang)    chips.push(`<span class="f-chip"><span class="k">语种</span>${escHTML(f.lang)}</span>`);
    if(f.country) chips.push(`<span class="f-chip"><span class="k">国家</span>${escHTML(f.country)}</span>`);
    if(f.owner)   chips.push(`<span class="f-chip"><span class="k">负责人</span>${escHTML(f.owner)}</span>`);
    if(item.url)  chips.push(`<a href="${item.url}" target="_blank">${escHTML(f.channel||'主页')} ↗</a>`);
    chips.push(`<a href="${f.base_url}" target="_blank">飞书表 ↗</a>`);
    const ntRaw = (item.note||'').replace(/<br\s*\/?>/gi,' · ').replace(/\[([^\]]+)\]\([^)]+\)/g,'$1');
    chips.push(`<div class="f-note" id="f-note-view" title="点击编辑备注（写回飞书表）" onclick="fcardNoteEdit('${item.message_id}')" style="cursor:pointer">📝 ${ntRaw?escHTML(ntRaw):'<span style=\"color:var(--text-3)\">点击添加备注…</span>'}</div>`);
    chips.push(`<div class="f-note" id="f-note-edit" style="display:none">
      <input id="f-note-input" placeholder="备注（报价、合作形式等），回车保存"
        onkeydown="if(event.key==='Enter')fcardNoteSave('${item.message_id}');if(event.key==='Escape')fcardNoteCancel()">
      <button class="btn btn-primary btn-sm" onclick="fcardNoteSave('${item.message_id}')">保存</button>
      <button class="btn btn-soft btn-sm" onclick="fcardNoteCancel()">取消</button>
    </div>`);
  }
  el.innerHTML = chips.join('');
  // 重绘前正在编辑这一条备注 → 自动恢复编辑态（抗 email/翻译回调、沉浸式翻译扩展的重绘冲掉）
  if(_noteEditing && item && _noteEditing===item.message_id) fcardNoteEdit(item.message_id);
}
async function fcardStatus(mid, sel){
  sel.disabled = true;
  try{
    const r = await fetch('/api/feishu-update',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({message_id:mid, status:sel.value})});
    const d = await r.json();
    if(d.ok){
      const it = allItems.find(i=>i.message_id===mid);
      if(it){
        it.feishu_status=d.feishu_status;
        if(it.feishu){ it.feishu.status=sel.value; }
        // 联动本地徽章：不合适/无法合作 → ❌；改回其他 → 解除
        if(/不合适|无法合作/.test(sel.value)) it.action='not_suitable';
        else if(it.action==='not_suitable') it.action='';
      }
      updateStats(calcStats()); renderList();
      if(_selMid===mid) renderFCard(curItem());
    } else { alert('❌ '+d.error); }
  }catch(e){ alert('❌ 网络错误'); }
  sel.disabled = false;
}
let _noteEditing = '';   // 正在编辑备注的 mid（重绘后据此自动恢复编辑态）
let _noteDraft   = null; // 编辑中的草稿值（存 JS 变量，重绘销毁 input 也不丢字）
function fcardNoteEdit(mid){
  const it = curItem(); if(!it || it.message_id!==mid) return;
  const first = _noteEditing !== mid;
  _noteEditing = mid;
  if(first) _noteDraft = it.note || '';   // 首次进入用表里的值，之后用草稿
  const view = document.getElementById('f-note-view');
  const ed   = document.getElementById('f-note-edit');
  const inp  = document.getElementById('f-note-input');
  if(!ed || !inp) return;
  if(view) view.style.display='none';
  ed.style.display='flex';
  inp.value = _noteDraft;
  inp.oninput = ()=>{ _noteDraft = inp.value; };   // 实时把字存进 JS 变量
  if(document.activeElement !== inp){
    // focus 延后到下一帧：绕开 display 切换 / 沉浸式翻译扩展 DOM 注入导致的首次失焦
    requestAnimationFrame(()=>requestAnimationFrame(()=>{ try{ inp.focus(); const n=inp.value.length; inp.setSelectionRange(n,n);}catch(e){} }));
  }
}
function fcardNoteCancel(){ _noteEditing=''; _noteDraft=null; renderFCard(curItem()); }
async function fcardNoteSave(mid){
  const inp = document.getElementById('f-note-input');
  const note = inp.value.trim();
  inp.disabled = true;
  try{
    const r = await fetch('/api/feishu-update',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({message_id:mid, note})});
    const d = await r.json();
    if(d.ok){
      const it = allItems.find(i=>i.message_id===mid);
      if(it) it.note = d.note;
      _noteEditing = ''; _noteDraft = null;   // 退出编辑态，避免重绘又拉起编辑框
      if(_selMid===mid) renderFCard(curItem());
    } else { alert('❌ '+d.error); inp.disabled=false; }
  }catch(e){ alert('❌ 网络错误'); inp.disabled=false; }
}

/* ── AI 生成回复 ── */
function fitReply(){
  // 回复框自动撑高到内容高度（上限 36vh，保证聊天区始终可见；超出框内滚动）
  const t = document.getElementById('p-edit');
  t.style.height = 'auto';
  t.style.height = Math.min(t.scrollHeight + 4, window.innerHeight * 0.24) + 'px';
}
function resetReplyBox(){
  const t = document.getElementById('p-edit');
  t.value = ''; t.style.height = '';
  _lastDraft = '';
  const tr = document.getElementById('p-edit-trans');
  tr.style.display='none'; tr.textContent='';
  clearReplyImgs();
  hideRate();
}

/* ── 回复里发图片：上传 → 芯片预览 → 发送时按 id 内联 ── */
let _replyImgs = [];   // [{id, name}]
function clearReplyImgs(){ _replyImgs = []; renderReplyImgs(); }
function renderReplyImgs(){
  const box = document.getElementById('p-imgs');
  if(!box) return;
  box.innerHTML = _replyImgs.map(im=>
    `<div class="rimg-chip" title="${escHTML(im.name||'')}">
       <img src="/api/reply-image/${encodeURIComponent(im.id)}" alt="">
       <button class="rm" onclick="removeReplyImg('${im.id}')" title="移除">✕</button>
     </div>`).join('');
}
function removeReplyImg(id){ _replyImgs = _replyImgs.filter(x=>x.id!==id); renderReplyImgs(); }
function onReplyPaste(e){
  const items = (e.clipboardData||{}).items||[];
  const imgs = [];
  for(const it of items){ if(it.type && it.type.startsWith('image/')){ const f=it.getAsFile(); if(f) imgs.push(f); } }
  if(imgs.length){ e.preventDefault(); attachImages(imgs); }   // 粘贴图片 = 直接上传，不进正文文本
}
async function attachImages(files){
  if(!files || !files.length) return;
  const box = document.getElementById('p-imgs');
  for(const f of files){
    if(!f.type || !f.type.startsWith('image/')) continue;
    const ph = document.createElement('div');
    ph.className='rimg-chip up'; ph.textContent='上传中…'; if(box) box.appendChild(ph);
    try{
      const fd = new FormData(); fd.append('image', f, f.name||'paste.png');
      const r = await fetch('/api/upload-image',{method:'POST',body:fd});
      const d = await r.json();
      if(d.ok){ _replyImgs.push({id:d.id, name:d.name||f.name||'image'}); }
      else { alert('图片上传失败：'+(d.error||'')); }
    }catch(e){ alert('图片上传失败'); }
    finally{ ph.remove(); }
  }
  renderReplyImgs();
}
/* ── AI 回复打分 ── */
let _rateCtx = null, _rateScore = 0;
function showRate(replyText, engine, instruction){
  _rateCtx = {reply:replyText, engine, instruction,
              email:(curItem()||{}).email||''};
  _rateScore = 0;
  document.querySelectorAll('.rate-star').forEach(s=>s.classList.remove('on'));
  document.getElementById('rate-note').value='';
  document.getElementById('rate-bar').classList.add('show');
}
function hideRate(){ document.getElementById('rate-bar').classList.remove('show'); _rateCtx=null; }
function setRate(n){
  _rateScore = n;
  document.querySelectorAll('.rate-star').forEach(s=>s.classList.toggle('on', (+s.dataset.s)<=n));
}
async function saveRate(){
  if(!_rateCtx || !_rateScore){ alert('先点星星打分'); return; }
  await fetch('/api/rate-reply',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({...
      _rateCtx, score:_rateScore, note:document.getElementById('rate-note').value.trim()})});
  document.getElementById('p-status').textContent='✅ 评分已记录，谢谢';
  hideRate();
}
async function translateReply(text){
  // 生成的英文回复 → 中文核验译文（哈希缓存，重复内容秒回）
  const box = document.getElementById('p-edit-trans');
  box.style.display='block'; box.textContent='🇨🇳 译文生成中...';
  try{
    const r = await fetch('/api/translate-texts',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({texts:[text]})});
    const d = await r.json();
    const tr = d.ok && d.trans && d.trans[0];
    if(tr) box.textContent = '🇨🇳 ' + tr;
    else box.style.display='none';
  }catch(e){ box.style.display='none'; }
}
function quickInstr(t){
  document.getElementById('p-edit').value = t;   // 指示进同一个框（微信式）
  composeReply();
}
let _lastDraft = '';   // 上次生成的英文稿（用于区分"框里是指示"还是"框里是稿"）
async function composeReply(){
  if(!_selMid) return;
  const btn   = document.getElementById('p-gen');
  const status= document.getElementById('p-status');
  const ed    = document.getElementById('p-edit');
  // 框里若还是上次生成的稿（没改过），再点生成 = 重新生成（不把英文稿当指示）
  const cur   = ed.value.trim();
  const instr = (cur && cur !== _lastDraft.trim()) ? cur : '';
  btn.disabled=true; btn.textContent='⟳ 生成中';
  status.textContent='AI 正在读完整往来撰写英文稿...';
  const t0 = performance.now();
  try{
    const res = await fetch('/api/compose',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({message_id:_selMid, instruction:instr,
        engine: document.getElementById('p-engine').value,
        short: document.getElementById('p-short').checked})});
    const d = await res.json();
    if(d.ok){
      ed.value = d.text;
      _lastDraft = d.text;
      fitReply();
      status.textContent=`✅ 已生成（${d.engine||'AI'} · ${((performance.now()-t0)/1000).toFixed(1)}s）— 可直接改，点「🚀 发送」出门`;
      translateReply(d.text);   // 中文核验译文
      showRate(d.text, d.engine||'', instr);   // 打分小框
    } else status.textContent='❌ '+d.error;
  }catch(e){ status.textContent='❌ 网络错误'; }
  btn.disabled=false; btn.textContent='🤖 生成';
}

/* ── 复制（手动流） ── */
async function copyReply(){
  const text = document.getElementById('p-edit').value;
  const btn  = document.getElementById('p-copy');
  try{
    await navigator.clipboard.writeText(text);
    btn.textContent='✅ 已复制';
    document.getElementById('p-status').textContent='已复制 → 去飞书粘贴发送，回来点刷新自动标记已回复';
  }catch(e){
    // 兼容回退
    const ta = document.getElementById('p-edit');
    ta.select(); document.execCommand('copy');
    btn.textContent='✅ 已复制';
  }
  setTimeout(()=>{ btn.textContent='📋'; }, 2500);
}

/* ── 批准发送 ── */
async function approveSend(){
  if(!_selMid) return;
  const btn    = document.getElementById('p-approve');
  const status = document.getElementById('p-status');
  const body   = document.getElementById('p-edit').value.trim();
  const withImg= document.getElementById('p-img').checked;
  if(!body){ status.textContent='正文为空'; return; }
  btn.disabled=true; btn.textContent='提交中...';
  const res = await fetch('/api/approve',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({message_id:_selMid, final_body:body, with_image:withImg, images:_replyImgs.map(x=>x.id)})});
  const d = await res.json();
  if(!d.ok){ status.textContent='❌ '+d.error; btn.disabled=false; btn.textContent='🚀 发送'; return; }
  btn.textContent='后台处理中...';
  status.textContent='⏳ 处理中，可直接点下一封继续';
  const mid=_selMid, jobId=d.job_id;
  (async function poll(){
    try{
      const r2 = await fetch('/api/job/'+jobId); const job = await r2.json();
      const here = _selMid===mid;
      if(job.status==='sent'){
        applyResult(mid,'sent','');
        const wasReview = (_reviewMid===mid) || _pendingItems.some(x=>x.message_id===mid);
        if(wasReview){
          // 审核中发出的草稿 → 从队列移除并自动跳下一条
          fetch('/api/pending-discard',{method:'POST',headers:{'Content-Type':'application/json'},
            body:JSON.stringify({message_ids:[mid]})}).then(()=>refreshPending()).then(()=>{
            if(here){ status.textContent='🚀 已发送，下一条'; btn.disabled=false; btn.textContent='🚀 发送'; }
            if(_pendingItems.length){ loadPendingDraft(_pendingItems[0].message_id); }
            else { _reviewMid=''; closeReview(); resetReplyBox(); }
          });
        } else if(here){ status.textContent='🚀 已发送！'; btn.disabled=false; btn.textContent='🚀 发送'; resetReplyBox(); openItem(mid); }
      } else if(job.status==='drafted'){
        applyResult(mid,'drafted',job.draft_url||'');
        if(here){
          status.innerHTML='⚠️ '+(job.note||'已转草稿')+(job.draft_url?` &nbsp;<a href="${job.draft_url}" target="_blank" style="color:var(--accent)">打开草稿</a>`:'');
          btn.disabled=false; btn.textContent='🚀 发送';
        }
      } else if(job.status==='error'){
        if(here){ status.textContent='❌ '+job.error; btn.disabled=false; btn.textContent='🚀 发送'; }
      } else setTimeout(poll, 800);
    }catch(e){ setTimeout(poll, 1500); }
  })();
}
function applyResult(mid, action, draftUrl){
  const item = allItems.find(i=>i.message_id===mid);
  if(item){
    item.action=action; if(draftUrl)item.draft_url=draftUrl;
    item.unread=false; item.action_ts=new Date().toLocaleString('zh');
    item.history=[...(item.history||[]),{action,ts:item.action_ts}];
  }
  _selected.delete(mid); updateBatchBtn();
  updateStats(calcStats()); renderList();
  if(_selMid===mid && item){ openItemHeaderRefresh(item); refreshQuick(item); }
}

/* ── 标记 ── */
async function doMark(mid, action){
  const res = await fetch('/api/mark',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({message_id:mid, action})});
  const d = await res.json();
  if(d.ok){
    const item = allItems.find(i=>i.message_id===mid);
    if(item){ item.action = action==='pending'?'':action;
      item.action_ts=new Date().toLocaleString('zh'); }
    _selected.delete(mid); updateBatchBtn();
    updateStats(calcStats()); renderList();
    if(_selMid===mid && item){ openItemHeaderRefresh(item); refreshQuick(item);
      const tip = document.getElementById('p-processed-tip');
      if(item.action){ tip.style.display='block';
        tip.textContent=`这封已标记「${ACTION_TXT[item.action]||item.action}」。`; }
      else tip.style.display='none';
    }
  }
}

/* ── 批量 ── */
function toggleSelect(mid, checked){
  if(checked) _selected.add(mid); else _selected.delete(mid);
  updateBatchBtn();
}
function selectAllPending(){
  // 待告别筛选下全选整个名单（这批人大多已回复过）；其他筛选只选未处理的
  const pick = currentFilter==='farewell' ? filteredItems() : filteredItems().filter(i=>!i.action);
  pick.forEach(i=>_selected.add(i.message_id));
  renderList(); updateBatchBtn();
}
function clearSelection(){ _selected.clear(); renderList(); updateBatchBtn(); }
function updateBatchBtn(){
  const n = _selected.size;
  const b2 = document.getElementById('batch-ai-btn');
  b2.textContent = `🤖 AI 批量生成 (${n})`; b2.disabled = n===0;
  const b3 = document.getElementById('batch-fw-btn');
  b3.textContent = `👋 告别批量 (${n})`; b3.disabled = n===0;
  // 批量栏只在"有勾选 / 有任务进度"时出现，平时不占一行
  const prog = document.getElementById('batch-progress').textContent.trim();
  document.getElementById('batchbar').classList.toggle('show', n>0 || !!prog);
}
async function batchFarewell(){
  if(!_selected.size) return;
  const b = document.getElementById('batch-fw-btn');
  b.disabled = true; b.textContent = '提交中...';
  const res = await fetch('/api/batch-farewell',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({message_ids:[..._selected],
      engine: localStorage.getItem('bloome-engine')||'deepseek',
      short: localStorage.getItem('bloome-short')!=='0'})});
  const d = await res.json();
  if(!d.ok){
    document.getElementById('batch-progress').textContent = '❌ '+d.error;
    updateBatchBtn(); return;
  }
  document.getElementById('batch-progress').textContent =
    `👋 正在按对方语种生成告别信（共${d.total}封），生成后进待发送审核`;
  _selected.clear();
  pollBatch();
}
async function batchCompose(){
  if(!_selected.size) return;
  const b2 = document.getElementById('batch-ai-btn');
  b2.disabled = true; b2.textContent = '提交中...';
  const res = await fetch('/api/batch-compose',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({message_ids:[..._selected],
      engine: localStorage.getItem('bloome-engine')||'deepseek',
      short: localStorage.getItem('bloome-short')!=='0',
      instruction: (document.getElementById('batch-instr').value||'').trim()})});
  const d = await res.json();
  if(!d.ok){
    document.getElementById('batch-progress').textContent = '❌ '+d.error;
    updateBatchBtn(); return;
  }
  document.getElementById('batch-progress').textContent =
    `🤖 AI 正在逐封生成（共${d.total}封），生成后进待发送审核，不会自动发出`;
  _selected.clear();
  pollBatch();
}
async function batchApprove(){
  if(!_selected.size) return;
  const lang = document.getElementById('batch-lang').value;
  const btn  = document.getElementById('batch-btn');
  btn.disabled = true; btn.textContent = '生成中...';
  const res = await fetch('/api/batch-approve',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({message_ids:[..._selected], lang,
      template_type: document.getElementById('batch-type').value, with_image:false})});
  const d = await res.json();
  if(!d.ok){
    document.getElementById('batch-progress').textContent = '❌ '+d.error;
    updateBatchBtn(); return;
  }
  _selected.clear(); renderList(); updateBatchBtn();
  const skip = (d.skipped||[]).length;
  const ctx  = (d.skipped_ctx||[]).length;
  let msg = `📬 已生成 ${d.queued} 封进待发送队列`;
  if(ctx)  msg += `；⛔ ${ctx} 封已拦截（对话已有来回，模板对不上上下文，请改用 🤖AI 批量生成）`;
  if(skip) msg += `（另 ${skip} 封无模板跳过）`;
  document.getElementById('batch-progress').textContent = msg;
  updateBatchBtn();
  await refreshPending();
  openReview();
}
async function pollBatch(){
  try{
    const res = await fetch('/api/batch-status'); const b = await res.json();
    const el = document.getElementById('batch-progress');
    if(b.running){
      el.textContent = (b.kind==='send' ? '🚀 发送中 ' : '⏳ 生成中 ')
        + `${b.done}/${b.total}（成功${b.ok} 失败${b.fail}）`;
      document.getElementById('batch-btn').disabled = true;
      document.getElementById('batch-ai-btn').disabled = true;
      setTimeout(pollBatch, 1200);
    } else if(b.total > 0 && b.done === b.total){
      if(b.kind === 'send'){
        const modeTxt = b.mode==='direct' ? '已发出' : '已建草稿，去飞书草稿箱发送';
        el.innerHTML = `✅ 发送完成 ${b.ok}/${b.total}（${modeTxt}）&nbsp;<a href="https://www.feishu.cn/mail" target="_blank">打开飞书邮箱</a>`;
        await refreshPending();
        loadReplies(false);
      } else {
        el.textContent = `✅ 已生成 ${b.ok}/${b.total} 封 → 在「📬 待发送审核」里过目后发送`;
        await refreshPending();
        openReview();
      }
      updateBatchBtn();
    } else { updateBatchBtn(); }
  }catch(e){ setTimeout(pollBatch, 2000); }
}

/* ── 待发送审核面板（批量唯一的发送出口） ── */
let _pendingItems = [];
let _reviewMid = '';   // 当前装载到回复框审核中的草稿 mid
async function refreshPending(){
  try{
    const r = await fetch('/api/pending'); const d = await r.json();
    _pendingItems = d.items || [];
    const b = document.getElementById('pending-btn');
    b.style.display = _pendingItems.length ? '' : 'none';
    b.textContent = `📬 待发送审核 (${_pendingItems.length})`;
    if(document.getElementById('review-dock').classList.contains('show')) renderReviewDock();
  }catch(e){}
}
function openReview(){
  if(!_pendingItems.length){ return; }
  document.getElementById('review-dock').classList.add('show');
  renderReviewDock();
  loadPendingDraft(_pendingItems[0].message_id);   // 自动装载第一条
}
function closeReview(){
  document.getElementById('review-dock').classList.remove('show');
  _reviewMid='';
}
function renderReviewDock(){
  document.getElementById('rv-count').textContent = `(${_pendingItems.length})`;
  const box = document.getElementById('rv-list');
  if(!_pendingItems.length){
    box.innerHTML = '<div style="color:var(--text-3);text-align:center;padding:30px;font-size:12px">队列已清空 🎉</div>';
    return;
  }
  box.innerHTML = _pendingItems.map(it => {
    const name = (it.email||'').split('@')[0];
    return `<div class="rv-row ${it.message_id===_reviewMid?'active':''}" onclick="loadPendingDraft('${escHTML(it.message_id)}')">
      <div class="rv-em">${escHTML(name)} <span style="color:var(--text-3);font-weight:400">@${escHTML((it.email||'').split('@')[1]||'')}</span></div>
      <div class="rv-kd">${escHTML(it.kind||'')} · ${escHTML(it.ts||'')}</div>
      <div class="rv-pv">${escHTML(it.text||'')}</div>
    </div>`;
  }).join('');
}
function loadPendingDraft(mid){
  const it = _pendingItems.find(x=>x.message_id===mid);
  if(!it) return;
  _reviewMid = mid;
  // 中间聊天区跳到该博主（带完整上下文）；openItem 完成后把草稿填进回复框
  const inAll = allItems.some(x=>x.message_id===mid);
  if(inAll){
    openItem(mid).then(()=>{ fillReviewDraft(it); });
  } else {
    // 极少数：队列项不在当前筛选/列表里，仍直接把草稿放进回复框
    fillReviewDraft(it);
  }
  renderReviewDock();
}
function fillReviewDraft(it){
  const ed = document.getElementById('p-edit');
  ed.value = it.text || ''; _lastDraft = it.text || '';
  fitReply(); translateReply(it.text||'');
  document.getElementById('p-status').textContent = '📬 审核中：这是队列里给 '+(it.email||'')+' 的草稿，改好点「🚀 发送」即出列';
  ed.focus();
}
async function rvSendAll(){
  if(!_pendingItems.length) return;
  if(!confirm(`不逐条审，直接发出队列里全部 ${_pendingItems.length} 封？\n直发模式下立即发出、无法撤回。`)) return;
  const btn = document.getElementById('rv-sendall-btn'); btn.disabled = true;
  const items = _pendingItems.map(it=>({message_id:it.message_id, text:it.text}));
  const r = await fetch('/api/pending-send',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({items})});
  const d = await r.json(); btn.disabled = false;
  if(!d.ok){ alert(d.error || '发送失败'); return; }
  closeReview();
  document.getElementById('batch-progress').textContent = `🚀 正在发送 ${d.total} 封...`;
  pollBatch();
}

/* ── 设置：提示词编辑 + 评分记录 ── */
let _settingsTab = 'prompts', _promptsData = null;
async function openSettings(){
  document.getElementById('settings-mask').classList.add('show');
  switchSettingsTab(_settingsTab);
}
function closeSettings(){ document.getElementById('settings-mask').classList.remove('show'); }

const STATUS_COLORS = {'待联系':'#8a8f99','已联系':'#3aa3cf','沟通中':'#46b58a','达成合作':'#5fae5f',
  '推进制作':'#9a6de0','已上线':'#46b58a','待定':'#c79a3e','需审核':'#d8618c',
  '无法合作':'#cf736a','不合适':'#cf736a','（未填）':'#6b7280'};
async function openStats(){
  const mask=document.getElementById('stats-mask'), body=document.getElementById('stats-body');
  mask.classList.add('show');
  document.getElementById('stats-total').textContent='';
  body.innerHTML='<p class="empty">⟳ 正在读飞书全表统计…</p>';
  try{
    const r=await fetch('/api/stats'); const d=await r.json();
    if(!d.ok){ body.innerHTML='<p class="empty">统计失败：'+escHTML(d.error||'')+'</p>'; return; }
    document.getElementById('stats-total').textContent='共 '+d.total+' 位博主';
    const max=Math.max(1,...d.counts.map(c=>c.count));
    body.innerHTML = d.counts.map(c=>{
      const col=statusColorOf(c.status)||STATUS_COLORS[c.status]||'#6b7280';
      const pct=Math.round(c.count/max*100);
      return `<div style="display:flex;align-items:center;gap:10px;margin:7px 0">
        <span style="flex:0 0 84px;font-size:12.5px;color:var(--text)">${escHTML(c.status)}</span>
        <span style="flex:1;height:18px;background:var(--bg-2);border-radius:5px;overflow:hidden">
          <span style="display:block;height:100%;width:${pct}%;background:${col};border-radius:5px"></span></span>
        <b style="flex:0 0 38px;text-align:right;font-size:12.5px;color:var(--text)">${c.count}</b>
      </div>`;
    }).join('') || '<p class="empty">无数据</p>';
  }catch(e){ body.innerHTML='<p class="empty">统计失败</p>'; }
}
function closeStats(){ document.getElementById('stats-mask').classList.remove('show'); }
function switchSettingsTab(tab){
  _settingsTab = tab;
  document.getElementById('tab-prompts').classList.toggle('active', tab==='prompts');
  document.getElementById('tab-ratings').classList.toggle('active', tab==='ratings');
  document.getElementById('prompts-save-btn').style.display = tab==='prompts'?'':'none';
  if(tab==='prompts') renderPromptsEditor(); else renderRatings();
}
const _PF_LABELS = {persona:'人设 / 身份（每次生成开头）', rules:'核心要求（单封+批量共用的铁律）',
                    playbook:'合作落地 playbook（仅"达成合作/推进制作"状态套用）', farewell:'告别信要求（告别批量用）'};
async function renderPromptsEditor(){
  const body = document.getElementById('settings-body');
  body.innerHTML = '<div style="color:var(--text-3);padding:20px">加载中…</div>';
  const d = await (await fetch('/api/prompts')).json();
  _promptsData = d.prompts;
  body.innerHTML = '<div style="font-size:11.5px;color:var(--text-3);margin-bottom:12px">改完点右上「保存提示词」即时生效（存 vault 外文件，不动代码）。留空=回落默认。</div>'
    + Object.keys(_PF_LABELS).map(k=>`
      <div class="pf-field">
        <label>${_PF_LABELS[k]}</label>
        <textarea id="pf-${k}" rows="${k==='persona'?2:(k==='playbook'?9:6)}">${escHTML(d.prompts[k]||'')}</textarea>
      </div>`).join('');
}
async function savePrompts(){
  const payload = {};
  Object.keys(_PF_LABELS).forEach(k=>{ payload[k] = document.getElementById('pf-'+k).value; });
  const btn = document.getElementById('prompts-save-btn'); btn.disabled=true; btn.textContent='保存中…';
  const d = await (await fetch('/api/prompts',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)})).json();
  btn.disabled=false; btn.textContent='保存提示词';
  alert(d.ok ? '✅ 提示词已保存，下次生成即用新版' : ('❌ '+(d.error||'失败')));
}
async function renderRatings(){
  const body = document.getElementById('settings-body');
  body.innerHTML = '<div style="color:var(--text-3);padding:20px">加载中…</div>';
  const d = await (await fetch('/api/ratings')).json();
  if(!d.rows.length){ body.innerHTML='<div style="color:var(--text-3);padding:30px;text-align:center">还没有评分记录。生成回复后在下方打分小框评分即可积累。</div>'; return; }
  body.innerHTML = `<div style="font-size:12px;color:var(--text-2);margin-bottom:12px">共 ${d.total} 条 · 平均 ${d.avg} ⭐（最新 ${d.rows.length} 条）</div>`
    + d.rows.map(r=>`<div class="rt-row">
        <div class="rt-h">${'⭐'.repeat(r.score)} · ${escHTML(r.email)} · ${escHTML(r.engine)} · ${escHTML(r.ts)}${r.instruction?(' · 指示：'+escHTML(r.instruction)):''}</div>
        <div class="rt-rp">${escHTML(r.reply)}</div>
        ${r.note?`<div class="rt-nt">📝 ${escHTML(r.note)}</div>`:''}
      </div>`).join('');
}

/* ── 付款申请预填 ── */
let _payDraft = null;
const PAY_FIELDS = [['urgent','是否加急'],['due_date','最晚付款日期'],['reason','付款事由'],
  ['category','费用类别'],['project','所属项目（可多选）'],['entity','付款主体'],
  ['method','付款方式'],['amount','付款金额'],['currency','币种'],
  ['payee','境外收款账户'],['has_invoice','是否有发票'],['resource_link','视频/资源链接']];
async function openPayment(){
  if(!_selMid){ return; }
  const mask=document.getElementById('payment-mask'); mask.classList.add('show');
  const body=document.getElementById('payment-body');
  body.innerHTML='<div style="padding:26px;color:var(--text-3)">🤖 AI 正在从完整对话提取付款信息…</div>';
  try{
    const d=await(await fetch('/api/payment-draft',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({message_id:_selMid, engine:document.getElementById('p-engine').value})})).json();
    if(!d.ok){ body.innerHTML='<div style="padding:26px;color:var(--c-red)">提取失败：'+(d.error||'')+'</div>'; return; }
    _payDraft=d.draft; renderPayment();
  }catch(e){ body.innerHTML='<div style="padding:26px;color:var(--c-red)">网络错误</div>'; }
}
function renderPayment(){
  const body=document.getElementById('payment-body');
  body.innerHTML=PAY_FIELDS.map(([k,label])=>{
    const v=_payDraft[k]||'';
    if(k==='reason')
      return `<div class="pf-field"><label>${label}</label><textarea id="pay-${k}" rows="3" oninput="_payDraft['${k}']=this.value">${escHTML(v)}</textarea></div>`;
    return `<div class="pf-field"><label>${label}</label><input id="pay-${k}" value="${escHTML(v).replace(/"/g,'&quot;')}" oninput="_payDraft['${k}']=this.value" style="width:100%;background:var(--input-bg);border:1px solid var(--border);border-radius:8px;padding:8px 11px;font-size:12.5px;color:var(--text);outline:none;box-sizing:border-box"></div>`;
  }).join('');
}
function closePayment(){ document.getElementById('payment-mask').classList.remove('show'); }
async function copyPaymentAll(){
  if(!_payDraft) return;
  const txt=PAY_FIELDS.map(([k,label])=>`${label}：${_payDraft[k]||''}`).join('\n');
  try{ await navigator.clipboard.writeText(txt); alert('✅ 已复制付款申请，去飞书逐项填'); }
  catch(e){ alert('复制失败，请手动选取'); }
}

/* ── 预加载 ── */
async function pollPreload(){
  try{
    const res = await fetch('/api/preload-status'); const d = await res.json();
    const bar = document.getElementById('preload-bar');
    if(d.finished){ bar.classList.remove('show'); return; }
    bar.classList.add('show');
    document.getElementById('preload-text').textContent = (d.stage||'加载')+'中...';
    if(d.total>0) document.getElementById('preload-pct').textContent = d.done+'/'+d.total;
    setTimeout(pollPreload, 1500);
  }catch(e){ setTimeout(pollPreload, 3000); }
}

/* ── 分栏拖拽（侧栏 / 列表 两条线都可拖，双击恢复默认，窗口缩放自动钳制） ── */
function makeResizer(rzId, el, key, defW, minW, maxW){
  const rz = document.getElementById(rzId);
  if(!rz || !el) return;
  const clamp = w => Math.min(Math.max(w, minW), maxW());
  const saved = parseInt(localStorage.getItem(key));
  if(saved) el.style.width = clamp(saved)+'px';
  rz.addEventListener('mousedown', e=>{
    const sx=e.clientX, sw=el.offsetWidth;
    rz.classList.add('active'); document.body.classList.add('resizing');
    const move = ev=>{ el.style.width = clamp(sw + ev.clientX - sx)+'px'; };
    const up = ()=>{
      rz.classList.remove('active'); document.body.classList.remove('resizing');
      localStorage.setItem(key, el.offsetWidth);
      document.removeEventListener('mousemove', move);
      document.removeEventListener('mouseup', up);
    };
    document.addEventListener('mousemove', move);
    document.addEventListener('mouseup', up);
    e.preventDefault();
  });
  rz.addEventListener('dblclick', ()=>{    // 双击恢复默认
    el.style.width=''; localStorage.removeItem(key);
  });
  window.addEventListener('resize', ()=>{
    if(el.style.width) el.style.width = clamp(el.offsetWidth)+'px';
  });
}
makeResizer('resizer',    document.getElementById('maillist'), 'bloome-listw', 392, 240, ()=>window.innerWidth*0.6);
makeResizer('sb-resizer', document.querySelector('.sidebar'),  'bloome-sbw',   208, 150, ()=>Math.min(360, window.innerWidth*0.3));

document.getElementById('p-engine').value = localStorage.getItem('bloome-engine') || 'deepseek';
document.getElementById('p-short').checked = localStorage.getItem('bloome-short') !== '0';   // 默认开
loadReplies();
pollPreload();
refreshPending();
setInterval(()=>loadReplies(true), 10*60*1000);   // 收件箱自动同步：每 10 分钟（手动刷新随时可点）
</script>
</body>
</html>"""

@app.route("/")
def index():
    resp = app.make_response(HTML)
    resp.headers["Cache-Control"] = "no-store"   # 禁缓存：改版后普通刷新即生效
    return resp

if __name__ == "__main__":
    print("📮 Bloome 邮件回复助手 v5（分栏式 · 双向同步）")
    print("   访问 → http://localhost:5050")
    print("   停止 → Ctrl+C\n")
    threading.Thread(target=_preload_everything, daemon=True).start()
    threading.Thread(target=_status_sync_loop, daemon=True).start()
    app.run(host="127.0.0.1", port=5050, debug=False, threaded=True)
