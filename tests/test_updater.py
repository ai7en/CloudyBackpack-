import hashlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pm_installer import gui as G, updater  # noqa: E402
from pm_installer.errors import NetworkError  # noqa: E402
from pm_installer.settings import SettingsStore  # noqa: E402
from test_manager import FakeBackend, PROFILES  # noqa: E402


class R:
    def __init__(self, body=b"", status=200):
        self.body, self.status_code = body, status

    def json(self):
        return json.loads(self.body)

    def iter_content(self, n):
        yield self.body

    def close(self):
        pass


class Sess:
    def __init__(self, routes):
        self.routes = routes

    def request(self, method, url, **kw):
        return self.routes[url]


def make_zip(files):
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        for k, v in files.items():
            zi = zipfile.ZipInfo(k)
            zi.external_attr = (0o100755 if k.endswith(".sh") else 0o100644) << 16
            z.writestr(zi, v)
    return b.getvalue()


NEW = {"Cloud Port Installer.sh": "#!/bin/bash\necho new\n",
       "cloudportinstaller/main.py": "print('new')\n",
       "cloudportinstaller/pm_installer/__init__.py": "x=2\n",
       "cloudportinstaller/pm_installer/fresh.py": "y=1\n",
       "cloudportinstaller/config/paths.json": "{\"new\": 1}",
       "cloudportinstaller/data/settings.json": "{}"}
URL = "https://h/release/VERSION.json"


class UpdaterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="pmup_"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.app = self.tmp / "app"
        (self.app / "pm_installer").mkdir(parents=True)
        (self.app / "config").mkdir()
        (self.app / "data").mkdir()
        (self.app / "main.py").write_text("print('old')\n")
        (self.app / "pm_installer" / "__init__.py").write_text("x=1\n")
        (self.app / "pm_installer" / "stale.py").write_text("gone\n")
        (self.app / "config" / "paths.json").write_text("{\"mine\": 1}")
        (self.app / "data" / "settings.json").write_text("{\"keep\": 1}")
        self.script = self.tmp / "Cloud Port Installer.sh"
        self.script.write_text("old launcher")
        self.zip = make_zip(NEW)

    def sess(self, version="9.0.0", sha=None, zbytes=None):
        z = self.zip if zbytes is None else zbytes
        meta = {"version": version, "zip": "cloudportinstaller.zip", "sha256": sha or hashlib.sha256(z).hexdigest(),
                "notes": "n"}
        return Sess({URL: R(json.dumps(meta).encode()), "https://h/release/cloudportinstaller.zip": R(z)})

    def test_version_compare(self):
        self.assertTrue(updater.parse_version("1.10.0") > updater.parse_version("1.9.9"))
        self.assertEqual(updater.parse_version("2"), (2, 0, 0, 0))

    def test_check_newer_and_latest(self):
        self.assertTrue(updater.check(self.sess("9.0.0"), URL).newer)
        self.assertFalse(updater.check(self.sess("0.0.1"), URL).newer)

    def test_check_errors(self):
        with self.assertRaises(updater.UpdateError):
            updater.check(Sess({URL: R(b"{}")}), URL)
        with self.assertRaises(updater.UpdateError):
            updater.check(Sess({URL: R(b"", 404)}), URL)
        with self.assertRaises(updater.UpdateError):
            updater.check(Sess({URL: R(json.dumps({"version": "2"}).encode())}), URL)

    def test_apply_replaces_code_keeps_user_files_and_backs_up(self):
        s = self.sess()
        info = updater.check(s, URL)
        files = updater.apply(s, info, self.app, self.tmp / "backup", self.script)
        self.assertEqual((self.app / "main.py").read_text(), "print('new')\n")
        self.assertTrue((self.app / "pm_installer" / "fresh.py").exists())
        self.assertFalse((self.app / "pm_installer" / "stale.py").exists(), "устаревший модуль удалён")
        self.assertEqual((self.app / "config" / "paths.json").read_text(), "{\"mine\": 1}")
        self.assertEqual((self.app / "data" / "settings.json").read_text(), "{\"keep\": 1}")
        self.assertIn("echo new", self.script.read_text())
        self.assertTrue(os.access(self.script, os.X_OK))
        self.assertNotIn("config/paths.json", files)
        bk = next((self.tmp / "backup").iterdir())
        self.assertEqual((bk / "main.py").read_text(), "print('old')\n")
        self.assertEqual(list(self.app.rglob("*.new")), [])

    def test_bad_checksum_changes_nothing(self):
        s = self.sess(sha="0" * 64)
        with self.assertRaises(updater.UpdateError):
            updater.apply(s, updater.check(s, URL), self.app, self.tmp / "backup", self.script)
        self.assertEqual((self.app / "main.py").read_text(), "print('old')\n")

    def test_zip_without_code_rejected_and_slip_blocked(self):
        z = make_zip({"readme.txt": "x"})
        s = self.sess(zbytes=z)
        with self.assertRaises(updater.UpdateError):
            updater.apply(s, updater.check(s, URL), self.app, self.tmp / "backup")
        bad = dict(NEW)
        bad["cloudportinstaller/../../evil.py"] = "boom"
        s = self.sess(zbytes=make_zip(bad))
        with self.assertRaises(Exception):
            updater.apply(s, updater.check(s, URL), self.app, self.tmp / "backup")
        self.assertFalse((self.tmp / "evil.py").exists())
        self.assertEqual((self.app / "main.py").read_text(), "print('old')\n")

    def test_screen_flow(self):
        store = SettingsStore(self.tmp / "home")
        st = store.load()
        st.update_url = URL
        be = FakeBackend()
        g = G.Gui(st, store, PROFILES, be, str(self.tmp / "root"), font_path="x.ttf")
        import time
        with mock.patch.object(G, "default_session", return_value=self.sess()), \
                mock.patch.object(G.updater, "apply", return_value=["main.py"]) as ap:
            g.push(G.UpdateScreen(g))
            scr = g.top
            for _ in range(100):
                g.step()
                if scr.state != "checking":
                    break
                time.sleep(0.02)
            self.assertEqual(scr.state, "ready")
            be.queue += ["ok"]
            for _ in range(100):
                g.step()
                if scr.state == "done":
                    break
                time.sleep(0.02)
            self.assertEqual(scr.state, "done")
            ap.assert_called_once()
            be.queue += ["ok"]
            g.step()
            self.assertFalse(g.running)


if __name__ == "__main__":
    unittest.main()
