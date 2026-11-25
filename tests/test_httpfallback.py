import http.server
import os
import socket
import sys
import threading
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests  # noqa: E402

from pm_installer import httpfallback as H  # noqa: E402
from pm_installer.clouds.direct import DirectLink  # noqa: E402
from pm_installer.diagnostics import describe_network_error  # noqa: E402

BIG = os.urandom(300_000)


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/json"):
            body = b'{"href": "x", "q": "%s"}' % self.path.split("?", 1)[-1].encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
        elif self.path == "/redir":
            self.send_response(302)
            self.send_header("Location", "/big.zip")
            self.send_header("Set-Cookie", "k=v")
            body = b""
        elif self.path == "/big.zip":
            body = BIG
            self.send_response(200)
            self.send_header("Content-Disposition", 'attachment; filename="port.zip"')
        elif self.path == "/cookie":
            body = (self.headers.get("Cookie") or "none").encode()
            self.send_response(200)
        else:
            body = b"nope"
            self.send_response(404)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


@unittest.skipUnless(H.curl_path(), "curl не установлен")
class CurlSessionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.srv.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def setUp(self):
        self.s = H.CurlSession()
        self.s.headers["User-Agent"] = "T/1"

    def test_json_params(self):
        r = self.s.request("GET", self.base + "/json", params={"a": "б в"}, timeout=(5, 5))
        self.assertEqual(r.status_code, 200)
        self.assertIn("q", r.json())
        self.assertIn("%D0%B1", r.json()["q"])

    def test_404_is_not_exception(self):
        r = self.s.request("GET", self.base + "/zzz", timeout=(5, 5))
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.text, "nope")

    def test_stream_with_redirect_and_headers(self):
        r = self.s.request("GET", self.base + "/redir", stream=True, timeout=(5, 5))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers["content-disposition"], 'attachment; filename="port.zip"')
        data = b"".join(r.iter_content(65536))
        r.close()
        self.assertEqual(data, BIG)
        self.assertTrue(r.url.endswith("/big.zip"))

    def test_cookies_persist(self):
        self.s.request("GET", self.base + "/redir", timeout=(5, 5))
        self.assertEqual(self.s.request("GET", self.base + "/cookie", timeout=(5, 5)).text, "k=v")

    def test_connection_refused_maps_to_requests_error(self):
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        with self.assertRaises(requests.ConnectionError) as cm:
            self.s.request("GET", f"http://127.0.0.1:{port}/", timeout=(3, 3))
        self.assertIn("отклонил", describe_network_error(cm.exception, "http://x/"))

    def test_dns_error_maps(self):
        with self.assertRaises(requests.ConnectionError) as cm:
            self.s.request("GET", "http://no-such-host.invalid/", timeout=(3, 3))
        self.assertIn("DNS", describe_network_error(cm.exception, "http://no-such-host.invalid/"))

    def test_stream_dns_error(self):
        with self.assertRaises(requests.ConnectionError):
            self.s.request("GET", "http://no-such-host.invalid/", stream=True, timeout=(3, 3))

    def test_provider_download_via_curl(self):
        import tempfile
        from pathlib import Path
        prov = DirectLink(session=self.s)
        remote = prov.list_files(self.base + "/redir")[0]
        with tempfile.TemporaryDirectory() as d:
            p = prov.download(remote, Path(d))
            self.assertEqual(p.name, "port.zip")
            self.assertEqual(p.read_bytes(), BIG)


class Selection(unittest.TestCase):
    def test_curl_when_no_ssl(self):
        with mock.patch.object(H, "ssl_available", return_value=False), \
                mock.patch.object(H, "curl_path", return_value="/usr/bin/curl"):
            self.assertIsInstance(H.default_session(), H.CurlSession)

    def test_requests_when_ssl(self):
        with mock.patch.object(H, "ssl_available", return_value=True):
            self.assertIsInstance(H.default_session(), requests.Session)

    def test_no_ssl_no_curl_falls_back_to_requests(self):
        with mock.patch.object(H, "ssl_available", return_value=False), \
                mock.patch.object(H, "curl_path", return_value=None):
            self.assertIsInstance(H.default_session(), requests.Session)

    def test_message_for_missing_ssl_module(self):
        e = requests.exceptions.SSLError('HTTPSConnectionPool: Can\'t connect to HTTPS URL because the SSL module is not available.')
        m = describe_network_error(e, "https://disk.yandex.ru/d/x")
        self.assertIn("ssl", m)
        self.assertNotIn("неверные дата", m)


if __name__ == "__main__":
    unittest.main()
