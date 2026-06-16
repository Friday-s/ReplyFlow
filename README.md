# ReplyFlow

Independent local WebUI for Feishu mail reply triage, AI drafting, review queues, and CRM updates.

## Run

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python3 webui.py
```

Open http://localhost:5050.

## Runtime Config

Set these locally before starting the app. Do not commit real values.
(Legacy `REPLYDESK_*` names are still accepted as a fallback.)

```bash
export REPLYFLOW_BASE_TOKEN="your-feishu-base-token"
export REPLYFLOW_TABLE_ID="your-feishu-table-id"
export REPLYFLOW_FROM_ADDRESS="you@example.com"
export REPLYFLOW_INTERNAL_DOMAINS="@your-company.com,@your-product.com"
export REPLYFLOW_BLOOME_IMG="$HOME/Downloads/promo-image.png"
export REPLYFLOW_INBOX_LIMIT="200"
```

DeepSeek key lookup:

1. `~/.replyflow/deepseek.key`
2. legacy fallback: `~/.bloome-deepseek.key`

## Local Mail Storage

Mail metadata, fetched thread bodies, generated drafts, and approved drafts are stored locally in:

```text
~/.replyflow/mail_store.sqlite3
```

(If you already have data under `~/.replydesk`, ReplyFlow keeps using it.)

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
