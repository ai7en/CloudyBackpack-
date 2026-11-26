"""Яндекс.Диск: REST API публичных ресурсов."""
from typing import List, Optional, Tuple

from ..constants import is_archive_name
from ..errors import InvalidLinkError
from . import register
from .base import CloudProvider, RemoteDir, RemoteFile


@register
class YandexDisk(CloudProvider):
    key = "yandex"
    label = "Яндекс.Диск"
    hosts = ("yadi.sk", "disk.yandex.ru", "disk.yandex.com", "disk.yandex.by",
             "disk.yandex.kz", "disk.yandex.uz", "disk.360.yandex.ru")
    API = "https://cloud-api.yandex.net/v1/disk/public/resources"

    MAX_DEPTH = 3
    MAX_ITEMS = 300

    def list_files(self, url: str) -> List[RemoteFile]:
        out: List[RemoteFile] = []
        self._walk(url, None, "", 0, out)
        return out

    def _walk(self, url: str, path, prefix: str, depth: int, out: List[RemoteFile]) -> None:
        offset = 0
        while len(out) < self.MAX_ITEMS:
            params = {"public_key": url, "limit": 100, "offset": offset}
            if path:
                params["path"] = path
            j = self._request("GET", self.API, params=params).json()
            if j.get("type") == "file":      # ссылка на один файл
                out.append(RemoteFile(j.get("name", ""), j.get("size"),
                                      {"public_key": url, "md5": j.get("md5"), "modified": j.get("modified")}))
                return
            emb = j.get("_embedded") or {}
            items = emb.get("items", [])
            for it in items:
                name = it.get("name", "")
                if it.get("type") == "file" and is_archive_name(name):
                    out.append(RemoteFile(name, it.get("size"), {
                        "public_key": url, "path": it.get("path"), "folder": True,
                        "display": prefix + name, "md5": it.get("md5"), "modified": it.get("modified")}))
                elif it.get("type") == "dir" and depth < self.MAX_DEPTH:
                    self._walk(url, it.get("path"), f"{prefix}{name}/", depth + 1, out)
            offset += len(items)
            if not items or offset >= emb.get("total", 0):
                break

    def list_dir(self, url: str, path: Optional[str] = None,
                 prefix: str = "") -> Tuple[List[RemoteDir], List[RemoteFile]]:
        dirs: List[RemoteDir] = []
        files: List[RemoteFile] = []
        offset = 0
        while offset < 2000:
            params = {"public_key": url, "limit": 100, "offset": offset}
            if path:
                params["path"] = path
            j = self._request("GET", self.API, params=params).json()
            if j.get("type") == "file":
                return [], [RemoteFile(j.get("name", ""), j.get("size"),
                                       {"public_key": url, "md5": j.get("md5"), "modified": j.get("modified")})]
            emb = j.get("_embedded") or {}
            items = emb.get("items", [])
            for it in items:
                name = it.get("name", "")
                if it.get("type") == "dir":
                    dirs.append(RemoteDir(name, it.get("path"), {"prefix": f"{prefix}{name}/"}))
                elif is_archive_name(name):
                    files.append(RemoteFile(name, it.get("size"), {
                        "public_key": url, "path": it.get("path"), "folder": True,
                        "display": prefix + name, "md5": it.get("md5"), "modified": it.get("modified")}))
            offset += len(items)
            if not items or offset >= emb.get("total", 0):
                break
        return dirs, files

    def open_stream(self, remote: RemoteFile, start: int = 0):
        params = {"public_key": remote.meta["public_key"]}
        if remote.meta.get("path"):
            params["path"] = remote.meta["path"]
        href = self._request("GET", f"{self.API}/download", params=params).json().get("href")
        if not href:
            raise InvalidLinkError("Яндекс.Диск не вернул ссылку на скачивание.")
        return self._request("GET", href, stream=True, headers=self.range_headers(start))
