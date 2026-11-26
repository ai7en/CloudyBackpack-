import json
import os
import shutil
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pm_installer import portinfo  # noqa: E402
from pm_installer.installer import PortInstaller  # noqa: E402
from pm_installer.settings import Settings  # noqa: E402
from test_manager import PROFILES  # noqa: E402

PJ = {"version": 3, "name": "game.zip", "items": ["Game.sh", "game"],
      "attr": {"title": "Game", "desc": "Описание\nигры", "porter": ["Bob", "Al"],
               "runtime": "frt_2.2.1", "reqs": ["opengl"], "genres": ["action"]}}


class PortInfoTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="pmpi_"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.ctl = self.tmp / "PortMaster"
        (self.ctl / "libs").mkdir(parents=True)
        (self.tmp / "w" / "game").mkdir(parents=True)

    def put(self, data, where="w/port.json"):
        (self.tmp / where).write_text(data if isinstance(data, str) else json.dumps(data), "utf-8")

    def test_parse_and_missing_runtime_warning(self):
        self.put(PJ)
        info = portinfo.read(self.tmp / "w", self.ctl)
        self.assertEqual((info.title, info.porter, info.runtime), ("Game", "Bob, Al", "frt_2.2.1"))
        self.assertEqual(info.desc, "Описание игры")
        self.assertTrue(any("frt_2.2.1" in w for w in info.warnings))
        self.assertTrue(any(l.startswith("Внимание") for l in info.lines()))

    def test_runtime_present_no_warning(self):
        self.put(PJ, "w/game/port.json")                      # на уровень глубже
        (self.ctl / "libs" / "frt_2.2.1.squashfs").write_bytes(b"x")
        self.assertEqual(portinfo.read(self.tmp / "w", self.ctl).warnings, [])

    def test_unknown_controlfolder_does_not_warn(self):
        self.put(PJ)
        self.assertIsNone(portinfo.runtime_present("x", None))
        os.environ.pop("PM_CONTROLFOLDER", None)
        orig = portinfo.CONTROL_CANDIDATES
        portinfo.CONTROL_CANDIDATES = ()
        self.addCleanup(setattr, portinfo, "CONTROL_CANDIDATES", orig)
        self.assertEqual(portinfo.read(self.tmp / "w").warnings, [])

    def test_bad_files(self):
        self.assertIsNone(portinfo.read(self.tmp / "w", self.ctl))
        self.put("{oops")
        self.assertIsNone(portinfo.read(self.tmp / "w", self.ctl))
        self.put("[1,2]")
        self.assertIsNone(portinfo.read(self.tmp / "w", self.ctl))

    def test_control_folder_env(self):
        self.assertEqual(portinfo.control_folder({"PM_CONTROLFOLDER": str(self.ctl)}), self.ctl)

    def test_installer_attaches_info_to_report(self):
        arc = self.tmp / "Game.zip"
        with zipfile.ZipFile(arc, "w") as z:
            z.writestr("Game.sh", "x")
            z.writestr("game/port.json", json.dumps(dict(PJ, attr={"title": "Game Title"})))
        inst = PortInstaller(Settings(firmware="rocknix", temp_dir=str(self.tmp / "t")), PROFILES,
                             root=str(self.tmp / "root"))
        rep = inst.install_archive(arc)
        self.assertEqual(rep.info.title, "Game Title")
        self.assertIn("Порт: Game Title", rep.summary())


if __name__ == "__main__":
    unittest.main()
