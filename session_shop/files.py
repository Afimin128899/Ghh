from __future__ import annotations

import shutil
import stat
import zipfile
from pathlib import Path, PurePosixPath


ALLOWED_ARCHIVE_SUFFIXES = {".json", ".session"}
SQLITE_HEADER = b"SQLite format 3\x00"


class ArchiveValidationError(ValueError):
    """Raised when an uploaded ZIP archive is unsafe or too large."""


def safe_filename(name: str | None, fallback: str = "upload.bin") -> str:
    """Return a basename suitable for writing inside an upload directory."""
    normalized = (name or "").replace("\\", "/").replace("\x00", "")
    candidate = PurePosixPath(normalized).name.strip()
    candidate = "".join(char for char in candidate if ord(char) >= 32)
    if candidate in {"", ".", ".."}:
        return fallback
    return candidate[:255]


def secure_path(path: Path, mode: int = 0o600) -> None:
    """Apply restrictive permissions where the platform supports them."""
    try:
        path.chmod(mode)
    except OSError:
        pass


def find_session_files(root: Path) -> list[Path]:
    return sorted(
        path
        for path in root.rglob("*")
        if path.is_file() and path.suffix.lower() == ".session"
    )


def is_sqlite_session(path: Path) -> bool:
    try:
        with path.open("rb") as session_file:
            return session_file.read(len(SQLITE_HEADER)) == SQLITE_HEADER
    except OSError:
        return False


def safe_extract_session_archive(
    archive_path: Path,
    destination: Path,
    *,
    max_members: int,
    max_uncompressed_bytes: int,
) -> list[Path]:
    """Extract only session/JSON files while blocking traversal and ZIP bombs."""
    destination.mkdir(parents=True, exist_ok=True)
    secure_path(destination, 0o700)
    root = destination.resolve()
    extracted: list[Path] = []

    try:
        archive = zipfile.ZipFile(archive_path, "r")
    except (OSError, zipfile.BadZipFile) as exc:
        raise ArchiveValidationError("Некорректный ZIP-архив") from exc

    with archive:
        members = archive.infolist()
        if len(members) > max_members:
            raise ArchiveValidationError(
                f"Слишком много файлов в архиве: {len(members)} (лимит {max_members})"
            )

        relevant = [
            info
            for info in members
            if not info.is_dir()
            and PurePosixPath(info.filename.replace("\\", "/")).suffix.lower()
            in ALLOWED_ARCHIVE_SUFFIXES
        ]
        total_size = sum(info.file_size for info in relevant)
        if total_size > max_uncompressed_bytes:
            raise ArchiveValidationError(
                "Распакованный архив превышает допустимый размер"
            )

        for info in relevant:
            normalized = info.filename.replace("\\", "/")
            relative = PurePosixPath(normalized)
            if relative.is_absolute() or ".." in relative.parts:
                raise ArchiveValidationError("Архив содержит небезопасный путь")
            if info.flag_bits & 0x1:
                raise ArchiveValidationError("Зашифрованные ZIP-файлы не поддерживаются")

            unix_mode = info.external_attr >> 16
            if unix_mode and stat.S_ISLNK(unix_mode):
                raise ArchiveValidationError("Символические ссылки в архиве запрещены")

            target = destination.joinpath(*relative.parts)
            resolved_target = target.resolve(strict=False)
            if not resolved_target.is_relative_to(root):
                raise ArchiveValidationError("Архив пытается записать файл вне каталога загрузки")
            if target.exists():
                raise ArchiveValidationError("Архив содержит повторяющиеся имена файлов")

            target.parent.mkdir(parents=True, exist_ok=True)
            secure_path(target.parent, 0o700)
            try:
                with archive.open(info, "r") as source, target.open("xb") as output:
                    shutil.copyfileobj(source, output, length=1024 * 1024)
            except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
                raise ArchiveValidationError("Не удалось безопасно распаковать архив") from exc
            secure_path(target)
            extracted.append(target)

    return extracted


def _upload_root(path: Path) -> Path | None:
    for parent in path.parents:
        if parent.name.startswith("upload_"):
            return parent
    return None


def remove_upload_artifact(file_path: str | Path | None) -> None:
    """Delete a processed session and remove its upload tree when it is empty."""
    if not file_path:
        return

    path = Path(file_path)
    upload_root = _upload_root(path)
    for candidate in (path, path.with_suffix(".json")):
        try:
            candidate.unlink(missing_ok=True)
        except OSError:
            pass

    if upload_root and upload_root.exists() and not find_session_files(upload_root):
        shutil.rmtree(upload_root, ignore_errors=True)
