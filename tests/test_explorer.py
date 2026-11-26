import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pm_installer import gui as G  # noqa: E402
from pm_installer.clouds import RemoteDir, RemoteFile, get_provider  # noqa: E402
from pm_installer.clouds.gdrive import GoogleDrive  # noqa: E402
from pm_installer.settings import SettingsStore  # noqa: E402
from test_core import FakeResp  # noqa: E402
from test_manager import FakeBackend, PROFILES  # noqa: E402


class ProviderListDir(unittest.TestCase):
    def test_yandex_list_dir_levels_and_prefix(self):
        prov = get_provider("https://yadi.sk/d/abc")
        root = [{"type": "dir", "name": "sub", "path": "/sub"},
                {"type": "file", "name": "a.zip", "path": "/a.zip", "size": 1, "md5": "m"},
                {"type": "file", "name": "n.txt", "path": "/n.txt"}]
        sub = [{"type": "file", "name": "b.7z", "path": "/sub/b.7z", "size": 2}]

        def fake(self_, method, url, **kw):
            items = sub if (kw.get("params") or {}).get("path") == "/sub" else root
            return FakeResp(js={"type": "dir", "_embedded": {"items": items, "total": len(items)}})
        with mock.patch.object(type(prov), "_request", fake):
            dirs, files = prov.list_dir("https://yadi.sk/d/abc")
            self.assertEqual([(d.name, d.path, d.meta["prefix"]) for d in dirs], [("sub", "/sub", "sub/")])
            self.assertEqual([(f.name, f.meta["display"]) for f in files], [("a.zip", "a.zip")])
            d2, f2 = prov.list_dir("https://yadi.sk/d/abc", "/sub", "sub/")
        self.assertEqual(d2, [])
        self.assertEqual((f2[0].name, f2[0].meta["display"], f2[0].meta["path"]), ("b.7z", "sub/b.7z", "/sub/b.7z"))

    def test_default_list_dir_is_flat(self):
        prov = get_provider("https://example.com/p.zip")
        with mock.patch.object(type(prov), "list_files", return_value=[RemoteFile("p.zip")]):
            dirs, files = prov.list_dir("https://example.com/p.zip")
        self.assertEqual((dirs, [f.name for f in files]), ([], ["p.zip"]))

    PAGE = ('<div class="flip-entries">'
            '<div class="flip-entry" id="entry-F1"><a href="https://drive.google.com/drive/folders/F1" target="_blank">'
            '<div class="flip-entry-info"><div class="flip-entry-title">Новые &amp; старые</div></div></a></div>'
            '<div class="flip-entry" id="entry-A1"><a href="https://drive.google.com/file/d/A1/view?usp=embed_web" '
            'target="_blank"><div class="flip-entry-title">game.zip</div></a></div>'
            '<div class="flip-entry" id="entry-T1"><a href="https://drive.google.com/file/d/T1/view" '
            'target="_blank"><div class="flip-entry-title">readme.txt</div></a></div></div>')

    def test_gdrive_parse_embedded_and_list_dir(self):
        entries, n = GoogleDrive.parse_embedded(self.PAGE)
        self.assertEqual(n, 3)
        self.assertEqual(entries[0], ("folder", "F1", "Новые & старые"))
        prov = get_provider("https://drive.google.com/drive/folders/ROOT")
        seen = []

        def fake(self_, method, url, **kw):
            seen.append(kw["params"]["id"])
            return FakeResp(text=self.PAGE)
        with mock.patch.object(type(prov), "_request", fake):
            dirs, files = prov.list_dir("https://drive.google.com/drive/folders/ROOT")
            self.assertEqual([(d.name, d.path) for d in dirs], [("Новые & старые", "F1")])
            self.assertEqual([(f.name, f.meta["id"]) for f in files], [("game.zip", "A1")])
            prov.list_dir("https://drive.google.com/drive/folders/ROOT", "F1", "x/")
        self.assertEqual(seen, ["ROOT", "F1"])

    def test_gdrive_empty_root_falls_back_to_gdown_path(self):
        prov = get_provider("https://drive.google.com/drive/folders/ROOT")
        with mock.patch.object(type(prov), "_request", return_value=FakeResp(text="<html></html>")), \
                mock.patch.object(type(prov), "list_files", return_value=[RemoteFile("z.zip")]) as lf:
            dirs, files = prov.list_dir("https://drive.google.com/drive/folders/ROOT")
        self.assertEqual([f.name for f in files], ["z.zip"])
        lf.assert_called_once()


