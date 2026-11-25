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
from pm_installer import archives as A  # noqa: E402
from pm_installer import sevenzip as SZ  # noqa: E402
from pm_installer.backends.combo import ExitCombo  # noqa: E402
from pm_installer.errors import CancelledError, MissingDependencyError, UnsupportedArchiveError  # noqa: E402
from pm_installer.extract_common import Reporter  # noqa: E402


class Combo(unittest.TestCase):
    def test_sequences(self):
        c = ExitCombo()
        self.assertEqual(c.press("ok", "back"), ["back"])
        self.assertEqual(c.press("select", "select"), ["select"])
        self.assertEqual(c.press("start", "start"), ["quit"])
        c.release("select")
        c.release("start")
        self.assertEqual(c.press("start", "start"), ["start"])
        c.release("start")
        self.assertEqual(c.press("select", "select"), ["select"])


class ZipProgress(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_zip_streams_with_bytes_and_names(self):
        arc = self.tmp / "p.zip"
        with zipfile.ZipFile(arc, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("a.sh", "x")
            z.writestr("d/big.bin", b"7" * 900_000)
            z.writestr("d/", "")
        seen = []
        with mock.patch.object(A, "Reporter", lambda cb: Reporter(cb, 0)):
            A.extract(arc, self.tmp / "o", lambda p: seen.append((p.files_done, p.files_total, p.bytes_done,
                                                                    p.bytes_total, p.current)))
        self.assertEqual(seen[-1][:2], (2, 2))
        self.assertEqual(seen[-1][2:4], (900_001, 900_001))
        self.assertTrue(any(s[4] == "d/big.bin" and 0 < s[2] < 900_001 for s in seen), "прогресс идёт внутри крупного файла")
        self.assertEqual((self.tmp / "o/d/big.bin").stat().st_size, 900_000)

    def test_zip_cancel_mid_file(self):
        import threading
        arc = self.tmp / "p.zip"
        with zipfile.ZipFile(arc, "w") as z:
            z.writestr("big.bin", b"7" * 2_000_000)
        ev = threading.Event()
        with mock.patch.object(A, "Reporter", lambda cb: Reporter(cb, 0)):
            with self.assertRaises(CancelledError):
                A.extract(arc, self.tmp / "o", lambda p: ev.set() if p.bytes_done > 300_000 else None, ev)

    def test_zip_slip_still_blocked(self):
        arc = self.tmp / "e.zip"
        with zipfile.ZipFile(arc, "w") as z:
            z.writestr("../evil", "x")
        with self.assertRaises(UnsupportedArchiveError):
            A.extract(arc, self.tmp / "o")


class SevenZipRouting(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.arc = self.tmp / "p.7z"
        self.arc.write_bytes(W.build([W.Item("Game.sh", b"x", mode=0o100755), W.Item("g/a", b"1" * 100)]))

    def test_extract_dispatch_uses_builtin_without_tools(self):
        with mock.patch.object(A.shutil, "which", return_value=None):       # ни 7z, ни bsdtar на «консоли»
            A.extract(self.arc, self.tmp / "o")
        self.assertEqual((self.tmp / "o/g/a").read_bytes(), b"1" * 100)
        self.assertTrue(os.access(self.tmp / "o/Game.sh", os.X_OK))

    def test_unsupported_method_falls_back_to_cli_then_reports(self):
        with mock.patch.object(SZ, "_build_pipeline", side_effect=SZ.FeatureNotSupported("метод")), \
                mock.patch.object(A, "_run_cli", return_value=False):
            with self.assertRaises(UnsupportedArchiveError):
                A.extract(self.arc, self.tmp / "o")
        with mock.patch.object(SZ, "_build_pipeline", side_effect=SZ.FeatureNotSupported("метод")), \
                mock.patch.object(A, "_run_cli", return_value=True) as cli:
            A.extract(self.arc, self.tmp / "o2")
            self.assertTrue(cli.called)

    def test_no_lzma_module_gives_clear_message(self):
        with mock.patch.object(SZ, "lzma", None), mock.patch.object(A, "_run_cli", return_value=False):
            with self.assertRaises(MissingDependencyError) as cm:
                A.extract(self.arc, self.tmp / "o")
        self.assertIn("lzma", str(cm.exception))

    def test_corruption_is_not_retried_with_other_tools(self):
        bad = self.tmp / "bad.7z"
        bad.write_bytes(W.build([W.Item("a", b"1" * 50)], bad_crc=True))
        with mock.patch.object(A, "_run_cli", return_value=True) as cli:
            with self.assertRaises(UnsupportedArchiveError):
                A.extract(bad, self.tmp / "o")
        self.assertFalse(cli.called)


if __name__ == "__main__":
    unittest.main()
