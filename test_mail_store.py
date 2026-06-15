import tempfile
import unittest
from pathlib import Path

from mail_store import MailStore


class MailStoreTest(unittest.TestCase):
    def test_message_thread_and_draft_storage(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = MailStore(Path(tmp) / "mail.sqlite3")
            store.upsert_message(
                {
                    "message_id": "msg_1",
                    "thread_id": "thread_1",
                    "email": "creator@example.com",
                    "from_raw": "Creator <creator@example.com>",
                    "subject": "Collab",
                    "date": "2026-06-15 12:00",
                    "body_plain_text": "Hello",
                    "attachments": [{"id": "att_1", "filename": "brief.pdf"}],
                },
                folder="INBOX",
            )
            store.save_local_draft("msg_1", "creator@example.com", "Thanks!", "test")

            msg = store.get_message("msg_1")
            self.assertEqual(msg["email"], "creator@example.com")
            self.assertEqual(msg["body_plain_text"], "Hello")
            self.assertEqual(msg["attachments"][0]["filename"], "brief.pdf")
            self.assertEqual(len(store.get_thread("thread_1")), 1)

            stats = store.stats()
            self.assertEqual(stats["messages"], 1)
            self.assertEqual(stats["messages_with_body"], 1)
            self.assertEqual(stats["local_drafts"], 1)


if __name__ == "__main__":
    unittest.main()
