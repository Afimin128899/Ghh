from __future__ import annotations

import tempfile
import unittest
import zipfile
from pathlib import Path

from session_shop.files import (
    ArchiveValidationError,
    find_session_files,
    is_sqlite_session,
    remove_upload_artifact,
    safe_extract_session_archive,
    safe_filename,
)


class FileSafetyTests(unittest.TestCase):
    def test_safe_filename_strips_path_components(self) -> None:
        self.assertEqual(safe_filename("../../secret.session"), "secret.session")
        self.assertEqual(safe_filename(r"..\..\secret.session"), "secret.session")

    def test_extracts_only_supported_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = root / "input.zip"
            destination = root / "upload_1_test"
            with zipfile.ZipFile(archive, "w") as bundle:
                bundle.writestr("nested/account.session", b"SQLite format 3\x00payload")
                bundle.writestr("nested/account.json", b"{}")
                bundle.writestr("nested/ignored.txt", b"ignored")

            extracted = safe_extract_session_archive(
                archive,
                destination,
                max_members=10,
                max_uncompressed_bytes=1024,
            )

            self.assertEqual(len(extracted), 2)
            self.assertEqual(len(find_session_files(destination)), 1)
            self.assertTrue(
                is_sqlite_session(destination / "nested" / "account.session")
            )
            self.assertFalse((destination / "nested" / "ignored.txt").exists())

    def test_rejects_path_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = root / "input.zip"
            with zipfile.ZipFile(archive, "w") as bundle:
                bundle.writestr("../stolen.session", b"session")

            with self.assertRaises(ArchiveValidationError):
                safe_extract_session_archive(
                    archive,
                    root / "upload_1_test",
                    max_members=10,
                    max_uncompressed_bytes=1024,
                )
            self.assertFalse((root / "stolen.session").exists())

    def test_rejects_oversized_archive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = root / "input.zip"
            with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
                bundle.writestr("large.session", b"x" * 2048)

            with self.assertRaises(ArchiveValidationError):
                safe_extract_session_archive(
                    archive,
                    root / "upload_1_test",
                    max_members=10,
                    max_uncompressed_bytes=1024,
                )

    def test_non_sqlite_file_is_not_a_telethon_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session = Path(tmp) / "fake.session"
            session.write_bytes(b"not a database")
            self.assertFalse(is_sqlite_session(session))

    def test_processed_upload_tree_is_removed_after_last_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            upload = Path(tmp) / "upload_1_test"
            upload.mkdir()
            first = upload / "first.session"
            second = upload / "second.session"
            first.write_bytes(b"one")
            first.with_suffix(".json").write_text("{}", encoding="utf-8")
            second.write_bytes(b"two")

            remove_upload_artifact(first)
            self.assertTrue(upload.exists())
            self.assertFalse(first.exists())

            remove_upload_artifact(second)
            self.assertFalse(upload.exists())


if __name__ == "__main__":
    unittest.main()
