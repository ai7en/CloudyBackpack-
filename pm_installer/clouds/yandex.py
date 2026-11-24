"""Яндекс.Диск: REST API публичных ресурсов."""
from typing import List

from ..constants import is_archive_name
from ..errors import InvalidLinkError
from . import register
from .base import CloudProvider, RemoteFile


@register
class YandexDisk(CloudProvider):
    key = "yandex"
    label = "Яндекс.Диск"
    hosts = ("yadi.sk", "disk.yandex.ru", "disk.yandex.com", "disk.yandex.by",
             "disk.yandex.kz", "disk.yandex.uz", "disk.360.yandex.ru")
    API = "https://cloud-api.yandex.net/v1/disk/public/resources"

    def list_files(self, url: str) -> List[RemoteFile]:
        out: List[RemoteFile] = []
        offset = 0
        while True:
            j = self._request("GET", self.API,
                              params={"public_key": url, "limit": 100, "offset": offset}).json()
            if j.get("type") == "file":
                return [RemoteFile(j.get("name", ""), j.get("size"), {"public_key": url})]
            emb = j.get("_embedded") or {}
            items = emb.get("items", [])
            for it in items:
                if it.get("type") == "file" and is_archive_name(it.get("name", "")):
                    out.append(RemoteFile(it["name"], it.get("size"),
                                          {"public_key": url, "path": it.get("path")}))
            offset += len(items)
            if not items or offset >= emb.get("total", 0):
                break
        return out

    def open_stream(self, remote: RemoteFile):
        params = {"public_key": remote.meta["public_key"]}
        if remote.meta.get("path"):
            params["path"] = remote.meta["path"]
        href = self._request("GET", f"{self.API}/download", params=params).json().get("href")
        if not href:
            raise InvalidLinkError("Яндекс.Диск не вернул ссылку на скачивание.")
        return self._request("GET", href, stream=True)
