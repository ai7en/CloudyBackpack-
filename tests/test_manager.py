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
from pm_installer import manager as M  # noqa: E402
from pm_installer.errors import InstallerError  # noqa: E402
from pm_installer.firmware import load_profiles  # noqa: E402
from pm_installer.settings import Settings, SettingsStore, cloud_title  # noqa: E402

BASE = Path(__file__).resolve().parent.parent
PROFILES = load_profiles(BASE / "config" / "paths.json")


def mk(root: Path, rel: str, text="x", size=0):
    p = root / rel.lstrip("/")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    if size:
        p.write_bytes(b"0" * size)
    return p


class ManagerCore(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="pmmgr_"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.root = str(self.tmp)
        self.prof = PROFILES["muos"]
        self.sd = "mnt/mmc/ports"
        self.sc = "mnt/mmc/ROMS/Ports"

    def build(self):
        mk(self.tmp, f"{self.sc}/Celeste.sh")
        mk(self.tmp, f"{self.sd}/celeste/data.bin", size=3000)       # имя совпадает без учёта регистра
        mk(self.tmp, f"{self.sc}/Super Game.sh", 'GAMEDIR="/$directory/ports/sg_folder"\n')
        mk(self.tmp, f"{self.sd}/sg_folder/a", size=500)             # имя папки взято из GAMEDIR
        mk(self.tmp, f"{self.sc}/OnlyScript.sh")
        mk(self.tmp, f"{self.sd}/orphan/a", size=100)
        mk(self.tmp, f"{self.sc}/PortMaster.sh")                     # служебное — не порт
        mk(self.tmp, f"{self.sd}/PortMaster/x")
        mk(self.tmp, f"{self.sd}/images/x")
        mk(self.tmp, f"{self.sc}/readme.txt")
        mk(self.tmp, f"{self.sc}/Cloud Port Installer.sh")
        mk(self.tmp, f"{self.sd}/cloudportinstaller/main.py")

    def test_scan_matching_and_kinds(self):
        self.build()
        ports = {p.name: p for p in M.scan(self.prof, self.root)}
        self.assertEqual(set(ports), {"Celeste", "Super Game", "OnlyScript", "orphan", "Cloud Port Installer"})
        self.assertEqual(ports["Celeste"].kind, "sh + папка")
        self.assertEqual(ports["Super Game"].folder.name, "sg_folder")
        self.assertEqual(ports["OnlyScript"].kind, "только sh (папки нет)")
        self.assertEqual(ports["orphan"].kind, "только папка (нет sh)")
        self.assertTrue(ports["Cloud Port Installer"].protected)
        self.assertEqual(ports["Cloud Port Installer"].folder.name, "cloudportinstaller")
        self.assertFalse(ports["Celeste"].protected)
        self.assertGreater(ports["Celeste"].mtime, 0)

    def test_sizes(self):
        self.build()
        p = next(x for x in M.scan(self.prof, self.root) if x.name == "Celeste")
        self.assertIsNone(p.size)
        M.compute_size(p)
        self.assertEqual(p.size, 3000 + 1)     # файл папки + «x» в .sh
        self.assertEqual(M.format_size(1536), "1.5 КБ")
        self.assertEqual(M.format_size(None), "…")
        self.assertEqual(M.format_size(250 * 1024 ** 2), "250 МБ")

    def test_second_sd_card_scanned(self):
        mk(self.tmp, "mnt/sdcard/ROMS/Ports/Two.sh")
        mk(self.tmp, "mnt/sdcard/ports/two/f", size=10)
        self.build()
        names = {p.name for p in M.scan(self.prof, self.root)}
        self.assertIn("Two", names)
        two = next(p for p in M.scan(self.prof, self.root) if p.name == "Two")
        self.assertEqual(two.kind, "sh + папка")

    def test_delete_removes_both(self):
        self.build()
        p = next(x for x in M.scan(self.prof, self.root) if x.name == "Celeste")
        M.compute_size(p)
        freed = M.delete_port(p, self.prof, self.root)
        self.assertEqual(freed, p.size)
        self.assertFalse((self.tmp / self.sd / "celeste").exists())
        self.assertFalse((self.tmp / self.sc / "Celeste.sh").exists())
        self.assertTrue((self.tmp / self.sd / "sg_folder").exists(), "соседние порты целы")
        self.assertNotIn("Celeste", {x.name for x in M.scan(self.prof, self.root)})

    def test_delete_orphans_and_script_only(self):
        self.build()
        ports = {p.name: p for p in M.scan(self.prof, self.root)}
        M.delete_port(ports["orphan"], self.prof, self.root)
        M.delete_port(ports["OnlyScript"], self.prof, self.root)
        self.assertFalse((self.tmp / self.sd / "orphan").exists())
        self.assertFalse((self.tmp / self.sc / "OnlyScript.sh").exists())

    def test_protected_cannot_be_deleted(self):
        self.build()
        me = next(p for p in M.scan(self.prof, self.root) if p.protected)
        with self.assertRaises(InstallerError):
            M.delete_port(me, self.prof, self.root)
        self.assertTrue((self.tmp / self.sd / "cloudportinstaller").exists())

    def test_refuses_paths_outside_port_dirs(self):
        outside = self.tmp / "elsewhere"
        outside.mkdir()
        (outside / "f").write_text("keep")
        bad = M.InstalledPort("evil", None, outside)
        self.build()
        with self.assertRaises(InstallerError):
            M.delete_port(bad, self.prof, self.root)
        self.assertTrue((outside / "f").exists())

    def test_symlink_folder_removed_without_touching_target(self):
        self.build()
        target = self.tmp / "target"
        target.mkdir()
        (target / "keep").write_text("1")
        link = self.tmp / self.sd / "linked"
        link.symlink_to(target)
        p = next(x for x in M.scan(self.prof, self.root) if x.name == "linked")
        M.delete_port(p, self.prof, self.root)
        self.assertFalse(link.exists() or link.is_symlink())
        self.assertTrue((target / "keep").exists())

    def test_rocknix_same_dir(self):
        prof = PROFILES["rocknix"]
        mk(self.tmp, "roms/ports/Game.sh")
        mk(self.tmp, "roms/ports/game/a")
        mk(self.tmp, "roms/ports/PortMaster/x")
        ports = M.scan(prof, self.root)
        self.assertEqual([(p.name, p.kind) for p in ports], [("Game", "sh + папка")])

    def test_no_dirs_gives_empty_list(self):
        self.assertEqual(M.scan(self.prof, self.root), [])


class CloudsSettings(unittest.TestCase):
    def test_migration_from_single_url(self):
        home = Path(tempfile.mkdtemp(prefix="pmset_"))
        self.addCleanup(shutil.rmtree, home, True)
        (home / "settings.json").write_text('{"url": "https://disk.yandex.ru/d/AAA", "cloud": "yandex"}')
        st = SettingsStore(home).load()
        self.assertEqual(st.clouds, [{"name": "", "url": "https://disk.yandex.ru/d/AAA", "cloud": "yandex"}])

    def test_no_resurrection_after_delete(self):
        home = Path(tempfile.mkdtemp(prefix="pmset_"))
        self.addCleanup(shutil.rmtree, home, True)
        store = SettingsStore(home)
        (home / "settings.json").write_text('{"url": "https://x.y/z"}')
        st = store.load()
        st.clouds = []
        store.save(st)
        self.assertEqual(store.load().clouds, [])

    def test_normalize_drops_bad_entries(self):
        st = Settings(clouds=[{"url": " "}, "junk", {"url": "https://a.b", "name": " N "}]).normalize()
        self.assertEqual(st.clouds, [{"name": "N", "url": "https://a.b", "cloud": "auto"}])

    def test_title(self):
        self.assertEqual(cloud_title({"name": "Мои порты", "url": "https://a.b"}), "Мои порты")
        self.assertEqual(cloud_title({"name": "", "url": "https://disk.yandex.ru/d/x"}), "disk.yandex.ru")


class FakeBackend:
    name = "fake"

    def __init__(self):
        self.swap_ab, self.queue = False, []

    def load_fonts(self, path, sizes): return [object() for _ in sizes]
    def text_size(self, font, s): return (len(s) * 8, 16)
    def draw_text(self, *a): pass
    def clear(self, c): pass
    def fill_rect(self, *a): pass
    def outline(self, *a): pass
    def present(self): pass
    def ticks(self): return 1000
    def delay(self, ms): pass
    def close(self): pass

    def poll(self):
        q, self.queue = self.queue, []
        return q


class ManagerGui(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="pmmg_"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.root = str(self.tmp / "root")
        r = Path(self.root)
        mk(r, "roms/ports/Alpha.sh")
        mk(r, "roms/ports/alpha/f", size=5000)
        mk(r, "roms/ports/Beta.sh")
        mk(r, "roms/ports/Cloud Port Installer.sh")
        mk(r, "roms/ports/cloudportinstaller/main.py")
        self.store = SettingsStore(self.tmp / "home")
        self.st = self.store.load()
        self.st.firmware = "rocknix"
        self.be = FakeBackend()
        self.g = G.Gui(self.st, self.store, PROFILES, self.be, self.root, font_path="x.ttf")

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

    def open_manager(self):
        self.press("down", "ok")
        scr = self.g.top
        self.assertIsInstance(scr, G.ManagerScreen)
        self.wait(lambda: scr.state == "ready" and all(p.size is not None for p in scr.ports))
        return scr

    def test_lists_sizes_and_details(self):
        scr = self.open_manager()
        self.assertEqual([p.name for p in scr.ports], ["Alpha", "Beta", "Cloud Port Installer"])
        self.assertEqual(scr.ports[0].size, 5001)
        self.press("ok")
        self.assertIsInstance(self.g.top, G.MessageScreen)
        self.press("back")
        self.press("y")                                 # сортировка по размеру
        self.assertEqual(scr.ports[0].name, "Alpha")
        self.press("y", "y")
        self.assertEqual(scr.sort, 0)

    def test_delete_flow(self):
        scr = self.open_manager()
        self.press("x")
        self.assertIsInstance(self.g.top, G.Confirm)
        self.press("ok")                                # «Нет»
        self.assertTrue((Path(self.root) / "roms/ports/alpha").exists())
        self.press("x", "down", "ok")                   # «Да»
        self.wait(lambda: isinstance(self.g.top, G.MessageScreen))
        self.assertFalse((Path(self.root) / "roms/ports/alpha").exists())
        self.assertFalse((Path(self.root) / "roms/ports/Alpha.sh").exists())
        self.press("ok")
        self.assertEqual([p.name for p in scr.ports], ["Beta", "Cloud Port Installer"])

    def test_cannot_delete_self(self):
        scr = self.open_manager()
        self.press("down", "down", "x")
        self.assertIsInstance(self.g.top, G.MessageScreen)
        self.assertTrue((Path(self.root) / "roms/ports/cloudportinstaller").exists())

    def test_unknown_firmware_shows_error(self):
        self.st.firmware = "auto"
        self.g.cfw = ""
        self.press("down", "ok")
        scr = self.g.top
        self.wait(lambda: scr.state == "error")
        self.assertIn("прошивк", scr.error)
        self.press("back")
        self.assertIsInstance(self.g.top, G.MainMenu)


if __name__ == "__main__":
    unittest.main()
