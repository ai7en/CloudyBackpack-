"""Запасной HTTP-клиент на curl — для рантаймов Python без модуля ssl (как python_3.11 в PortMaster на части прошивок).

CurlSession повторяет ровно то подмножество requests.Session, которое нужно облакам: GET с params/stream/timeout,
cookies между запросами, .json()/.text/.content/.iter_content()/.headers/.status_code/.url/.close().
Ошибки curl превращаются в requests.ConnectionError / ConnectTimeout / SSLError, поэтому остальной код не меняется.
"""
import json as _json
import os
import shutil
import subprocess
import tempfile
import time
import urllib.parse
from typing import Optional

import requests
from requests.structures import CaseInsensitiveDict

_DNS, _REFUSED, _TIMEOUT = 6, 7, 28
_SSL = {35, 51, 53, 54, 58, 59, 60, 64, 66, 77, 80, 82, 83, 90, 91}


def ssl_available() -> bool:
    try:
        import ssl  # noqa: F401
        return True
    except ImportError:
        return False


def curl_path() -> Optional[str]:
    return shutil.which("curl")


def _raise_for_curl(code: int, err: str):
    err = (err or "").strip().splitlines()[-1] if (err or "").strip() else f"curl exit {code}"
    if code == _DNS:
        raise requests.ConnectionError(f"Failed to resolve host: Temporary failure in name resolution ({err})")
    if code == _REFUSED:
        raise requests.ConnectionError(f"Connection refused ({err})")
    if code == _TIMEOUT:
        raise requests.ConnectTimeout(f"Connection timed out ({err})")
    if code in _SSL:
        raise requests.exceptions.SSLError(f"certificate verify failed: {err}")
    raise requests.ConnectionError(f"curl: {err} (код {code})")


def _read(path: str) -> bytes:
    try:
        with open(path, "rb") as f:
            return f.read()
    except OSError:
        return b""


def _parse_headers(raw: bytes):
    """Последний блок заголовков (после редиректов): (status, headers, location_chain_last)."""
    blocks = [b for b in raw.replace(b"\r\n", b"\n").split(b"\n\n") if b.strip()]
    status, headers, last_loc = 0, CaseInsensitiveDict(), None
    for blk in blocks:
        lines = blk.decode("latin-1").split("\n")
        if not lines[0].startswith("HTTP/"):
            continue
        parts = lines[0].split(None, 2)
        status = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
        headers = CaseInsensitiveDict()
        for ln in lines[1:]:
            if ":" in ln:
                k, v = ln.split(":", 1)
                headers[k.strip()] = v.strip()
        if "Location" in headers:
            last_loc = headers["Location"]
    return status, headers, last_loc


class CurlResponse:
    def __init__(self, status, headers, url, body=b"", proc=None, tmpdir=None):
        self.status_code, self.headers, self.url = status, headers, url
        self._body, self._proc, self._tmp = body, proc, tmpdir
        self.encoding = "utf-8"

    @property
    def content(self) -> bytes:
        if self._proc is not None:
            self._body = b"".join(self.iter_content(65536))
        return self._body

    @property
    def text(self) -> str:
        enc = "utf-8"
        ct = self.headers.get("Content-Type", "")
        if "charset=" in ct:
            enc = ct.split("charset=", 1)[1].split(";")[0].strip() or "utf-8"
        return self.content.decode(enc, errors="replace")

    def json(self):
        return _json.loads(self.content.decode("utf-8"))

    def iter_content(self, chunk_size=1):
        if self._proc is None:
            for i in range(0, len(self._body), chunk_size or 1):
                yield self._body[i:i + chunk_size]
            return
        out = self._proc.stdout
        while True:
            chunk = out.read(chunk_size)
            if not chunk:
                break
            yield chunk
        code = self._proc.wait()
        if code != 0:
            try:
                err = self._proc.stderr.read().decode("utf-8", "replace")
            except Exception:  # noqa: BLE001
                err = ""
            self._proc = None
            _raise_for_curl(code, err)

    def close(self):
        p, self._proc = self._proc, None
        if p is not None:
            try:
                p.kill()
            except OSError:
                pass
            for f in (p.stdout, p.stderr):
                try:
                    f.close()
                except Exception:  # noqa: BLE001
                    pass
            try:
                p.wait(timeout=5)
            except Exception:  # noqa: BLE001
                pass
        if self._tmp:
            shutil.rmtree(self._tmp, ignore_errors=True)
            self._tmp = None


