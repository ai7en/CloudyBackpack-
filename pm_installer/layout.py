"""Анализ распакованного архива: где .sh, где папка порта."""
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from .constants import JUNK_NAMES
from .errors import ArchiveLayoutError


@dataclass
class PortLayout:
    root: Path
    scripts: List[Path]
    port_dirs: List[Path]
    primary: Optional[Path]
    ignored: List[Path] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)


def _entries(d: Path) -> List[Path]:
    return [p for p in sorted(d.iterdir())
            if p.name not in JUNK_NAMES and not p.name.startswith("._")]


def _is_sh(p: Path) -> bool:
    return p.is_file() and p.suffix.lower() == ".sh"


def _norm(s: str) -> str:
    return re.sub(r"[\W_]+", "", s.lower())


def _weight(d: Path) -> int:
    return sum(len(files) for _, _, files in os.walk(d))


def analyze(extracted: Path) -> PortLayout:
    root = Path(extracted)
    # Архив мог быть запакован «внутри обёртки»: одна папка без файлов — спускаемся
    for _ in range(3):
        entries = _entries(root)
        if any(_is_sh(p) for p in entries):
            break
        dirs = [p for p in entries if p.is_dir()]
        if len(dirs) == 1 and len(dirs) == len(entries):
            root = dirs[0]
            continue
        break

    entries = _entries(root)
    scripts = [p for p in entries if _is_sh(p)]
    if not scripts:
        raise ArchiveLayoutError(
            "В корне архива не найден .sh-файл запуска — это не похоже на порт PortMaster.")
    dirs = [p for p in entries if p.is_dir()]
    ignored = [p for p in entries if p.is_file() and p not in scripts]

    stems = {_norm(s.stem) for s in scripts}
    matched = [d for d in dirs if _norm(d.name) in stems]
    primary = matched[0] if matched else (max(dirs, key=_weight) if dirs else None)

    warnings: List[str] = []
    if not dirs:
        warnings.append("В архиве нет папки с данными порта — будет установлен только .sh.")
    elif not matched:
        warnings.append(f"Имя папки не совпадает с именем .sh; основной считается «{primary.name}».")
    return PortLayout(root=root, scripts=scripts, port_dirs=dirs, primary=primary,
                      ignored=ignored, warnings=warnings)
