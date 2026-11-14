"""Safe archive paths and recovery of legacy files written outside ``FILES_DIR``.

The pre-fix upload path was built as ``FILES_DIR / station_id / date / name``
without checking that ``station_id`` was a harmless path component.  A
station of ``..`` therefore escaped the retention tree (``files/../<date>/...``)
while still being accepted by the API.

Two guarantees are implemented here:

* new writes only ever use a safe station component (enforced by the API);
* a previously published version whose archive escaped the tree is repaired
  lazily on download.  The expected original is reconstructed from
  ``(station, date, version, file_id)`` -- never from the ``file_path``
  database column -- and is only accepted when its SHA-256 matches the
  checksum registered with that version.  A verified copy is placed at a
  safe location inside ``FILES_DIR``; only after the database registration
  has been updated is the old out-of-tree file removed.  If copying or
  registration fails, both the original and the registration stay untouched
  so the operation can be retried.  A per-version file lock keeps concurrent
  recoveries from racing each other.
"""
from __future__ import annotations

import fcntl
import hashlib
import os
import stat
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from . import config, db

CHUNK_SIZE = 1 * 1024 * 1024
# Reserved namespace for archives recovered from unsafe legacy station ids.
RECOVERED_DIRNAME = "_recovered"
is_safe_component = config.is_safe_component


class ArchiveError(Exception):
    """Base class for archive lookup/recovery failures (rendered as JSON)."""

    status_code = 500
    error_code = "archive_error"
    retryable = False

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


class ArchiveMissing(ArchiveError):
    status_code = 410
    error_code = "archive_missing"


class ArchiveCorrupt(ArchiveError):
    status_code = 422
    error_code = "archive_corrupt"


class ArchiveInvalid(ArchiveError):
    status_code = 422
    error_code = "archive_invalid"


class ArchiveRecoveryFailed(ArchiveError):
    """A copy/registration failure; nothing was removed, retrying is safe."""

    status_code = 500
    error_code = "archive_recovery_failed"
    retryable = True


def _encode_component(value: str) -> str:
    """Percent-encode everything but ASCII letters/digits.

    Python's :func:`urllib.parse.quote` leaves ``.`` unescaped, which would
    make ``..`` unsafe again, so encode the bytes ourselves.
    """
    parts: list[str] = []
    for char in value:
        if char.isascii() and char.isalnum():
            parts.append(char)
        else:
            parts.extend(f"%{byte:02X}" for byte in char.encode("utf-8"))
    return "".join(parts)


def _archive_name(version: int, file_id: str) -> str:
    name = f"v{int(version)}_{file_id}.csv"
    if not is_safe_component(name):
        raise ArchiveInvalid(
            "版本登记的文件名不是安全的路径片段，无法定位存档"
        )
    return name


def _within(path: Path, root: Path) -> bool:
    resolved = path.resolve()
    root_resolved = root.resolve()
    return resolved == root_resolved or root_resolved in resolved.parents


def _resolve_within(path: Path, root: Path, what: str) -> Path:
    resolved = path.resolve()
    if not _within(resolved, root):
        raise ArchiveInvalid(f"{what}超出允许的数据目录，拒绝访问")
    return resolved


def _source_candidate(station_id: str, date_str: str, name: str) -> Path:
    """Where the pre-fix code would have written this version's archive.

    This deliberately ignores the database ``file_path`` column: identity
    comes from station/date/version/file_id only.
    """
    return config.FILES_DIR / station_id / date_str / name


