import http.server
import os
import socket
import sys
import threading
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests  # noqa: E402

from pm_installer import diagnostics as D  # noqa: E402


class Describe(unittest.TestCase):
    def d(self, e, url="https://cloud-api.yandex.net/x"):
        return D.describe_network_error(e, url)

    def test_ssl_mentions_clock(self):
        m = self.d(requests.exceptions.SSLError("certificate verify failed: certificate is not yet valid"))
        self.assertIn("дата и время", m)
        self.assertIn("SSLError", m)

    def test_dns(self):
        e = requests.ConnectionError("Failed to resolve 'x' ([Errno -3] Temporary failure in name resolution)")
        self.assertIn("DNS", self.d(e))

    def test_unreachable(self):
        self.assertIn("Wi-Fi", self.d(requests.ConnectionError("[Errno 101] Network is unreachable")))

    def test_refused(self):
        self.assertIn("отклонил", self.d(requests.ConnectionError("[Errno 111] Connection refused")))

    def test_timeout(self):
        self.assertIn("таймаут", self.d(requests.ConnectTimeout("Connection to x timed out")))

    def test_generic(self):
        self.assertIn("Нет соединения", self.d(requests.ConnectionError("weird")))

    def test_origin(self):
        self.assertEqual(D.origin_of("https://a.b/c/d?x=1"), "https://a.b/")
        self.assertIsNone(D.origin_of("not a url"))
        self.assertIsNone(D.origin_of(""))


class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()

    def log_message(self, *a):
        pass


class RunChecks(unittest.TestCase):
    def test_ok_local_server(self):
        srv = http.server.HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        seen = []
        res = D.run_checks([f"http://127.0.0.1:{srv.server_port}/"], on_progress=seen.append)
        self.assertEqual([c.status for c in res if c.name.startswith(("DNS", "Соед", "HTTP"))], ["ok"] * 3)
        self.assertEqual(len(seen), len(res))

    def test_dead_port_fails_at_connect(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        res = D.run_checks([f"http://127.0.0.1:{port}/"])
        self.assertEqual(res[-1].status, "fail")
        self.assertTrue(res[-1].name.startswith("Соединение"))

    def test_bad_clock_warns(self):
        class T:
            tm_year = 1970
        with mock.patch.object(D.time, "localtime", return_value=__import__("time").gmtime(0)):
            res = D.run_checks([])
        self.assertEqual(res[0].status, "warn")


if __name__ == "__main__":
    unittest.main()
