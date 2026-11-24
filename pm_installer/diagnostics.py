"""Диагностика сети: понятные причины сетевых ошибок и экран «Проверка сети»."""
import re
import socket
import time
from dataclasses import dataclass
from typing import Callable, List, Optional, Sequence
from urllib.parse import urlparse

import requests

DEFAULT_TARGETS = ("https://cloud-api.yandex.net/", "https://drive.google.com/")
MIN_SANE_YEAR = 2026   # год сборки; на консолях без батарейки часы часто показывают 1970/2023

_DNS = ("name or service not known", "temporary failure in name resolution", "nodename nor servname",
        "getaddrinfo failed", "failed to resolve", "nameresolutionerror", "no address associated")
_UNREACHABLE = ("network is unreachable", "no route to host", "network unreachable")


def _detail(e: BaseException) -> str:
    """Короткая техническая причина: «Caused by …» из urllib3, иначе само сообщение."""
    text = str(e)
    m = re.search(r"Caused by (.+?)\)*$", text)
    text = (m.group(1) if m else text).replace("\n", " ")
    return f"{type(e).__name__}: {text}"[:150]


def describe_network_error(e: BaseException, url: str = "") -> str:
    """Человеческое объяснение сетевой ошибки + техническая причина в скобках."""
    host = urlparse(url).hostname or "сервером"
    low = str(e).lower()
    if isinstance(e, requests.exceptions.SSLError) or ("certificate" in low and "verify" in low):
        base = (f"Ошибка защищённого соединения с {host}. Чаще всего на консоли неверные дата и время "
                f"(сейчас {time.strftime('%Y-%m-%d %H:%M')}) — установите правильные.")
    elif any(m in low for m in _DNS):
        base = f"Не удаётся найти сервер {host} (DNS). Проверьте, что Wi-Fi подключён и интернет работает."
    elif any(m in low for m in _UNREACHABLE):
        base = "Сеть недоступна. Включите и подключите Wi-Fi."
    elif "refused" in low:
        base = f"Сервер {host} отклонил соединение."
    elif isinstance(e, requests.exceptions.Timeout) or "timed out" in low:
        base = f"Сервер {host} не отвечает (таймаут). Проверьте Wi-Fi и повторите."
    else:
        base = f"Нет соединения с {host}. Проверьте Wi-Fi и повторите."
    return f"{base} [{_detail(e)}]"


@dataclass
class Check:
    name: str
    status: str      # ok | warn | fail
    detail: str


def origin_of(url: str) -> Optional[str]:
    p = urlparse((url or "").strip())
    return f"{p.scheme}://{p.netloc}/" if p.scheme in ("http", "https") and p.netloc else None


def run_checks(targets: Sequence[str] = DEFAULT_TARGETS,
               on_progress: Optional[Callable[[List[Check]], None]] = None) -> List[Check]:
    """Часы → сертификаты → для каждой цели DNS, TCP, HTTP(S). Блокирует: вызывать из потока."""
    out: List[Check] = []

    def add(name: str, status: str, detail: str) -> None:
        out.append(Check(name, status, detail))
        if on_progress:
            on_progress(list(out))

    now = time.localtime()
    sane = now.tm_year >= MIN_SANE_YEAR
    add("Дата и время", "ok" if sane else "warn",
        time.strftime("%Y-%m-%d %H:%M", now) + ("" if sane else " — похоже на неверное, HTTPS может не работать"))

    try:
        import certifi
        import os
        path = certifi.where()
        add("Сертификаты", "ok" if os.path.isfile(path) else "fail", path)
    except ImportError:
        add("Сертификаты", "warn", "certifi не найден — используются системные")

    for url in targets:
        p = urlparse(url)
        host, port = p.hostname or "", p.port or (443 if p.scheme == "https" else 80)
        try:
            ip = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)[0][4][0]
            add(f"DNS {host}", "ok", ip)
        except OSError as e:
            add(f"DNS {host}", "fail", f"{e}. Wi-Fi подключён?")
            continue
        try:
            socket.create_connection((host, port), timeout=8).close()
            add(f"Соединение {host}", "ok", f"порт {port} доступен")
        except OSError as e:
            add(f"Соединение {host}", "fail", str(e))
            continue
        try:
            r = requests.get(url, timeout=(8, 10), stream=True)
            code = r.status_code
            r.close()
            add(f"{'HTTPS' if p.scheme == 'https' else 'HTTP'} {host}", "ok", f"ответ HTTP {code}")
        except requests.RequestException as e:
            add(f"{'HTTPS' if p.scheme == 'https' else 'HTTP'} {host}", "fail", describe_network_error(e, url))
    return out
