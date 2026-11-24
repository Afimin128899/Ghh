from __future__ import annotations

import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from session_shop.storage import Database, QueueLimitError


class DatabaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.database_path = self.root / "queue.db"
        self.database = Database(self.database_path)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def add_item(self, item_id: str) -> Path:
        session = self.root / f"{item_id}.session"
        session.write_bytes(item_id.encode())
        self.database.add_queue_item(
            item_id=item_id,
            chat_id=42,
            file_path=str(session),
            original_name=session.name,
            auto_2fa=None,
            proxy_dict=None,
        )
        return session

    def test_claim_is_atomic_across_workers(self) -> None:
        for index in range(20):
            self.add_item(f"item-{index:02d}")

        with ThreadPoolExecutor(max_workers=8) as executor:
            claimed = list(
                executor.map(lambda _index: self.database.pop_next_item(42), range(20))
            )

        ids = [item["id"] for item in claimed if item is not None]
        self.assertEqual(len(ids), 20)
        self.assertEqual(len(set(ids)), 20)
        self.assertIsNone(self.database.pop_next_item(42))

    def test_processing_item_is_recovered_after_restart(self) -> None:
        self.add_item("recover-me")
        claimed = self.database.pop_next_item(42)
        self.assertEqual(claimed["id"], "recover-me")

        reopened = Database(self.database_path)
        recovered = reopened.pop_next_item(42)
        self.assertEqual(recovered["id"], "recover-me")

    def test_missing_files_are_removed_during_recovery(self) -> None:
        session = self.add_item("missing")
        session.unlink()

        reopened = Database(self.database_path)
        self.assertEqual(reopened.count_active_queue(42), 0)

    def test_remove_item_returns_sensitive_file_path(self) -> None:
        session = self.add_item("remove-me")
        removed_path = self.database.remove_item("remove-me")

        self.assertEqual(removed_path, str(session))
        self.assertEqual(self.database.count_active_queue(42), 0)

    def test_runtime_flags_round_trip(self) -> None:
        self.assertFalse(self.database.get_flag("feature", False))
        self.database.set_flag("feature", True)
        self.assertTrue(self.database.get_flag("feature", False))

    def test_queue_limit_rejects_entire_batch(self) -> None:
        first = self.root / "first.session"
        second = self.root / "second.session"
        first.write_bytes(b"first")
        second.write_bytes(b"second")
        items = [
            {
                "id": "first",
                "chat_id": 42,
                "file_path": str(first),
                "original_name": first.name,
                "auto_2fa": None,
                "proxy_dict": None,
            },
            {
                "id": "second",
                "chat_id": 42,
                "file_path": str(second),
                "original_name": second.name,
                "auto_2fa": None,
                "proxy_dict": None,
            },
        ]

        with self.assertRaises(QueueLimitError):
            self.database.add_queue_items(items, max_active=1)

        self.assertEqual(self.database.count_active_queue(42), 0)


if __name__ == "__main__":
    unittest.main()
