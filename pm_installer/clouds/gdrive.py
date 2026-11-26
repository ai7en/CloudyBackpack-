"""Google Drive: файлы — прямой разбор file/d/<ID>; папки — через gdown (если установлен)."""
import html
import os
import re
from typing import Dict, List, Optional, Tuple

from ..constants import is_archive_name
from ..errors import InvalidLinkError, MissingDependencyError
from . import register
from .base import CloudProvider, RemoteDir, RemoteFile


@register
class GoogleDrive(CloudProvider):
    key = "gdrive"
    label = "Google Drive"
    hosts = ("drive.google.com", "docs.google.com", "drive.usercontent.google.com")
    DOWNLOAD = "https://drive.usercontent.google.com/download"

    @staticmethod
    def parse(url: str) -> Tuple[str, str]:
        """→ ("file" | "folder", id)"""
        m = re.search(r"/folders/([\w-]+)", url)
        if m:
            return "folder", m.group(1)
        m = re.search(r"/file/d/([\w-]+)", url) or re.search(r"[?&]id=([\w-]+)", url)
        if m:
            return "file", m.group(1)
        raise InvalidLinkError("В ссылке Google Drive не найден идентификатор файла (ожидается …/file/d/<ID>/…).")

    EMBED = "https://drive.google.com/embeddedfolderview"
    _ENTRY = re.compile(r'<a href="https://drive\.google\.com/(file/d|drive/folders)/([\w-]+)[^"]*"[^>]*>'
                        r'.*?class="flip-entry-title">(.*?)</div>', re.S)

    @classmethod
    def parse_embedded(cls, page: str) -> Tuple[List[Tuple[str, str, str]], int]:
        """→ ([(file|folder, id, имя)], число найденных записей). Разбор страницы embeddedfolderview."""
        out = []
        for kind, id_, title in cls._ENTRY.findall(page):
            out.append(("folder" if kind != "file/d" else "file", id_, html.unescape(re.sub(r"<[^>]+>", "", title)).strip()))
        return out, len(out)

    def list_dir(self, url: str, path: Optional[str] = None,
                 prefix: str = "") -> Tuple[List[RemoteDir], List[RemoteFile]]:
        kind, id_ = self.parse(url)
        if kind == "file":
            return [], [RemoteFile("", None, {"id": id_})]
        folder = path or id_
        r = self._request("GET", self.EMBED, params={"id": folder})
        entries, _ = self.parse_embedded(r.text)
        r.close()
        if not entries:
            if path is None:                      # корень: пробуем gdown, если он есть
                return [], self.list_files(url)
            return [], []
        dirs = [RemoteDir(n, i, {"prefix": f"{prefix}{n}/"}) for k, i, n in entries if k == "folder"]
        files = [RemoteFile(n, None, {"id": i, "display": prefix + n})
                 for k, i, n in entries if k == "file" and is_archive_name(n)]
        return dirs, files

    def list_files(self, url: str) -> List[RemoteFile]:
        kind, id_ = self.parse(url)
        if kind == "file":
            return [RemoteFile("", None, {"id": id_})]   # имя узнаем при скачивании
        try:
            import gdown
        except ImportError as e:
            raise MissingDependencyError(
                "Для папок Google Drive нужна библиотека gdown (pip install gdown) "
                "либо дайте ссылку на отдельный файл.") from e
        try:
            items = gdown.download_folder(url=url, skip_download=True, quiet=True,
                                          use_cookies=False, remaining_ok=True) or []
        except Exception as e:  # gdown бросает разнородные исключения
            raise InvalidLinkError(f"Не удалось прочитать папку Google Drive: {e}") from e
        out = []
        for it in items:
            name = os.path.basename(getattr(it, "path", "") or "")
            if is_archive_name(name):
                out.append(RemoteFile(name, None, {"id": it.id}))
        return out

    def open_stream(self, remote: RemoteFile, start: int = 0):
        params = {"id": remote.meta["id"], "export": "download", "confirm": "t"}
        hdr = self.range_headers(start)
        r = self._request("GET", self.DOWNLOAD, params=params, stream=True, headers=hdr)
        if not self._is_html(r):
            return r
        # Большие файлы: страница «проверка на вирусы» с формой подтверждения
        page = r.text
        r.close()
        form = self._parse_form(page)
        if form is None:
            raise InvalidLinkError("Google Drive не отдаёт файл: он не публичный или исчерпан лимит скачиваний.")
        action, fields = form
        r = self._request("GET", action, params=fields, stream=True, headers=hdr)
        if self._is_html(r):
            r.close()
            raise InvalidLinkError("Google Drive не отдаёт файл: он не публичный или исчерпан лимит скачиваний.")
        return r

    @staticmethod
    def _is_html(resp) -> bool:
        return resp.headers.get("Content-Type", "").lower().startswith("text/html")

    @staticmethod
    def _parse_form(page: str) -> Optional[Tuple[str, Dict[str, str]]]:
        m = (re.search(r'<form[^>]+id="download-form"[^>]*action="([^"]+)"', page)
             or re.search(r'<form[^>]+action="([^"]+)"', page))
        if not m:
            return None
        fields = {html.unescape(k): html.unescape(v) for k, v in
                  re.findall(r'<input[^>]+name="([^"]+)"[^>]+value="([^"]*)"', page)}
        if "id" not in fields:
            return None
        return html.unescape(m.group(1)), fields
