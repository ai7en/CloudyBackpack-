"""Свободное место: проверка до загрузки и распаковки, чтобы не падать посреди операции."""
import os
import shutil
from pathlib import Path
from typing import Optional

from .errors import InstallerError

RESERVE = 8 * 1024 * 1024          # запас на служебные файлы и округления
MARGIN = 1.02


class NoSpaceError(InstallerError):
    """Не хватает места на диске."""


def _existing(path) -> Optional[Path]:
    p = Path(path)
    while True:
        if p.exists():
            return p
        if p.parent == p:
            return None
        p = p.parent


def free_bytes(path) -> Optional[int]:
    """Свободно на разделе, где лежит (или будет лежать) path; None, если определить нельзя."""
    p = _existing(path)
    if p is None:
        return None
    try:
        return shutil.disk_usage(p).free
    except OSError:
        return None


def same_device(a, b) -> bool:
    pa, pb = _existing(a), _existing(b)
    if pa is None or pb is None:
        return False
    try:
        return os.stat(pa).st_dev == os.stat(pb).st_dev
    except OSError:
        return False


def needed(size: int) -> int:
    return int(size * MARGIN) + RESERVE


def check(path, need: int, what: str) -> None:
    """NoSpaceError, если на разделе path свободно меньше need байт."""
    from .manager import format_size
    free = free_bytes(path)
    if free is not None and free < need:
        raise NoSpaceError(
            f"Недостаточно места {what}: нужно примерно {format_size(need)}, свободно {format_size(free)} "
            f"({path}). Освободите место (удалите ненужные порты в «Установленные порты») "
            "или выберите другую временную папку в настройках.")
