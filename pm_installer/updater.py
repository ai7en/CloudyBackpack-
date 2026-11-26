"""Самообновление: сверяет версию с release/VERSION.json в репозитории и ставит новый код поверх текущего.

VERSION.json: {"version": "1.2.0", "zip": "cloudportinstaller.zip", "sha256": "...", "notes": "..."}
(zip — адрес относительно VERSION.json). Папка data/ и config/paths.json пользователя не затрагиваются.
"""
import hashlib
import logging
import os
import re
import shutil
import tempfile
import time
import urllib.parse
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional

import requests

from . import __version__
from .errors import InstallerError, NetworkError
from .extract_common import safe_target

log = logging.getLogger("pm_installer.update")

DEFAULT_URL = "https://raw.githubusercontent.com/ai7en/CloudyBackpack-/main/release/VERSION.json"
PORT_DIR = "cloudportinstaller"
KEEP = ("data/", "config/paths.json", "log.txt")


class UpdateError(InstallerError):
    pass


@dataclass
class UpdateInfo:
    version: str
    zip_url: str
    sha256: str
    notes: str = ""

    @property
    def newer(self) -> bool:
        return parse_version(self.version) > parse_version(__version__)


def parse_version(v: str):
    nums = [int(x) for x in re.findall(r"\d+", str(v))[:4]]
    return tuple(nums + [0] * (4 - len(nums)))


def check(session, url: str = "") -> UpdateInfo:
    url = (url or DEFAULT_URL).strip()
    try:
        r = session.request("GET", url, timeout=(10, 30))
    except requests.RequestException as e:
        raise NetworkError(f"Не удалось проверить обновления: {e}") from e
    if r.status_code != 200:
        raise UpdateError(f"Сервер обновлений ответил HTTP {r.status_code}.")
    try:
        j = r.json()
        info = UpdateInfo(str(j["version"]), urllib.parse.urljoin(url, str(j.get("zip") or f"{PORT_DIR}.zip")),
                          str(j.get("sha256") or "").lower(), str(j.get("notes") or ""))
    except (ValueError, KeyError, TypeError) as e:
        raise UpdateError("Файл версии повреждён.") from e
    if not info.sha256:
        raise UpdateError("В файле версии нет контрольной суммы — обновление отклонено.")
    return info


def _download(session, info: UpdateInfo, dest: Path, progress) -> None:
    try:
        r = session.request("GET", info.zip_url, stream=True, timeout=(10, 60))
    except requests.RequestException as e:
        raise NetworkError(f"Не удалось скачать обновление: {e}") from e
    if r.status_code != 200:
        r.close()
        raise UpdateError(f"Сервер вернул HTTP {r.status_code} при загрузке обновления.")
    h, done = hashlib.sha256(), 0
    try:
        with open(dest, "wb") as f:
            for chunk in r.iter_content(65536):
                f.write(chunk)
                h.update(chunk)
                done += len(chunk)
                progress("Загрузка", done)
    except requests.RequestException as e:
        raise NetworkError(f"Загрузка обновления оборвалась: {e}") from e
    finally:
        r.close()
    if h.hexdigest() != info.sha256:
        raise UpdateError("Контрольная сумма обновления не совпала — файл повреждён или подменён. Не применено.")


def _keep(rel: str) -> bool:
    return any(rel == k or (k.endswith("/") and rel.startswith(k)) for k in KEEP)


def apply(session, info: UpdateInfo, app_root: Path, backup_dir: Path,
          script_path: Optional[Path] = None, progress: Callable[[str, int], None] = lambda *_: None) -> List[str]:
    """Скачивает и применяет обновление. → список обновлённых файлов. Старый код копируется в backup_dir."""
    app_root, backup_dir = Path(app_root), Path(backup_dir)
    tmp = Path(tempfile.mkdtemp(prefix="pm_update_", dir=str(backup_dir.parent if backup_dir.parent.is_dir()
                                                            else app_root)))
    try:
        zpath = tmp / "u.zip"
        _download(session, info, zpath, progress)
        try:
            z = zipfile.ZipFile(zpath)
        except zipfile.BadZipFile as e:
            raise UpdateError("Архив обновления повреждён.") from e
        with z:
            prefix = PORT_DIR + "/"
            members = [m for m in z.infolist() if not m.is_dir()]
            code = [m for m in members if m.filename.startswith(prefix)]
            if not any(m.filename == prefix + "main.py" for m in code) or \
                    not any(m.filename.startswith(prefix + "pm_installer/") for m in code):
                raise UpdateError("В архиве обновления нет кода приложения.")
            progress("Установка", 0)
            stage = tmp / "stage"
            plan = []
            for m in code:
                rel = m.filename[len(prefix):]
                if _keep(rel):
                    continue
                dst = safe_target(stage, rel)
                dst.parent.mkdir(parents=True, exist_ok=True)
                with z.open(m) as src, open(dst, "wb") as out:
                    shutil.copyfileobj(src, out)
                plan.append((rel, dst, (m.external_attr >> 16) & 0o777))
            script_member = next((m for m in members if "/" not in m.filename and m.filename.endswith(".sh")), None)
            new_script = None
            if script_member is not None and script_path is not None:
                new_script = stage / "_launcher.sh"
                new_script.write_bytes(z.read(script_member))
        # резервная копия старого кода
        stamp = time.strftime("%Y%m%d-%H%M%S")
        bk = backup_dir / f"{__version__}-{stamp}"
        for rel, _, _ in plan:
            old = app_root / rel
            if old.is_file():
                (bk / rel).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(old, bk / rel)
        # применение: каждый файл — через временный файл и атомарное переименование
        for rel, src, mode in plan:
            dst = safe_target(app_root, rel)
            dst.parent.mkdir(parents=True, exist_ok=True)
            staged = dst.with_name(dst.name + ".new")
            shutil.copyfile(src, staged)
            os.chmod(staged, mode or 0o644)
            os.replace(staged, dst)
        # удалить устаревшие .py из пакета (их нет в новой версии)
        new_py = {rel for rel, _, _ in plan if rel.endswith(".py")}
        for old in (app_root / "pm_installer").rglob("*.py"):
            rel = old.relative_to(app_root).as_posix()
            if rel not in new_py:
                old.unlink()
        if new_script is not None:
            staged = script_path.with_name(script_path.name + ".new")
            shutil.copyfile(new_script, staged)
            os.chmod(staged, 0o755)
            os.replace(staged, script_path)
        log.info("Обновлено до %s (%d файлов), копия: %s", info.version, len(plan), bk)
        return [rel for rel, _, _ in plan]
    except OSError as e:
        raise UpdateError(f"Не удалось записать обновление: {e}") from e
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
