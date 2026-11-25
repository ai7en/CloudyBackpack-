"""Абстрактный интерфейс облака. Новое облако = новый подкласс CloudProvider + @register."""
import logging
import os
import re
import threading
import urllib.parse
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, ClassVar, List, Optional, Tuple

import requests

from ..diagnostics import describe_network_error
from ..httpfallback import default_session
from ..errors import (CancelledError, InvalidLinkError, NetworkError, wrap_oserror)

log = logging.getLogger("pm_installer.net")

ProgressCB = Callable[[int, Optional[int]], None]   # (скачано байт, всего или None)
UA = "Mozilla/5.0 (X11; Linux aarch64) PortMasterInstaller/1.0"
CHUNK = 256 * 1024


@dataclass
class RemoteFile:
    name: str = ""                 # может быть пустым — тогда имя берётся из заголовков при загрузке
    size: Optional[int] = None
    meta: dict = field(default_factory=dict)   # служебные данные провайдера


def filename_from_headers(resp) -> str:
    cd = resp.headers.get("Content-Disposition", "")
    m = re.search(r"filename\*\s*=\s*[\w-]+''([^;]+)", cd, re.I)
    if m:
        return urllib.parse.unquote(m.group(1).strip().strip('"'))
    m = re.search(r'filename\s*=\s*"([^"]+)"', cd, re.I) or re.search(r"filename\s*=\s*([^;]+)", cd, re.I)
    if not m:
        return ""
    name = m.group(1).strip()
    try:  # requests декодирует заголовки как latin-1
        name = name.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        pass
    return name


def safe_filename(name: str) -> str:
    name = urllib.parse.unquote(name or "").replace("\\", "/").split("/")[-1]
    name = re.sub(r'[\x00-\x1f<>:"|?*]', "_", name).strip(" .")
    return name or "download"


class CloudProvider(ABC):
    key: ClassVar[str] = ""
    label: ClassVar[str] = ""
    hosts: ClassVar[Tuple[str, ...]] = ()
    timeout = (10, 60)   # (соединение, чтение), сек

    def __init__(self, session: Optional[requests.Session] = None):
        self.session = session or default_session()
        self.session.headers.setdefault("User-Agent", UA)

    @classmethod
    def matches(cls, url: str) -> bool:
        host = (urllib.parse.urlparse(url).hostname or "").lower()
        return any(host == h or host.endswith("." + h) for h in cls.hosts)

    @abstractmethod
    def list_files(self, url: str) -> List[RemoteFile]:
        """Файлы, доступные по публичной ссылке (для папки — её архивы)."""

    @abstractmethod
    def open_stream(self, remote: RemoteFile) -> requests.Response:
        """Открывает потоковый ответ с содержимым файла."""

    # ------------------------------------------------------------ общее

    def _request(self, method: str, url: str, **kw) -> requests.Response:
        kw.setdefault("timeout", self.timeout)
        try:
            r = self.session.request(method, url, **kw)
        except (requests.ConnectionError, requests.Timeout) as e:
            log.warning("Сетевая ошибка %s %s: %r", method, url, e)
            raise NetworkError(describe_network_error(e, url)) from e
        except requests.RequestException as e:
            raise InvalidLinkError(f"Некорректная ссылка: {e}") from e
        if r.status_code == 404:
            r.close()
            raise InvalidLinkError("Ссылка не найдена (404): файл удалён или ссылка введена неверно.")
        if r.status_code in (401, 403):
            r.close()
            raise InvalidLinkError("Доступ закрыт: ссылка должна быть публичной (доступ «всем, у кого есть ссылка»).")
        if r.status_code == 429:
            r.close()
            raise NetworkError("Сервис ограничил число запросов (429). Подождите и повторите.")
        if r.status_code >= 400:
            r.close()
            raise NetworkError(f"Сервер вернул ошибку HTTP {r.status_code}.")
        return r

    def download(self, remote: RemoteFile, dest_dir: Path, progress: Optional[ProgressCB] = None,
                 cancel: Optional[threading.Event] = None) -> Path:
        dest_dir = Path(dest_dir)
        try:
            dest_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            raise wrap_oserror(e, dest_dir) from e
        resp = self.open_stream(remote)
        part: Optional[Path] = None
        try:
            name = safe_filename(remote.name or filename_from_headers(resp)
                                 or urllib.parse.urlparse(resp.url).path.rsplit("/", 1)[-1])
            try:
                total = remote.size or int(resp.headers.get("Content-Length") or 0) or None
            except ValueError:
                total = None
            target = dest_dir / name
            part = dest_dir / (name + ".part")
            done = 0
            try:
                with open(part, "wb") as f:
                    for chunk in resp.iter_content(CHUNK):
                        if cancel is not None and cancel.is_set():
                            raise CancelledError("Загрузка отменена.")
                        if chunk:
                            f.write(chunk)
                            done += len(chunk)
                            if progress:
                                progress(done, total)
            except requests.RequestException as e:
                log.warning("Обрыв загрузки: %r", e)
                raise NetworkError("Загрузка оборвалась: " + describe_network_error(e, getattr(resp, "url", ""))) from e
            except OSError as e:
                raise wrap_oserror(e, part) from e
            if total and done < total and not resp.headers.get("Content-Encoding"):
                raise NetworkError(f"Загрузка оборвалась ({done} из {total} байт).")
            os.replace(part, target)
            part = None
            return target
        finally:
            resp.close()
            if part is not None:
                try:
                    part.unlink()
                except OSError:
                    pass