def _reap(proc) -> str:
    """Дочитывает stderr и закрывает каналы завершившегося curl."""
    try:
        err = proc.stderr.read().decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        err = ""
    for f in (proc.stdout, proc.stderr):
        try:
            f.close()
        except Exception:  # noqa: BLE001
            pass
    return err


class CurlSession:
    """Минимальный аналог requests.Session поверх curl."""

    def __init__(self):
        self.headers = {}
        self._dir = tempfile.mkdtemp(prefix="pm_curl_")
        self._jar = os.path.join(self._dir, "cookies.txt")

    def __del__(self):
        shutil.rmtree(getattr(self, "_dir", ""), ignore_errors=True)

    def _cmd(self, url, hdr_file, timeout, extra):
        exe = curl_path()
        if not exe:
            raise requests.ConnectionError("curl не найден")
        ct, rt = (timeout if isinstance(timeout, tuple) else (timeout, timeout)) if timeout else (10, 60)
        cmd = [exe, "-sS", "-L", "--max-redirs", "10", "--connect-timeout", str(int(ct)),
               "--speed-time", str(int(rt)), "--speed-limit", "1", "-b", self._jar, "-c", self._jar,
               "-D", hdr_file, "--compressed"]
        for k, v in self.headers.items():
            cmd += ["-H", f"{k}: {v}"]
        return cmd + extra + [url]

    def request(self, method, url, params=None, timeout=None, stream=False, **kw):
        if method.upper() != "GET":
            raise NotImplementedError("CurlSession поддерживает только GET")
        if params:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
        tmp = tempfile.mkdtemp(prefix="pm_req_", dir=self._dir)
        hdr = os.path.join(tmp, "h")
        if not stream:
            body = os.path.join(tmp, "b")
            try:
                r = subprocess.run(self._cmd(url, hdr, timeout, ["-o", body]), capture_output=True)
                if r.returncode != 0:
                    _raise_for_curl(r.returncode, r.stderr.decode("utf-8", "replace"))
                status, headers, loc = _parse_headers(_read(hdr))
                data = _read(body)
            finally:
                shutil.rmtree(tmp, ignore_errors=True)
            return CurlResponse(status, headers, urllib.parse.urljoin(url, loc) if loc else url, data)
        proc = subprocess.Popen(self._cmd(url, hdr, timeout, ["-o", "-"]), stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE)
        deadline = time.time() + (timeout[0] + timeout[1] if isinstance(timeout, tuple) else 60) + 5
        while True:   # ждём конец заголовков финального ответа
            raw = _read(hdr)
            if raw.endswith(b"\r\n\r\n") or raw.endswith(b"\n\n"):
                st, headers, loc = _parse_headers(raw)
                if not (300 <= st < 400):
                    break
            if proc.poll() is not None and not raw:
                err = _reap(proc)
                shutil.rmtree(tmp, ignore_errors=True)
                _raise_for_curl(proc.returncode or 1, err)
            if proc.poll() is not None:     # curl завершился: заголовки полны либо ошибка
                if proc.returncode not in (0, None) and not raw.endswith((b"\r\n\r\n", b"\n\n")):
                    err = _reap(proc)
                    shutil.rmtree(tmp, ignore_errors=True)
                    _raise_for_curl(proc.returncode, err)
                st, headers, loc = _parse_headers(raw)
                break
            if time.time() > deadline:
                proc.kill()
                proc.wait()
                _reap(proc)
                shutil.rmtree(tmp, ignore_errors=True)
                raise requests.ConnectTimeout("Connection timed out (нет ответа)")
            time.sleep(0.02)
        return CurlResponse(st, headers, urllib.parse.urljoin(url, loc) if loc else url, proc=proc, tmpdir=tmp)


def default_session():
    """requests.Session, а если у Python нет ssl и есть curl — CurlSession."""
    if not ssl_available() and curl_path():
        return CurlSession()
    return requests.Session()