def _destination(station_id: str, date_str: str, name: str) -> Path:
    """Always an in-tree location, even for unsafe legacy station ids."""
    if is_safe_component(station_id):
        return config.FILES_DIR / station_id / date_str / name
    return (
        config.FILES_DIR
        / RECOVERED_DIRNAME
        / _encode_component(station_id)
        / date_str
        / name
    )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _unlink_quiet(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def _destination_status(path: Path, expected_checksum: str) -> bool | None:
    """Return True/False for a present-but-(in)valid file, None if absent."""
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ArchiveRecoveryFailed(
            f"无法检查存档状态，可重试：{exc}"
        ) from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        return False
    try:
        return _sha256_file(path) == expected_checksum
    except OSError as exc:
        raise ArchiveRecoveryFailed(
            f"无法读取存档校验和，可重试：{exc}"
        ) from exc


def _prepare_destination_parent(destination: Path) -> None:
    parent = destination.parent
    resolved_parent = parent.resolve()
    if not _within(resolved_parent, config.FILES_DIR):
        raise ArchiveInvalid("恢复目标超出留存目录，拒绝写入")
    try:
        parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ArchiveRecoveryFailed(
            f"无法创建留存目录，原文件已保留，可重试：{exc}"
        ) from exc


def _copy_verified(source: Path, destination: Path, expected_checksum: str) -> None:
    """Copy *source* to a unique temp file, verify it, then atomically land it.

    The source is only read; it is never moved or truncated here, so a
    failure at any point leaves it intact for a retry.
    """
    tmp_path = config.TMP_DIR / f".restore-{uuid.uuid4().hex}.tmp.csv"
    try:
        config.TMP_DIR.mkdir(parents=True, exist_ok=True)
        try:
            src_handle = open(source, "rb")
        except FileNotFoundError as exc:
            raise ArchiveMissing(
                f"留存原文件缺失，无法恢复：{source.name}"
            ) from exc
        except OSError as exc:
            raise ArchiveRecoveryFailed(
                f"无法读取留存原文件，可重试：{exc}"
            ) from exc

        digest = hashlib.sha256()
        with src_handle, open(tmp_path, "xb") as out_handle:
            for chunk in iter(lambda: src_handle.read(CHUNK_SIZE), b""):
                digest.update(chunk)
                out_handle.write(chunk)
            out_handle.flush()
            os.fsync(out_handle.fileno())

        actual = digest.hexdigest()
        if actual != expected_checksum:
            raise ArchiveCorrupt(
                "留存原文件校验和与版本登记不吻合，未做任何修改"
                f"（登记 {expected_checksum[:12]}…，实际 {actual[:12]}…）"
            )

        _prepare_destination_parent(destination)
        # Atomic replace: a concurrent recovery may have already landed the
        # very same verified bytes; checksums make that harmless.
        os.replace(tmp_path, destination)
    except ArchiveError:
        _unlink_quiet(tmp_path)
        raise
    except OSError as exc:
        _unlink_quiet(tmp_path)
        raise ArchiveRecoveryFailed(
            f"复制存档失败，原文件已保留，可重试：{exc}"
        ) from exc


def _register(
    row: Any,
    station_id: str,
    date_str: str,
    version: int,
    relative_path: str,
) -> None:
    if row["file_path"] == relative_path:
        return
    try:
        db.update_version_file_path(
            station_id,
            date_str,
            version,
            row["file_id"],
            row["checksum"],
            relative_path,
        )
    except Exception as exc:  # sqlite errors are wrapped by db as well
        raise ArchiveRecoveryFailed(
            f"数据库更新失败，原文件与版本登记已保留，可重试：{exc}"
        ) from exc


@contextmanager
def _restore_lock(key: str) -> Iterator[None]:
    config.TMP_DIR.mkdir(parents=True, exist_ok=True)
    lock_name = hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]
    lock_path = config.TMP_DIR / f"restore-{lock_name}.lock"
    handle = open(lock_path, "a+b")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def ensure_archive(
    row: Any,
    station_id: str,
    date_str: str,
    version: int,
) -> Path:
    """Return a verified in-tree path for the version, recovering if needed.

    ``row`` must contain the registered ``file_id``, ``checksum`` and
    ``file_path``.  The returned path is guaranteed to live under
    ``FILES_DIR`` and its bytes are guaranteed to match the registered
    checksum.
    """
    try:
        name = _archive_name(version, row["file_id"])
        checksum = row["checksum"]
        destination = _destination(station_id, date_str, name)
        relative_path = str(destination.relative_to(config.FILES_DIR))
        source = _resolve_within(
            _source_candidate(station_id, date_str, name),
            config.DATA_DIR,
            "留存原文件路径",
        )
        if source.name != name:
            raise ArchiveInvalid("留存原文件路径与版本登记不吻合")
    except ArchiveError:
        raise
    except OSError as exc:
        raise ArchiveInvalid(f"无法解析存档路径：{exc}") from exc

    key = f"{station_id}|{date_str}|{version}|{row['file_id']}"
    try:
        with _restore_lock(key):
            status = _destination_status(destination, checksum)

            if status is True:
                _register(row, station_id, date_str, version, relative_path)
                _remove_legacy_source(source, checksum, station_id)
                return destination

            if status is False and is_safe_component(station_id):
                raise ArchiveCorrupt(
                    "存档文件校验和与版本登记不吻合，且该版本没有可恢复的"
                    "历史原文件"
                )

            try:
                source_is_file = source.is_file()
            except OSError as exc:
                raise ArchiveRecoveryFailed(
                    f"无法检查留存原文件，可重试：{exc}"
                ) from exc

            if not source_is_file:
                if status is False:
                    raise ArchiveCorrupt(
                        "存档文件校验和不吻合，且历史原文件已缺失，无法恢复"
                    )
                raise ArchiveMissing(
                    f"留存原文件缺失，无法恢复：{relative_path}"
                )

            _copy_verified(source, destination, checksum)
            _register(row, station_id, date_str, version, relative_path)
            _remove_legacy_source(source, checksum, station_id)
            return destination
    except ArchiveError:
        raise
    except OSError as exc:
        raise ArchiveRecoveryFailed(
            f"存档恢复失败，原文件与版本登记已保留，可重试：{exc}"
        ) from exc


def _remove_legacy_source(source: Path, checksum: str, station_id: str) -> None:
    """Best-effort cleanup of a verified out-of-tree legacy file.

    Never delete anything whose bytes do not match the registered checksum,
    and never delete a file that is already inside the retention directory.
    Failures are harmless: the archive is already restored in-tree.
    """
    if is_safe_component(station_id):
        return
    try:
        if _within(source, config.FILES_DIR):
            return
        if not source.is_file():
            return
        if _sha256_file(source) != checksum:
            return
    except OSError:
        return
    _unlink_quiet(source)
