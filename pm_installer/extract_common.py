"""Общее для распаковщиков: прогресс, безопасный путь, ограничитель частоты обновлений."""
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from .errors import UnsupportedArchiveError


@dataclass
class ExtractProgress:
    files_done: int = 0
    files_total: int = 0
    bytes_done: int = 0
    bytes_total: int = 0
    current: str = ""

    @property
    def frac(self) -> float:
        if self.bytes_total > 0:
            return min(1.0, self.bytes_done / self.bytes_total)
        return min(1.0, self.files_done / self.files_total) if self.files_total else -1.0


ProgressCB = Optional[Callable[[ExtractProgress], None]]


class Reporter:
    """Вызывает callback не чаще 10 раз в секунду (GUI не должен захлёбываться на тысячах файлов)."""

    def __init__(self, cb: ProgressCB, interval: float = 0.1):
        self.cb, self.interval, self.last = cb, interval, 0.0
        self.p = ExtractProgress()

    def __call__(self, force: bool = False) -> None:
        if self.cb is None:
            return
        now = time.monotonic()
        if force or now - self.last >= self.interval:
            self.last = now
            self.cb(self.p)


def safe_target(root: Path, name: str) -> Path:
    """Путь внутри root; абсолютные пути и «..» в архиве — ошибка (zip-slip)."""
    clean = name.replace("\\", "/")
    parts = [p for p in clean.split("/") if p not in ("", ".")]
    if clean.startswith("/") or ".." in parts or (parts and len(parts[0]) == 2 and parts[0][1] == ":"):
        raise UnsupportedArchiveError(f"Небезопасный путь в архиве: {name}")
    target = (root / "/".join(parts)).resolve()
    rr = root.resolve()
    if target != rr and rr not in target.parents:
        raise UnsupportedArchiveError(f"Небезопасный путь в архиве: {name}")
    return target