class ExplorerGui(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="pmex_"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        store = SettingsStore(self.tmp / "home")
        self.st = store.load()
        self.st.firmware = "rocknix"
        self.st.clouds = [{"name": "N", "url": "https://x/y", "cloud": "auto"}]
        self.be = FakeBackend()
        self.g = G.Gui(self.st, store, PROFILES, self.be, str(self.tmp / "root"), font_path="x.ttf")
        self.tree = {
            None: ([RemoteDir("Sub", "/sub", {"prefix": "Sub/"})],
                   [RemoteFile("alpha.zip", 10, {"display": "alpha.zip"}),
                    RemoteFile("beta.zip", 20, {"display": "beta.zip"})]),
            "/sub": ([], [RemoteFile("gamma.7z", 30, {"display": "Sub/gamma.7z"})]),
        }

    def open_dir(self, path=None, prefix=""):
        d, f = self.tree[path]
        return mock.Mock(), d, f

    def press(self, *acts):
        self.be.queue += list(acts)
        self.g.step()

    def wait(self, scr):
        for _ in range(100):
            self.g.step()
            if scr.state != "loading":
                return
            time.sleep(0.03)
        self.fail("не дождались")

    def start(self):
        inst = mock.Mock()
        inst.open_dir.side_effect = self.open_dir
        patcher = mock.patch.object(G, "PortInstaller", return_value=inst)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.press("ok", "ok")
        scr = self.g.top
        self.wait(scr)
        return scr

    def test_navigate_in_and_out_keeps_marks(self):
        scr = self.start()
        self.assertEqual([k for k, _ in scr.rows], ["dir", "file", "file"])
        self.press("down", "x")                        # отметить alpha.zip
        self.press("up", "ok")                         # войти в Sub
        self.wait(scr)
        self.assertEqual([k for k, _ in scr.rows], ["up", "file"])
        self.assertEqual(scr.stack[0][2], "Sub")
        self.press("down", "x")                        # gamma.7z
        self.assertEqual(set(scr.marked), {"alpha.zip", "Sub/gamma.7z"})
        self.press("back")                             # B — на уровень вверх, не выход
        self.wait(scr)
        self.assertIs(self.g.top, scr)
        self.assertEqual(scr.stack, [])
        self.assertEqual(len(scr.marked), 2)
        with mock.patch.object(G, "InstallScreen") as inst_cls:
            self.press("start")
        self.assertEqual([f.name for f in inst_cls.call_args[0][2]], ["gamma.7z", "alpha.zip"])   # порядок по полному имени
        self.g.pop()                                   # убрать подставную InstallScreen
        self.press("back")
        self.assertIsInstance(self.g.top, G.CloudsScreen)

    def test_up_row_goes_back_and_ok_on_dir_does_not_install(self):
        scr = self.start()
        with mock.patch.object(G, "InstallScreen") as inst_cls:
            self.press("ok")                           # на папке
            inst_cls.assert_not_called()
        self.wait(scr)
        self.press("ok")                               # строка «..»
        self.wait(scr)
        self.assertEqual(scr.stack, [])

    def test_filter_via_select_and_clear_with_back(self):
        scr = self.start()
        self.press("select")
        tin = self.g.top
        self.assertIsInstance(tin, G.TextInput)
        tin.value = "BET"
        tin._finish(self.g, True)
        self.assertEqual([G.key(o) for k, o in scr.rows if k == "file"], ["beta.zip"])
        self.assertEqual(scr.rows[0][0], "file")       # папки тоже фильтруются
        self.press("back")                             # сбросить поиск, а не выйти
        self.assertEqual(scr.filter, "")
        self.assertEqual(len(scr.rows), 3)
        self.assertIs(self.g.top, scr)

    def test_y_marks_only_visible_files(self):
        scr = self.start()
        scr.filter = "alpha"
        scr._rebuild()
        self.press("y")
        self.assertEqual(set(scr.marked), {"alpha.zip"})
        self.press("y")
        self.assertEqual(scr.marked, {})


if __name__ == "__main__":
    unittest.main()
