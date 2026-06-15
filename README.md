# ReplyDesk

Independent local WebUI for Feishu mail reply triage, AI drafting, review queues, and CRM updates.

## Run

```bash
cd replydesk
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python3 webui.py
```

Open http://localhost:5050.

## Runtime Config

Set these locally before starting the app. Do not commit real values.

```bash
export REPLYDESK_BASE_TOKEN="your-feishu-base-token"
export REPLYDESK_TABLE_ID="your-feishu-table-id"
export REPLYDESK_FROM_ADDRESS="you@example.com"
export REPLYDESK_INTERNAL_DOMAINS="@your-company.com,@your-product.com"
export REPLYDESK_BLOOME_IMG="$HOME/Downloads/bloome.png"
export REPLYDESK_INBOX_LIMIT="200"
```

DeepSeek key lookup:

1. `~/.replydesk/deepseek.key`
2. legacy fallback: `~/.bloome-deepseek.key`

## Local Mail Storage

Mail metadata, fetched thread bodies, generated drafts, and approved drafts are stored locally in:

```text
~/.replydesk/mail_store.sqlite3
```

Useful local-store endpoints:

```text
GET /api/local-store
GET /api/local-store/messages?limit=100
```

The database lives outside the repository so private email content is not committed.

## External Dependencies

The app expects these CLIs to be available when using live Feishu/GitHub flows:

- `lark-cli`
- `claude` for translation fallback
- `codex` if using the Codex generation engine
- `gh` for GitHub repository scoring in some outreach templates
