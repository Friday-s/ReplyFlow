from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any


# Data dir: ~/.replyflow; keep using legacy ~/.replydesk if it already has data.
_NEW_DIR = Path.home() / ".replyflow"
_OLD_DIR = Path.home() / ".replydesk"
APP_DATA_DIR = _OLD_DIR if (_OLD_DIR.exists() and not _NEW_DIR.exists()) else _NEW_DIR
MAIL_STORE_PATH = APP_DATA_DIR / "mail_store.sqlite3"


class MailStore:
    """Small SQLite archive for Feishu mail metadata, thread bodies, and local drafts."""

    def __init__(self, path: Path = MAIL_STORE_PATH):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init(self) -> None:
        with self._conn() as conn:
            conn.executescript(
                """
                create table if not exists messages (
                    message_id text primary key,
                    thread_id text,
                    email text,
                    from_raw text,
                    subject text,
                    date text,
                    date_formatted text,
                    unread integer default 0,
                    folder text,
                    labels_json text,
                    body_plain_text text,
                    body_html text,
                    attachments_json text,
                    raw_json text,
                    updated_at text not null
                );

                create index if not exists idx_messages_email on messages(email);
                create index if not exists idx_messages_thread on messages(thread_id);
                create index if not exists idx_messages_date on messages(date);

                create table if not exists local_drafts (
                    id integer primary key autoincrement,
                    message_id text,
                    email text,
                    body text not null,
                    source text,
                    created_at text not null
                );
                """
            )

    def upsert_message(self, message: dict[str, Any], folder: str = "") -> None:
        message_id = str(message.get("message_id") or "").strip()
        if not message_id:
            return
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        body_plain = message.get("body_plain_text")
        body_html = message.get("body_html")
        attachments = message.get("attachments") or []
        labels = message.get("labels") or []
        with self._conn() as conn:
            conn.execute(
                """
                insert into messages (
                    message_id, thread_id, email, from_raw, subject, date, date_formatted,
                    unread, folder, labels_json, body_plain_text, body_html,
                    attachments_json, raw_json, updated_at
                )
                values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                on conflict(message_id) do update set
                    thread_id=excluded.thread_id,
                    email=coalesce(nullif(excluded.email, ''), messages.email),
                    from_raw=coalesce(nullif(excluded.from_raw, ''), messages.from_raw),
                    subject=coalesce(nullif(excluded.subject, ''), messages.subject),
                    date=coalesce(nullif(excluded.date, ''), messages.date),
                    date_formatted=coalesce(nullif(excluded.date_formatted, ''), messages.date_formatted),
                    unread=excluded.unread,
                    folder=coalesce(nullif(excluded.folder, ''), messages.folder),
                    labels_json=excluded.labels_json,
                    body_plain_text=coalesce(excluded.body_plain_text, messages.body_plain_text),
                    body_html=coalesce(excluded.body_html, messages.body_html),
                    attachments_json=excluded.attachments_json,
                    raw_json=excluded.raw_json,
                    updated_at=excluded.updated_at
                """,
                (
                    message_id,
                    str(message.get("thread_id") or ""),
                    str(message.get("email") or message.get("from_email") or ""),
                    str(message.get("from_raw") or message.get("from") or ""),
                    str(message.get("subject") or ""),
                    str(message.get("date") or ""),
                    str(message.get("date_formatted") or ""),
                    1 if message.get("unread") else 0,
                    folder,
                    json.dumps(labels, ensure_ascii=False),
                    body_plain,
                    body_html,
                    json.dumps(attachments, ensure_ascii=False),
                    json.dumps(message, ensure_ascii=False, default=str),
                    now,
                ),
            )

    def upsert_many(self, messages: list[dict[str, Any]], folder: str = "") -> None:
        for message in messages:
            self.upsert_message(message, folder=folder)

    def get_message(self, message_id: str) -> dict[str, Any] | None:
        with self._conn() as conn:
            row = conn.execute(
                "select * from messages where message_id = ?", (message_id,)
            ).fetchone()
        return self._row_to_message(row) if row else None

    def get_thread(self, thread_id: str) -> list[dict[str, Any]]:
        if not thread_id:
            return []
        with self._conn() as conn:
            rows = conn.execute(
                """
                select * from messages
                where thread_id = ?
                order by coalesce(date, date_formatted, updated_at)
                """,
                (thread_id,),
            ).fetchall()
        return [self._row_to_message(row) for row in rows]

    def list_messages(self, limit: int = 200) -> list[dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                """
                select * from messages
                order by coalesce(date, date_formatted, updated_at) desc
                limit ?
                """,
                (limit,),
            ).fetchall()
        return [self._row_to_message(row) for row in rows]

    def save_local_draft(self, message_id: str, email: str, body: str, source: str) -> None:
        if not body.strip():
            return
        with self._conn() as conn:
            conn.execute(
                """
                insert into local_drafts(message_id, email, body, source, created_at)
                values (?, ?, ?, ?, ?)
                """,
                (
                    message_id,
                    email,
                    body,
                    source,
                    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                ),
            )

    def stats(self) -> dict[str, Any]:
        with self._conn() as conn:
            total = conn.execute("select count(*) from messages").fetchone()[0]
            with_body = conn.execute(
                "select count(*) from messages where body_plain_text is not null and body_plain_text != ''"
            ).fetchone()[0]
            drafts = conn.execute("select count(*) from local_drafts").fetchone()[0]
            last = conn.execute("select max(updated_at) from messages").fetchone()[0]
        return {
            "path": str(self.path),
            "messages": total,
            "messages_with_body": with_body,
            "local_drafts": drafts,
            "last_updated": last or "",
        }

    def _row_to_message(self, row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        raw = self._loads(data.pop("raw_json"), {})
        merged = raw if isinstance(raw, dict) else {}
        data["labels"] = self._loads(data.pop("labels_json"), [])
        data["attachments"] = self._loads(data.pop("attachments_json"), [])
        for key, value in data.items():
            if value not in (None, "", []):
                merged[key] = value
        merged["raw"] = raw
        data["unread"] = bool(data.get("unread"))
        merged["unread"] = bool(merged.get("unread"))
        return merged

    @staticmethod
    def _loads(value: str | None, default: Any) -> Any:
        if not value:
            return default
        try:
            return json.loads(value)
        except Exception:
            return default
