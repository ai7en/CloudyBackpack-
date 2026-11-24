"""Профили прошивок: словарь путей, автоопределение, подготовка целевых папок."""
import json
import logging
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from .errors import InstallerError, PermissionDeniedError, wrap_oserror

log = logging.getLogger("pm_installer.firmware")


@dataclass(frozen=True)
class FirmwareProfile:
    key: str
    label: str
    port_dirs: Tuple[str, ...]      # кандидаты для папок портов (по приоритету)
    script_dirs: Tuple[str, ...]    # кандидаты для .sh-файлов
    os_release: Tuple[str, ...] = ()   # ключевые слова в /etc/os-release
    markers: Tuple[str, ...] = ()      # пути-признаки прошивки


@dataclass(frozen=True)
class Targets:
    port_dir: Path
    script_dir: Path


def rooted(path: str, root: str = "") -> Path:
    """Путь внутри альтернативного корня (нужен для тестов и dry-run)."""
    return Path(root + path) if root else Path(path)


# ---------------------------------------------------------------- загрузка словаря

def _str_list(v, key: str, field: str) -> Tuple[str, ...]:
    if not isinstance(v, (list, tuple)) or not v or not all(isinstance(x, str) and x.startswith("/") for x in v):
        raise InstallerError(f"Профиль «{key}»: поле {field} должно быть непустым списком абсолютных путей")
    return tuple(v)


def load_profiles(*config_files: Optional[Path]) -> Dict[str, FirmwareProfile]:
    """Читает paths.json (+ пользовательские файлы поверх). Профиль может иметь "extends"."""
    raw: Dict[str, dict] = {}
    for f in config_files:
        if not f or not Path(f).is_file():
            continue
        try:
            data = json.loads(Path(f).read_text("utf-8"))
        except (OSError, ValueError) as e:
            raise InstallerError(f"Не удалось прочитать конфигурацию путей {f}: {e}") from e
        for key, val in (data.get("firmwares") or {}).items():
            raw[key] = {**raw.get(key, {}), **val}

    built: Dict[str, FirmwareProfile] = {}

    def build(key: str, stack: Tuple[str, ...] = ()) -> FirmwareProfile:
        if key in built:
            return built[key]
        if key in stack:
            raise InstallerError(f"Циклическое «extends» в профиле «{key}»")
        d = raw[key]
        base: dict = {}
        if d.get("extends"):
            parent = d["extends"]
            if parent not in raw:
                raise InstallerError(f"Профиль «{key}» наследует неизвестный «{parent}»")
            p = build(parent, stack + (key,))
            # detect не наследуется: иначе родитель и потомок определялись бы неоднозначно
            base = {"label": p.label, "port_dirs": p.port_dirs, "script_dirs": p.script_dirs}
        merged = {**base, **{k: v for k, v in d.items() if k != "extends"}}
        det = merged.get("detect") or {}
        built[key] = FirmwareProfile(
            key=key,
            label=str(merged.get("label", key)),
            port_dirs=_str_list(merged.get("port_dirs"), key, "port_dirs"),
            script_dirs=_str_list(merged.get("script_dirs"), key, "script_dirs"),
            os_release=tuple(s.lower() for s in det.get("os_release", [])),
            markers=tuple(det.get("markers", [])),
        )
        return built[key]

    for k in raw:
        build(k)
    if not built:
        raise InstallerError("Словарь путей прошивок пуст (проверьте config/paths.json)")
    return built


# ---------------------------------------------------------------- автоопределение

def _read_os_release(root: str) -> str:
    for p in ("/etc/os-release", "/usr/lib/os-release"):
        try:
            return rooted(p, root).read_text("utf-8", "replace").lower()
        except OSError:
            continue
    return ""


def env_cfw_name(root: str = "") -> str:
    """Имя прошивки от PortMaster (control.txt задаёт CFW_NAME). С --root не используется."""
    return "" if root else os.environ.get("CFW_NAME", "").strip()


def detect(profiles: Dict[str, FirmwareProfile], root: str = "", cfw_name: str = "") -> Optional[str]:
    """Ключ прошивки или None, если определить однозначно не удалось."""
    osr = _read_os_release(root)
    cfw = cfw_name.strip().lower()
    scores: List[Tuple[int, str]] = []
    for key, p in profiles.items():
        score = 0
        if cfw and (cfw == key.lower() or cfw == p.label.lower() or cfw in p.os_release):
            score += 5   # прямая подсказка от PortMaster надёжнее эвристик
        if any(kw in osr for kw in p.os_release):
            score += 3
        if any(rooted(m, root).exists() for m in p.markers):
            score += 1
        if score:
            scores.append((score, key))
    scores.sort(reverse=True)
    if not scores or (len(scores) > 1 and scores[0][0] == scores[1][0]):
        return None
    return scores[0][1]


# ---------------------------------------------------------------- пути

def pick_dir(candidates: Iterable[str], root: str = "") -> Path:
    """Существующий кандидат → кандидат с существующим родителем → первый."""
    paths = [rooted(c, root) for c in candidates]
    for p in paths:
        if p.is_dir():
            return p
    for p in paths:
        if p.parent.is_dir():
            return p
    return paths[0]


def ensure_dir(p: Path, allow_sudo: bool = True) -> None:
    """Создаёт папку (0755). sudo — только для mkdir в системных разделах."""
    if p.is_dir():
        if not os.access(p, os.W_OK | os.X_OK):
            raise PermissionDeniedError(f"Нет прав на запись в {p}.")
        return
    try:
        p.mkdir(mode=0o755, parents=True, exist_ok=True)
        return
    except PermissionError:
        pass
    except OSError as e:
        raise wrap_oserror(e, p) from e
    if allow_sudo and shutil.which("sudo"):
        try:
            r = subprocess.run(["sudo", "-n", "mkdir", "-p", str(p)],
                               capture_output=True, text=True, timeout=20)
            if r.returncode == 0:
                subprocess.run(["sudo", "-n", "chown", f"{os.getuid()}:{os.getgid()}", str(p)],
                               capture_output=True, text=True, timeout=20)
                if os.access(p, os.W_OK | os.X_OK):
                    log.info("Папка %s создана через sudo", p)
                    return
            log.warning("sudo mkdir %s: %s", p, r.stderr.strip())
        except (OSError, subprocess.SubprocessError) as e:
            log.warning("sudo недоступен: %s", e)
    raise PermissionDeniedError(
        f"Не удалось создать {p}: недостаточно прав. Создайте папку вручную или запустите "
        "установщик от пользователя с правом записи.")


def resolve_targets(profile: FirmwareProfile, root: str = "", allow_sudo: bool = True) -> Targets:
    port_dir = pick_dir(profile.port_dirs, root)
    script_dir = pick_dir(profile.script_dirs, root)
    ensure_dir(port_dir, allow_sudo)
    ensure_dir(script_dir, allow_sudo)
    log.info("Пути для %s: порты=%s, скрипты=%s", profile.key, port_dir, script_dir)
    return Targets(port_dir, script_dir)


def describe_targets(profile: FirmwareProfile, root: str = "") -> List[Tuple[str, Path, bool, bool]]:
    """Для экрана «Пути»: (вид, путь, существует, доступен для записи). Ничего не создаёт."""
    out = []
    for kind, cands in (("Папки портов", profile.port_dirs), ("Скрипты .sh", profile.script_dirs)):
        for c in cands:
            p = rooted(c, root)
            out.append((kind, p, p.is_dir(), p.is_dir() and os.access(p, os.W_OK | os.X_OK)))
    return out
