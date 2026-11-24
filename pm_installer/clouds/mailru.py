"""Mail.ru Облако: публичная ссылка на файл (cloud.mail.ru/public/<id>/<имя>).

Схема: со страницы ссылки берётся адрес сервера weblink_get, затем файл качается
по <weblink_get>/<id>/<имя>. Это неофициальный путь: формат страницы может измениться.
"""
import re
import urllib.parse
from typing import List

from ..constants import is_archive_name
from ..errors import InvalidLinkError
from . import register
from .base import CloudProvider, RemoteFile


@register
class MailRuCloud(CloudProvider):
    key = "mailru"
    label = "Mail.ru Облако"
    hosts = ("cloud.mail.ru",)

    @staticmethod
    def parse(url: str) -> str:
        m = re.search(r"cloud\.mail\.ru/public/([^?#]+)", url)
        weblink = urllib.parse.unquote(m.group(1)).strip("/") if m else ""
        if "/" not in weblink:
            raise InvalidLinkError("Ссылка Mail.ru должна иметь вид https://cloud.mail.ru/public/<id>/<имя>.")
        return weblink

    @staticmethod
    def find_base(page: str) -> str:
        m = (re.search(r'"weblink_get"\s*:\s*\[\s*\{[^{}]*?"url"\s*:\s*"([^"]+)"', page)
             or re.search(r'"weblink_get"\s*:\s*"([^"]+)"', page))
        if not m:
            raise InvalidLinkError(
                "Не удалось определить адрес скачивания Mail.ru (формат страницы мог измениться). "
                "Попробуйте другое облако или прямую ссылку.")
        return m.group(1).replace("\\/", "/")

    def list_files(self, url: str) -> List[RemoteFile]:
        weblink = self.parse(url)
        page = self._request("GET", "https://cloud.mail.ru/public/" + urllib.parse.quote(weblink)).text
        if re.search(r'"(?:kind|type)"\s*:\s*"folder"', page):
            raise InvalidLinkError("Папки Mail.ru не поддерживаются — дайте ссылку на отдельный файл-архив.")
        base = self.find_base(page)
        tail = weblink.rsplit("/", 1)[-1]
        return [RemoteFile(tail if is_archive_name(tail) else "", None,
                           {"weblink": weblink, "base": base})]

    def open_stream(self, remote: RemoteFile):
        url = remote.meta["base"].rstrip("/") + "/" + urllib.parse.quote(remote.meta["weblink"], safe="/")
        return self._request("GET", url, stream=True)
