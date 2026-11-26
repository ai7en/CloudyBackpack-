"""Абстрактный интерфейс облака. Новое облако = новый подкласс CloudProvider + @register."""
import logging
import os
import re
import threading
import time
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


@dataclass
class RemoteDir:
    name: str = ""
    path: Optional[str] = None                 # идентификатор папки у провайдера
    meta: dict = field(default_factory=dict)   # prefix — относительный путь для имён файлов внутри


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

    def list_dir(self, url: str, path: Optional[str] = None,
                 prefix: str = "") -> Tuple[List[RemoteDir], List[RemoteFile]]:
        """Содержимое одной папки: (подпапки, файлы). По умолчанию — плоский список без папок."""
        return [], self.list_files(url)

    @abstractmethod
    def open_stream(self, remote: RemoteFile, start: int = 0) -> requests.Response:
        """Потоковый ответ с содержимым файла; start > 0 — запросить с этого байта (докачка, заголовок Range)."""

    @staticmethod
    def range_headers(start: int) -> Optional[dict]:
        return {"Range": f"bytes={int(start)}-"} if start > 0 else None

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

    RETRIES = 3

    @staticmethod
    def _identity(remote: RemoteFile) -> str:
        m = remote.meta
        return "|".join(str(x) for x in (remote.size, m.get("md5"), m.get("modified"), m.get("path"), m.get("id")))

    def download(self, remote: RemoteFile, dest_dir: Path, progress: Optional[ProgressCB] = None,
                 cancel: Optional[threading.Event] = None,
                 on_retry: Optional[Callable[[int, int, str], None]] = None) -> Path:
        """Скачивает файл. Обрыв связи — до RETRIES повторов с докачкой (Range) из .part;
        недокачанный .part остаётся на диске и подхватывается при следующей попытке."""
        dest_dir = Path(dest_dir)
        try:
            dest_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            raise wrap_oserror(e, dest_dir) from e
        attempt = 0
        while True:
            try:
                return self._download_once(remote, dest_dir, progress, cancel)
            except NetworkError as e:
                attempt += 1
                if attempt > self.RETRIES or (cancel is not None and cancel.is_set()):
                    raise
                log.warning("Повтор загрузки %d/%d: %s", attempt, self.RETRIES, e)
                if on_retry:
                    on_retry(attempt, self.RETRIES, str(e))
                for _ in range(attempt * 4):               # пауза 2, 4, 6 с, прерываемая отменой
                    if cancel is not None and cancel.is_set():
                        raise CancelledError("Загрузка отменена.")
                    time.sleep(0.5)

    def _open_resumable(self, remote: RemoteFile, start: int):
        """→ (ответ, с какого байта реально идёт содержимое)."""
        try:
            resp = self.open_stream(remote, start) if start else self.open_stream(remote)
        except NetworkError as e:
            if start and "416" in str(e):                   # сервер: диапазон за пределами — начинаем заново
                return self.open_stream(remote), 0
            raise
        if start and resp.status_code != 206:               # Range проигнорирован — придёт весь файл
            return resp, 0
        return resp, start

    def _download_once(self, remote, dest_dir, progress, cancel) -> Path:
        ident = self._identity(remote)
        part = tgt = marker = None
        start = 0
        if remote.name:                                     # имя известно заранее → можно докачивать
            tgt = dest_dir / safe_filename(remote.name)
            part = dest_dir / (tgt.name + ".part")
            marker = dest_dir / (tgt.name + ".part.id")
            try:
                if part.is_file() and marker.read_text("utf-8") == ident and \
                        (not remote.size or part.stat().st_size < remote.size):
                    start = part.stat().st_size
            except OSError:
                start = 0
        resp, start = self._open_resumable(remote, start)
        keep = False
        try:
            if tgt is None:
                name = safe_filename(filename_from_headers(resp)
                                     or urllib.parse.urlparse(resp.url).path.rsplit("/", 1)[-1])
                tgt = dest_dir / name
                part = dest_dir / (name + ".part")
                marker = dest_dir / (name + ".part.id")
            total = remote.size
            if not total:
                m = re.search(r"/(\d+)\s*$", resp.headers.get("Content-Range", ""))
                try:
                    total = int(m.group(1)) if m else (int(resp.headers.get("Content-Length") or 0) + start) or None
                except ValueError:
                    total = None
            done = start
            try:
                marker.write_text(ident, "utf-8")
                with open(part, "ab" if start else "wb") as f:
                    for chunk in resp.iter_content(CHUNK):
                        if cancel is not None and cancel.is_set():
                            raise CancelledError("Загрузка отменена.")
                        if chunk:
                            f.write(chunk)
                            done += len(chunk)
                            if progress:
                                progress(done, total)
            except requests.RequestException as e:
                keep = True                                  # .part остаётся для докачки
                log.warning("Обрыв загрузки: %r", e)
                raise NetworkError("Загрузка оборвалась: " + describe_network_error(e, getattr(resp, "url", ""))) from e
            except OSError as e:
                raise wrap_oserror(e, part) from e
            if total and done < total and not resp.headers.get("Content-Encoding"):
                keep = True
                raise NetworkError(f"Загрузка оборвалась ({done} из {total} байт).")
            os.replace(part, tgt)
            part = None
            return tgt
        finally:
            resp.close()
            if part is not None and not keep:
                for f in (part, marker):
                    try:
                        f.unlink()
                    except OSError:
                        pass
            if part is None and marker is not None:
                try:
                    marker.unlink()
                except OSError:
                    pass
