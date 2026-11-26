import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
import zipfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pm_installer import gui as G  # noqa: E402
from pm_installer.clouds import RemoteFile  # noqa: E402
from pm_installer.firmware import load_profiles  # noqa: E402
from pm_installer.history import History  # noqa: E402
from pm_installer.installer import PortInstaller  # noqa: E402
from pm_installer.settings import Settings, SettingsStore  # noqa: E402
from test_manager import FakeBackend, PROFILES, mk  # noqa: E402

BASE = Path(__file__).resolve().parent.parent
SRC = {"url": "https://disk.yandex.ru/d/AAA", "name": "Мои порты"}


def rf(name="Game.zip", size=100, md5="m1", display=None, modified="t1"):
    return RemoteFile(name, size, {"md5": md5, "modified": modified, "display": display or name})


class HistoryCore(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="pmh_"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.h = History(self.tmp / "installed.json")

    def test_status_new_same_changed(self):
        self.assertEqual(self.h.status(SRC["url"], rf()), "new")
        self.h.record(SRC, rf(), ["Game.sh", "game"])
        self.assertEqual(self.h.status(SRC["url"], rf()), "same")
        self.assertEqual(self.h.status(SRC["url"], rf(md5="m2")), "changed")
        self.assertEqual(self.h.status(SRC["url"], rf(md5=None, size=999)), "changed")
        self.assertEqual(self.h.status(SRC["url"], rf(md5=None, modified="t2")), "changed")
        self.assertEqual(self.h.status("https://other", rf()), "new")
        self.assertEqual(self.h.status(SRC["url"], rf(display="Sub/Game.zip")), "new", "ключ учитывает подпапку")

    def test_no_names_not_recorded(self):
        self.h.record(SRC, rf(), [])
        self.assertFalse((self.tmp / "installed.json").exists())

    def test_source_of_and_reassignment(self):
        self.h.record(SRC, rf(), ["Game.sh", "game"])
        self.assertEqual(self.h.source_of(["game"])["file"], "Game.zip")
        self.assertEqual(self.h.source_of(["GAME.SH"])["cloud"], "Мои порты")
        self.assertIsNone(self.h.source_of(["other"]))
        self.h.record({"url": "https://b", "name": "B"}, rf("G2.zip", display="G2.zip"), ["Game.sh"])
        self.assertEqual(self.h.source_of(["Game"])["cloud"], "B", "порт переехал к новой записи")

    def test_forget_port_and_cleanup(self):
        self.h.record(SRC, rf(), ["Game.sh", "game"])
        self.h.forget_port(["other"])
        self.assertIsNotNone(self.h.source_of(["game"]))
        self.h.forget_port(["Game.sh"])        # .sh и папка — один порт
        self.assertIsNone(self.h.source_of(["game"]))
        self.assertEqual(self.h.status(SRC["url"], rf()), "new")

    def test_corrupt_file_is_ignored_and_atomic(self):
        (self.tmp / "installed.json").write_text("{not json")
        self.assertEqual(self.h.status(SRC["url"], rf()), "new")
        self.h.record(SRC, rf(), ["x"])
        self.assertFalse((self.tmp / "installed.json.tmp").exists())
        self.assertEqual(self.h.status(SRC["url"], rf()), "same")

    def test_installer_records_after_install(self):
        arc = self.tmp / "Game.zip"
        with zipfile.ZipFile(arc, "w") as z:
            z.writestr("Game.sh", "x")
            z.writestr("game/a", "1")
        inst = PortInstaller(Settings(firmware="rocknix", temp_dir=str(self.tmp / "t")), PROFILES,
                             root=str(self.tmp / "root"))
        inst.history = self.h
        prov = mock.Mock()
        prov.download.return_value = arc
        inst.install_remote(prov, [rf()], SRC)
        src = self.h.source_of(["game"])
        self.assertEqual(sorted(src["ports"]), ["Game.sh", "game"])
        self.assertEqual(self.h.status(SRC["url"], rf()), "same")


class HistoryGui(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="pmhg_"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.root = str(self.tmp / "root")
        mk(Path(self.root), "roms/ports/Game.sh")
        mk(Path(self.root), "roms/ports/game/a", size=100)
        self.store = SettingsStore(self.tmp / "home")
        self.st = self.store.load()
        self.st.firmware = "rocknix"
        self.be = FakeBackend()
        self.g = G.Gui(self.st, self.store, PROFILES, self.be, self.root, font_path="x.ttf")
        self.g.history.record(SRC, rf(), ["Game.sh", "game"])

    def press(self, *acts):
        self.be.queue += list(acts)
        self.g.step()

    def wait(self, cond):
        for _ in range(100):
            self.g.step()
            if cond():
                return
            time.sleep(0.03)
        self.fail("не дождались")

    def test_browse_marks_updates_and_r_selects_them(self):
        files = [rf(), rf("New.zip", md5="n", display="New.zip"), rf("Other.zip", md5="o", display="Other.zip")]
        files[0].meta["md5"] = "CHANGED"
        inst = mock.Mock()
        inst.open_dir.return_value = (mock.Mock(), [], files)
        self.st.clouds = [dict(SRC, cloud="auto")]
        with mock.patch.object(G, "PortInstaller", return_value=inst):
            self.press("ok", "ok")
            scr = self.g.top
            self.wait(lambda: scr.state == "ready")
        self.assertEqual(scr.states, {"Game.zip": "changed", "New.zip": "new", "Other.zip": "new"})
        self.press("r")
        self.assertEqual(set(scr.marked), {"Game.zip"})

    def test_manager_shows_source_and_forgets_on_delete(self):
        self.press("down", "ok")
        scr = self.g.top
        self.wait(lambda: scr.state == "ready" and all(p.size is not None for p in scr.ports))
        game = next(p for p in scr.ports if p.name == "Game")
        self.assertEqual(game.source["cloud"], "Мои порты")
        self.press("ok")
        shown = []
        self.be.draw_text = lambda f, s, *a: shown.append(s)
        self.g.step()
        self.assertTrue(any("Облако: Мои порты" in s for s in shown))
        self.assertTrue(any("Установлен приложением" in s for s in shown))
        self.press("back")
        self.press("x", "down", "ok")
        self.wait(lambda: isinstance(self.g.top, G.MessageScreen))
        self.assertIsNone(self.g.history.source_of(["Game"]))
        self.assertEqual(self.g.history.status(SRC["url"], rf()), "new")


if __name__ == "__main__":
    unittest.main()
