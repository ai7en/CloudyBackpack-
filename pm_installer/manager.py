"""Менеджер установленных портов: поиск .sh и папок, размер, дата, удаление."""
import logging
import os
import re
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

from .constants import APP_FOLDER, APP_SCRIPT
from .errors import InstallerError, wrap_oserror
from .firmware import FirmwareProfile, rooted

log = logging.getLogger("pm_installer.manager")

# Служебные папки/скрипты, которые портами не являются и трогать их нельзя
IGNORED_DIRS = {"portmaster", "images", "videos", "manuals", "themes", "gamelist", "gamelists",
                "autoinstall", "music", "backup", "backups"}
IGNORED_SCRIPTS = {"portmaster.sh"}

_GAMEDIR_RE = re.compile(r"""GAMEDIR\s*=\s*["']?[^\n]*?/ports/([^"'\s/$]+)""", re.I)


def norm(name: str) -> str:
    return re.sub(r"[^0-9a-zа-яё]+", "", name.lower())


def format_size(n: Optional[int]) -> str:
    if n is None:
        return "…"
    if n < 1024:
        return f"{n} Б"
    for unit, div in (("КБ", 1024), ("МБ", 1024 ** 2), ("ГБ", 1024 ** 3)):
        if n < div * 1024 or unit == "ГБ":
            v = n / div
            return f"{v:.0f} {unit}" if v >= 10 else f"{v:.1f} {unit}"
    return str(n)


def format_date(ts: float) -> str:
    return time.strftime("%d.%m.%Y", time.localtime(ts)) if ts > 0 else "—"


@dataclass
class InstalledPort:
    name: str
    script: Optional[Path] = None
    folder: Optional[Path] = None
    size: Optional[int] = None        # None — ещё считается
    mtime: float = 0.0
    protected: bool = False

    @property
    def kind(self) -> str:
        if self.script and self.folder:
            return "sh + папка"
        return "только sh (папки нет)" if self.script else "только папка (нет sh)"

    @property
    def complete(self) -> bool:
        return bool(self.script and self.folder)


def _dir_size(path: Path) -> int:
    total = 0
    for base, dirs, files in os.walk(path):
        for f in files:
            try:
                st = os.lstat(os.path.join(base, f))
                total += st.st_size
            except OSError:
                pass
    return total


def compute_size(port: InstalledPort) -> int:
    n = 0
    if port.folder:
        n += _dir_size(port.folder)
    if port.script:
        try:
            n += port.script.stat().st_size
        except OSError:
            pass
    port.size = n
    return n


def _existing_dirs(candidates, root: str) -> List[Path]:
    out, seen = [], set()
    for c in candidates:
        p = rooted(c, root)
        try:
            if p.is_dir():
                key = os.path.realpath(p)
                if key not in seen:
                    seen.add(key)
                    out.append(p)
        except OSError:
            continue
    return out


def referenced_folder(script: Path) -> Optional[str]:
    """Имя папки, которую .sh использует как GAMEDIR (…/ports/<имя>), если удаётся прочитать."""
    try:
        with open(script, "r", encoding="utf-8", errors="replace") as f:
            text = f.read(65536)
    except OSError:
        return None
    m = _GAMEDIR_RE.search(text)
    return m.group(1) if m else None


def scan(profile: FirmwareProfile, root: str = "") -> List[InstalledPort]:
    """Все порты в существующих папках прошивки. Размеры не считаются (см. compute_size)."""
    script_dirs = _existing_dirs(profile.script_dirs, root)
    port_dirs = _existing_dirs(profile.port_dirs, root)
    scripts: List[Path] = []
    folders: List[Path] = []
    for d in script_dirs:
        try:
            for f in sorted(d.iterdir()):
                if f.suffix.lower() == ".sh" and f.is_file() and f.name.lower() not in IGNORED_SCRIPTS:
                    scripts.append(f)
        except OSError as e:
            log.warning("Не удалось прочитать %s: %s", d, e)
    for d in port_dirs:
        try:
            for f in sorted(d.iterdir()):
                if f.is_dir() and not f.name.startswith(".") and f.name.lower() not in IGNORED_DIRS:
                    folders.append(f)
        except OSError as e:
            log.warning("Не удалось прочитать %s: %s", d, e)

    by_lower: Dict[str, Path] = {}
    by_norm: Dict[str, Path] = {}
    for f in folders:
        by_lower.setdefault(f.name.lower(), f)
        by_norm.setdefault(norm(f.name), f)
    used: set = set()
    ports: List[InstalledPort] = []
    for s in scripts:
        ref = referenced_folder(s)
        folder = None
        for cand in ((by_lower.get(ref.lower()) if ref else None), by_norm.get(norm(s.stem))):
            if cand is not None and cand not in used:
                folder = cand
                break
        if folder is not None:
            used.add(folder)
        ports.append(InstalledPort(s.stem, s, folder))
    ports += [InstalledPort(f.name, None, f) for f in folders if f not in used]

    for p in ports:
        times = []
        for q in (p.folder, p.script):
            if q:
                try:
                    times.append(q.stat().st_mtime)
                except OSError:
                    pass
        p.mtime = max(times) if times else 0.0
        p.protected = (p.script is not None and p.script.name.lower() == APP_SCRIPT.lower()) or \
                      (p.folder is not None and p.folder.name.lower() == APP_FOLDER)
    ports.sort(key=lambda p: p.name.lower())
    return ports


def delete_port(port: InstalledPort, profile: FirmwareProfile, root: str = "") -> int:
    """Удаляет .sh и папку порта. Возвращает освобождённые байты (если размер был известен)."""
    if port.protected:
        raise InstallerError("Это приложение удалить нельзя — оно нужно для установки портов.")
    allowed = [os.path.realpath(d) for d in
               _existing_dirs(list(profile.script_dirs) + list(profile.port_dirs), root)]

    def inside(p: Path) -> bool:
        parent = os.path.realpath(p.parent)
        return parent in allowed

    for p in (port.script, port.folder):
        if p is not None and not inside(p):
            raise InstallerError(f"Отказ: «{p}» вне папок портов этой прошивки.")
    freed = port.size or 0
    try:
        if port.script is not None and (port.script.exists() or port.script.is_symlink()):
            port.script.unlink()
        if port.folder is not None and (port.folder.exists() or port.folder.is_symlink()):
            if port.folder.is_symlink():
                port.folder.unlink()
            else:
                shutil.rmtree(port.folder)
    except OSError as e:
        raise wrap_oserror(e, port.folder or port.script) from e
    log.info("Удалён порт: %s (sh=%s, папка=%s)", port.name, port.script, port.folder)
    return freed
