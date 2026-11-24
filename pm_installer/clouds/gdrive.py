"""Google Drive: файлы — прямой разбор file/d/<ID>; папки — через gdown (если установлен)."""
import html
import os
import re
from typing import Dict, List, Optional, Tuple

from ..constants import is_archive_name
from ..errors import InvalidLinkError, MissingDependencyError
from . import register
from .base import CloudProvider, RemoteFile


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

    def open_stream(self, remote: RemoteFile):
        params = {"id": remote.meta["id"], "export": "download", "confirm": "t"}
        r = self._request("GET", self.DOWNLOAD, params=params, stream=True)
        if not self._is_html(r):
            return r
        # Большие файлы: страница «проверка на вирусы» с формой подтверждения
        page = r.text
        r.close()
        form = self._parse_form(page)
        if form is None:
            raise InvalidLinkError("Google Drive не отдаёт файл: он не публичный или исчерпан лимит скачиваний.")
        action, fields = form
        r = self._request("GET", action, params=fields, stream=True)
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
