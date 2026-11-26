import os
import shutil
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests  # noqa: E402

from pm_installer import cache, gui as G  # noqa: E402
from pm_installer.clouds import RemoteFile  # noqa: E402
from pm_installer.clouds.base import CloudProvider  # noqa: E402
from pm_installer.errors import CancelledError, NetworkError  # noqa: E402
from pm_installer.installer import PortInstaller  # noqa: E402
from pm_installer.settings import Settings, SettingsStore  # noqa: E402
from test_manager import FakeBackend, PROFILES  # noqa: E402

DATA = bytes(range(256)) * 400          # 102400 байт


class Resp:
    def __init__(self, data, status=200, start=0, fail_after=None, headers=None, url="http://x/f.zip"):
        self.data, self.status_code, self.start, self.fail_after = data, status, start, fail_after
        self.headers = dict(headers or {})
        self.url = url
        self.closed = False

    def iter_content(self, n):
        sent = 0
        for i in range(0, len(self.data), 4096):
            if self.fail_after is not None and sent >= self.fail_after:
                raise requests.ConnectionError("connection reset by peer")
            chunk = self.data[i:i + 4096]
            sent += len(chunk)
            yield chunk

    def close(self):
        self.closed = True


class FakeProv(CloudProvider):
    key = "fake"
    label = "fake"

    def __init__(self, script):
        super().__init__(session=mock.Mock(headers={}))
        self.script = list(script)     # функции start -> Resp
        self.starts = []

    def list_files(self, url):
        return []

    def open_stream(self, remote, start=0):
        self.starts.append(start)
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        return step(start)


def partial(start):
    return Resp(DATA[start:], 206, start, headers={"Content-Range": f"bytes {start}-{len(DATA) - 1}/{len(DATA)}"})


def full(start):
    return Resp(DATA, 200)


def dropping(after):
    return lambda start: Resp(DATA[start:], 206 if start else 200, start, fail_after=after)


class ResumeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="pmrs_"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        p = mock.patch("pm_installer.clouds.base.time.sleep")
        p.start()
        self.addCleanup(p.stop)
        self.rf = RemoteFile("f.zip", len(DATA), {"md5": "m"})

    def test_drop_then_resume_with_range(self):
        prov = FakeProv([dropping(20000), partial])
        retries = []
        out = prov.download(self.rf, self.tmp, on_retry=lambda *a: retries.append(a))
        self.assertEqual(out.read_bytes(), DATA)
        self.assertEqual(prov.starts[0], 0)
        self.assertTrue(0 < prov.starts[1] < len(DATA), prov.starts)
        self.assertEqual(len(retries), 1)
        self.assertEqual(list(self.tmp.glob("*.part*")), [])

    def test_range_ignored_restarts_from_zero(self):
        prov = FakeProv([dropping(8192), full])
        out = prov.download(self.rf, self.tmp)
        self.assertEqual(out.read_bytes(), DATA)

    def test_416_restarts(self):
        (self.tmp / "f.zip.part").write_bytes(DATA[:5000])
        (self.tmp / "f.zip.part.id").write_text(CloudProvider._identity(self.rf))
        prov = FakeProv([NetworkError("Сервер вернул ошибку HTTP 416."), full])
        self.assertEqual(prov.download(self.rf, self.tmp).read_bytes(), DATA)
        self.assertEqual(prov.starts, [5000, 0])

    def test_existing_part_for_other_version_is_not_resumed(self):
        (self.tmp / "f.zip.part").write_bytes(b"old-garbage")
        (self.tmp / "f.zip.part.id").write_text("another-identity")
        prov = FakeProv([full])
        self.assertEqual(prov.download(self.rf, self.tmp).read_bytes(), DATA)
        self.assertEqual(prov.starts, [0])

    def test_gives_up_after_retries_and_keeps_part(self):
        prov = FakeProv([dropping(8192)] * 4)
        with self.assertRaises(NetworkError):
            prov.download(self.rf, self.tmp)
        self.assertEqual(len(prov.starts), CloudProvider.RETRIES + 1)
        self.assertTrue((self.tmp / "f.zip.part").exists(), "частичный файл остаётся для следующего запуска")

    def test_cancel_removes_part_and_does_not_retry(self):
        cancel = threading.Event()
        prov = FakeProv([full])
        with self.assertRaises(CancelledError):
            prov.download(self.rf, self.tmp, progress=lambda d, t: cancel.set(), cancel=cancel)
        self.assertEqual(list(self.tmp.glob("*.part*")), [])
        self.assertEqual(len(prov.starts), 1)

    def test_total_from_content_range_when_size_unknown(self):
        seen = []
        rf = RemoteFile("f.zip", None, {})
        prov = FakeProv([partial])
        (self.tmp / "f.zip.part").write_bytes(DATA[:1000])
        (self.tmp / "f.zip.part.id").write_text(CloudProvider._identity(rf))
        prov.download(rf, self.tmp, progress=lambda d, t: seen.append((d, t)))
        self.assertEqual(seen[-1], (len(DATA), len(DATA)))
        self.assertEqual(prov.starts, [1000])

    def test_authorization_errors_are_not_retried(self):
        from pm_installer.errors import InvalidLinkError
        prov = FakeProv([InvalidLinkError("закрыт")])
        with self.assertRaises(InvalidLinkError):
            prov.download(self.rf, self.tmp)
        self.assertEqual(len(prov.starts), 1)


class CacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="pmca_"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.rf = RemoteFile("Game.zip", 3, {"md5": "m"})
        import zipfile
        self.arc = self.tmp / "src.zip"
        with zipfile.ZipFile(self.arc, "w") as z:
            z.writestr("Game.sh", "x")
            z.writestr("game/a", "1")
        self.rf.size = self.arc.stat().st_size

    def installer(self, keep):
        return PortInstaller(Settings(firmware="rocknix", temp_dir=str(self.tmp / "t"), keep_downloads=keep),
                             PROFILES, root=str(self.tmp / "root"))

    def prov(self):
        def dl(rf, d, *a, **k):
            d.mkdir(parents=True, exist_ok=True)
            out = d / rf.name
            shutil.copy(self.arc, out)
            return out
        prov = mock.Mock()
        prov._identity = CloudProvider._identity
        prov.download.side_effect = dl
        return prov

    def test_without_cache_file_is_deleted(self):
        inst, prov = self.installer(False), self.prov()
        inst.install_remote(prov, [self.rf], {"url": "u"})
        self.assertEqual(cache.usage(inst.temp_root), (0, 0))

    def test_cache_reused_and_cleared(self):
        inst, prov = self.installer(True), self.prov()
        inst.settings.conflict_policy = "overwrite"
        inst.install_remote(prov, [self.rf], {"url": "u"})
        n, size = cache.usage(inst.temp_root)
        self.assertEqual((n, size), (1, self.rf.size))
        inst.install_remote(prov, [self.rf], {"url": "u"})
        self.assertEqual(prov.download.call_count, 1, "второй раз архив берётся из кэша")
        changed = RemoteFile("Game.zip", self.rf.size, {"md5": "other"})
        inst.install_remote(prov, [changed], {"url": "u"})
        self.assertEqual(prov.download.call_count, 2, "другая версия в облаке — качаем заново")
        self.assertEqual(cache.clear(inst.temp_root) > 0, True)
        self.assertEqual(cache.usage(inst.temp_root), (0, 0))

    def test_settings_screen_toggle_and_clear(self):
        store = SettingsStore(self.tmp / "home")
        st = store.load()
        st.temp_dir = str(self.tmp / "t")
        (self.tmp / "t" / "cache").mkdir(parents=True)
        (self.tmp / "t" / "cache" / "a__b.zip").write_bytes(b"x" * 10)
        be = FakeBackend()
        g = G.Gui(st, store, PROFILES, be, str(self.tmp / "root"), font_path="x.ttf")
        g.push(G.SettingsScreen())
        scr = g.top
        keys = [k for k, _ in scr.ROWS]
        scr.sel = keys.index("keep_downloads")
        be.queue += ["right"]
        g.step()
        self.assertTrue(st.keep_downloads)
        self.assertTrue(store.load().keep_downloads, "настройка сохранена")
        be.queue += ["down", "ok"]
        g.step()
        self.assertIsInstance(g.top, G.MessageScreen)
        self.assertEqual(cache.usage(self.tmp / "t"), (0, 0))


if __name__ == "__main__":
    unittest.main()
