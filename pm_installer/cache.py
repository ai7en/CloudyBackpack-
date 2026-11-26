"""Кэш скачанных архивов: <временная папка>/cache. Недокачанные .part лежат в <временная папка>/download."""
import hashlib
import shutil
from pathlib import Path
from typing import Optional, Tuple


def cache_dir(temp_root: Path) -> Path:
    return Path(temp_root) / "cache"


def entry_path(temp_root: Path, source_url: str, ident: str, name: str) -> Path:
    h = hashlib.sha1(f"{source_url}\n{ident}".encode("utf-8", "replace")).hexdigest()[:10]
    return cache_dir(temp_root) / f"{h}__{name}"


def lookup(temp_root: Path, source_url: str, ident: str, name: str, size: Optional[int]) -> Optional[Path]:
    """Файл из кэша, если он есть и размер совпадает с облачным."""
    if not name:
        return None
    p = entry_path(temp_root, source_url, ident, name)
    try:
        if p.is_file() and (not size or p.stat().st_size == size):
            return p
    except OSError:
        pass
    return None


def store(temp_root: Path, source_url: str, ident: str, name: str, downloaded: Path) -> Path:
    """Переносит скачанный архив в кэш (вместо удаления)."""
    dst = entry_path(temp_root, source_url, ident, name or downloaded.name)
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(downloaded), str(dst))
    return dst


def usage(temp_root: Path) -> Tuple[int, int]:
    """(число файлов, байт) в кэше и недокачанных загрузках."""
    n = total = 0
    for d in (cache_dir(temp_root), Path(temp_root) / "download"):
        if d.is_dir():
            for p in d.iterdir():
                if p.is_file():
                    n += 1
                    total += p.stat().st_size
    return n, total


def clear(temp_root: Path) -> int:
    """Удаляет кэш и недокачанные файлы; → освобождённые байты."""
    _, total = usage(temp_root)
    for d in (cache_dir(temp_root), Path(temp_root) / "download"):
        shutil.rmtree(d, ignore_errors=True)
    return total
