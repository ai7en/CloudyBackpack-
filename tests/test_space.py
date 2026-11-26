import os
import shutil
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import sevenzip_writer as W  # noqa: E402
from pm_installer import space  # noqa: E402
from pm_installer import archives as A  # noqa: E402
from pm_installer.firmware import load_profiles  # noqa: E402
from pm_installer.installer import PortInstaller  # noqa: E402
from pm_installer.settings import Settings  # noqa: E402

BASE = Path(__file__).resolve().parent.parent


class Space(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="pmsp_"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_free_bytes_walks_up_to_existing_parent(self):
        self.assertGreater(space.free_bytes(self.tmp / "no" / "such" / "dir"), 0)

    def test_check_raises_with_numbers_and_hint(self):
        with mock.patch.object(space, "free_bytes", return_value=10 * 1024 * 1024):
            with self.assertRaises(space.NoSpaceError) as cm:
                space.check("/x", 500 * 1024 * 1024, "для распаковки")
        msg = str(cm.exception)
        self.assertIn("для распаковки", msg)
        self.assertIn("500", msg)
        self.assertIn("10", msg)
        self.assertIn("Установленные порты", msg)

    def test_check_passes_when_unknown_or_enough(self):
        with mock.patch.object(space, "free_bytes", return_value=None):
            space.check("/x", 10 ** 12, "x")
        with mock.patch.object(space, "free_bytes", return_value=10 ** 9):
            space.check("/x", 10 ** 6, "x")

    def test_same_device(self):
        (self.tmp / "a").mkdir()
        self.assertTrue(space.same_device(self.tmp / "a", self.tmp / "new" / "dir"))

    def test_uncompressed_size_zip_and_7z(self):
        z = self.tmp / "p.zip"
        with zipfile.ZipFile(z, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("a", b"0" * 5000)
            zf.writestr("b", b"1" * 700)
        self.assertEqual(A.uncompressed_size(z), 5700)
        s = self.tmp / "p.7z"
        s.write_bytes(W.build([W.Item("a", b"0" * 3000), W.Item("d", is_dir=True), W.Item("e", b"")]))
        self.assertEqual(A.uncompressed_size(s), 3000)
        junk = self.tmp / "j.zip"
        junk.write_bytes(b"nope")
        self.assertIsNone(A.uncompressed_size(junk))

    def _installer(self):
        st = Settings(firmware="rocknix", temp_dir=str(self.tmp / "t"))
        return PortInstaller(st, load_profiles(BASE / "config" / "paths.json"), root=str(self.tmp / "root"))

    def _zip(self):
        z = self.tmp / "Game.zip"
        with zipfile.ZipFile(z, "w") as zf:
            zf.writestr("Game.sh", "x")
            zf.writestr("game/data", b"0" * 4000)
        return z

    def test_install_refuses_before_extracting_when_no_space(self):
        inst = self._installer()
        with mock.patch.object(space, "free_bytes", return_value=1024):
            with self.assertRaises(space.NoSpaceError):
                inst.install_archive(self._zip())
        self.assertFalse((self.tmp / "root" / "roms" / "ports" / "Game.sh").exists())

    def test_install_ok_with_space(self):
        rep = self._installer().install_archive(self._zip())
        self.assertTrue((self.tmp / "root" / "roms" / "ports" / "Game.sh").is_file())
        self.assertTrue(rep.installed)

    def test_download_precheck_uses_remote_size(self):
        from pm_installer.clouds import RemoteFile
        inst = self._installer()
        provider = mock.Mock()
        with mock.patch.object(space, "free_bytes", return_value=1024):
            with self.assertRaises(space.NoSpaceError):
                inst.install_remote(provider, [RemoteFile("big.zip", 900 * 1024 * 1024, {})])
        provider.download.assert_not_called()


if __name__ == "__main__":
    unittest.main()
