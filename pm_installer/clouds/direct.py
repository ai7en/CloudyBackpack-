"""Прямая http(s)-ссылка на архив — запасной провайдер (и пример минимального CloudProvider)."""
import urllib.parse
from typing import List

from ..errors import InvalidLinkError
from . import register
from .base import CloudProvider, RemoteFile, safe_filename


@register
class DirectLink(CloudProvider):
    key = "direct"
    label = "Прямая ссылка"

    @classmethod
    def matches(cls, url: str) -> bool:
        return url.lower().startswith(("http://", "https://"))

    def list_files(self, url: str) -> List[RemoteFile]:
        last = urllib.parse.urlparse(url).path.rsplit("/", 1)[-1]
        name = safe_filename(last) if "." in last else ""
        return [RemoteFile(name, None, {"url": url})]

    def open_stream(self, remote: RemoteFile, start: int = 0):
        r = self._request("GET", remote.meta["url"], stream=True, headers=self.range_headers(start))
        if r.headers.get("Content-Type", "").lower().startswith("text/html"):
            r.close()
            raise InvalidLinkError("По ссылке открывается веб-страница, а не файл. Если это ссылка на облако "
                                   "(Яндекс.Диск, Google Drive, Mail.ru) — выберите тип облака «auto».")
        return r
