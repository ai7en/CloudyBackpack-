"""Настройки приложения (JSON)."""
import json
import logging
import os
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import List, Optional
from urllib.parse import urlparse

from .errors import wrap_oserror

log = logging.getLogger("pm_installer.settings")

CONFLICT_POLICIES = ("ask", "overwrite", "skip", "backup")
CONFLICT_LABELS = {
    "ask": "Спрашивать",
    "overwrite": "Перезаписывать",
    "skip": "Пропускать",
    "backup": "Резервная копия",
}


@dataclass
class Settings:
    url: str = ""                   # публичная ссылка на облачную папку/файл
    cloud: str = "auto"             # auto | yandex | gdrive | mailru | direct | ...
    firmware: str = "auto"          # auto | muos | rocknix | knulli | ...
    temp_dir: str = ""              # "" = <data>/tmp (на SD-карте, не в RAM-tmpfs)
    conflict_policy: str = "ask"    # ask | overwrite | skip | backup
    swap_ab: bool = False           # поменять местами A и B на геймпаде
    clouds: list = field(default_factory=list)   # [{"name": ..., "url": ..., "cloud": "auto|yandex|..."}]

    def normalize(self) -> "Settings":
        if self.conflict_policy not in CONFLICT_POLICIES:
            self.conflict_policy = "ask"
        self.swap_ab = bool(self.swap_ab)
        self.url = (self.url or "").strip()
        self.cloud = self.cloud or "auto"
        self.firmware = self.firmware or "auto"
        clean = []
        for c in self.clouds if isinstance(self.clouds, list) else []:
            if isinstance(c, dict) and str(c.get("url", "")).strip():
                clean.append({"name": str(c.get("name", "")).strip(), "url": str(c["url"]).strip(),
                              "cloud": str(c.get("cloud") or "auto")})
        self.clouds = clean
        return self


def cloud_title(entry: dict) -> str:
    """Название облака: заданное пользователем, иначе хост из ссылки."""
    name = (entry.get("name") or "").strip()
    if name:
        return name
    url = (entry.get("url") or "").strip()
    return urlparse(url if "://" in url else "https://" + url).hostname or url or "Облако"


def app_home() -> Path:
    """Каталог данных: $PM_INSTALLER_HOME, иначе <проект>/data, иначе ~/.config/pm_installer."""
    env = os.environ.get("PM_INSTALLER_HOME")
    candidates = [Path(env)] if env else [Path(__file__).resolve().parent.parent / "data"]
    candidates.append(Path.home() / ".config" / "pm_installer")
    for p in candidates:
        try:
            p.mkdir(parents=True, exist_ok=True)
            if os.access(p, os.W_OK):
                return p
        except OSError:
            continue
    return candidates[-1]


class SettingsStore:
    def __init__(self, home: Optional[Path] = None):
        self.home = Path(home) if home else app_home()
        self.path = self.home / "settings.json"

    @property
    def default_temp(self) -> Path:
        return self.home / "tmp"

    @property
    def log_dir(self) -> Path:
        return self.home / "logs"

    def load(self) -> Settings:
        if not self.path.is_file():
            return Settings()
        try:
            data = json.loads(self.path.read_text("utf-8"))
            if not isinstance(data, dict):
                raise ValueError("ожидался объект JSON")
        except (OSError, ValueError) as e:
            log.error("settings.json повреждён (%s) — используются значения по умолчанию", e)
            try:
                os.replace(self.path, self.path.with_suffix(".json.broken"))
            except OSError:
                pass
            return Settings()
        known = {f.name for f in fields(Settings)}
        st = Settings(**{k: v for k, v in data.items() if k in known})
        if "clouds" not in data and str(data.get("url", "")).strip():     # миграция: одна ссылка → список
            st.clouds = [{"name": "", "url": data["url"], "cloud": data.get("cloud") or "auto"}]
        return st.normalize()

    def save(self, s: Settings) -> None:
        s.normalize()
        tmp = self.path.with_suffix(".json.tmp")
        try:
            self.home.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(asdict(s), ensure_ascii=False, indent=2), "utf-8")
            os.replace(tmp, self.path)
        except OSError as e:
            raise wrap_oserror(e, self.path) from e
