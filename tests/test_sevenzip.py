import array
import os
import random
import shutil
import stat
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import lzma  # noqa: E402

import sevenzip_writer as W  # noqa: E402
from pm_installer import sevenzip as SZ  # noqa: E402
from pm_installer.errors import CancelledError, UnsupportedArchiveError  # noqa: E402
from pm_installer.extract_common import ExtractProgress  # noqa: E402

RNG = random.Random(7)
BIG = bytes(RNG.getrandbits(8) for _ in range(200_000))
TEXT = (b"hello portmaster\n" * 5000)


def sample_items():
    return [W.Item("Game.sh", b"#!/bin/bash\necho hi\n", mode=0o100755),
            W.Item("game", is_dir=True),
            W.Item("game/bin", BIG, mode=0o100755),
            W.Item("game/readme.txt", TEXT),
            W.Item("game/empty.cfg", b""),
            W.Item("game/sub", is_dir=True),
            W.Item("game/sub/русский файл.txt", "данные".encode())]


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="pm7z_"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def archive(self, **kw):
        p = self.tmp / "a.7z"
        p.write_bytes(W.build(sample_items(), **kw))
        return p

    def check_tree(self, out: Path):
        self.assertEqual((out / "Game.sh").read_bytes(), b"#!/bin/bash\necho hi\n")
        self.assertEqual((out / "game/bin").read_bytes(), BIG)
        self.assertEqual((out / "game/readme.txt").read_bytes(), TEXT)
        self.assertEqual((out / "game/empty.cfg").read_bytes(), b"")
        self.assertEqual((out / "game/sub/русский файл.txt").read_text("utf-8"), "данные")
        self.assertTrue((out / "game/sub").is_dir())
        self.assertTrue(os.access(out / "Game.sh", os.X_OK), "unix-права (+x) сохраняются")
        self.assertTrue(os.access(out / "game/bin", os.X_OK))
        self.assertFalse(os.access(out / "game/readme.txt", os.X_OK))


class Extract(Base):
    def run_case(self, **kw):
        out = self.tmp / "out"
        SZ.extract_7z(self.archive(**kw), out)
        self.check_tree(out)

    def test_lzma2_solid(self):
        self.run_case()

    def test_encoded_header(self):
        self.run_case(encode_header=True)

    def test_lzma1(self):
        self.run_case(method="lzma")

    def test_copy(self):
        self.run_case(method="copy")

    def test_non_solid_folders(self):
        self.run_case(folders=[[0], [1], [2, 3]])

    def test_bcj_x86_chain(self):
        self.run_case(bcj=(lzma.FILTER_X86, b"\x04"))

    def test_bcj_arm_old_id(self):
        self.run_case(bcj=(lzma.FILTER_ARM, b"\x03\x03\x05\x01"))

    def test_empty_archive(self):
        p = self.tmp / "e.7z"
        p.write_bytes(W.build([W.Item("only_dir", is_dir=True), W.Item("e.txt", b"")]))
        out = self.tmp / "o"
        SZ.extract_7z(p, out)
        self.assertTrue((out / "only_dir").is_dir())
        self.assertEqual((out / "e.txt").read_bytes(), b"")

    def test_progress_reports_files_bytes_and_names(self):
        from unittest import mock
        from pm_installer.extract_common import Reporter
        seen = []
        with mock.patch.object(SZ, "Reporter", lambda cb: Reporter(cb, 0)):       # без ограничения частоты
            SZ.extract_7z(self.archive(), self.tmp / "o",
                          lambda p: seen.append((p.files_done, p.bytes_done, p.current)))
        last = seen[-1]
        self.assertEqual(last[0], 5)                       # 5 файлов (каталоги не считаются)
        self.assertEqual(last[1], sum(len(i.data) for i in sample_items()))
        self.assertTrue(any("bin" in s[2] for s in seen))
        self.assertEqual(ExtractProgress(bytes_total=10, bytes_done=5).frac, 0.5)

    def test_list_entries(self):
        names = [(e.name, e.size) for e in SZ.list_entries(self.archive()) if not e.is_dir]
        self.assertIn(("game/bin", len(BIG)), names)

    def test_cancel(self):
        ev = threading.Event()
        ev.set()
        with self.assertRaises(CancelledError):
            SZ.extract_7z(self.archive(), self.tmp / "o", cancel=ev)


class Damaged(Base):
    def test_bad_crc_detected(self):
        with self.assertRaises(UnsupportedArchiveError) as cm:
            SZ.extract_7z(self.archive(bad_crc=True), self.tmp / "o")
        self.assertIn("Контрольная сумма", str(cm.exception))

    def test_truncated_download(self):
        p = self.archive()
        data = p.read_bytes()
        p.write_bytes(data[:len(data) // 2])
        with self.assertRaises(UnsupportedArchiveError) as cm:
            SZ.extract_7z(p, self.tmp / "o")
        self.assertIn("не докачан", str(cm.exception))

    def test_corrupt_data_stream(self):
        p = self.archive()
        data = bytearray(p.read_bytes())
        for i in range(60, 400):
            data[i] ^= 0xFF
        p.write_bytes(bytes(data))
        with self.assertRaises(UnsupportedArchiveError):
            SZ.extract_7z(p, self.tmp / "o")

    def test_not_7z(self):
        p = self.tmp / "x.7z"
        p.write_bytes(b"<html>error</html>" * 5)
        with self.assertRaises(UnsupportedArchiveError):
            SZ.extract_7z(p, self.tmp / "o")

    def test_zip_slip_rejected(self):
        p = self.tmp / "evil.7z"
        p.write_bytes(W.build([W.Item("../evil.txt", b"x")]))
        with self.assertRaises(UnsupportedArchiveError):
            SZ.extract_7z(p, self.tmp / "o")
        self.assertFalse((self.tmp / "evil.txt").exists())

    def test_symlink_entry(self):
        p = self.tmp / "l.7z"
        p.write_bytes(W.build([W.Item("real.txt", b"data"), W.Item("link", b"real.txt", mode=stat.S_IFLNK | 0o777)]))
        out = self.tmp / "o"
        SZ.extract_7z(p, out)
        self.assertTrue((out / "link").is_symlink())
        self.assertEqual((out / "link").read_text(), "data")


def _liblzma_arm64_encode(raw: bytes):
    """Прямое ARM64-преобразование эталонным liblzma (≥5.4) через ctypes → «закодированные» байты или None."""
    import ctypes
    import ctypes.util
    name = ctypes.util.find_library("lzma")
    if not name:
        return None
    lib = ctypes.CDLL(name)
    if not hasattr(lib, "lzma_raw_buffer_encode"):
        return None
    opts = ctypes.create_string_buffer(512)
    lib.lzma_lzma_preset.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    if lib.lzma_lzma_preset(opts, 6):
        return None

    class Filt(ctypes.Structure):
        _fields_ = [("id", ctypes.c_uint64), ("options", ctypes.c_void_p)]
    filters = (Filt * 3)(Filt(0x0A, None), Filt(0x21, ctypes.cast(opts, ctypes.c_void_p)),
                         Filt(0xFFFFFFFFFFFFFFFF, None))
    out = ctypes.create_string_buffer(len(raw) * 2 + 1024)
    pos = ctypes.c_size_t(0)
    lib.lzma_raw_buffer_encode.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_char_p, ctypes.c_size_t,
                                           ctypes.c_void_p, ctypes.POINTER(ctypes.c_size_t), ctypes.c_size_t]
    if lib.lzma_raw_buffer_encode(filters, None, raw, len(raw), out, ctypes.byref(pos), len(out)) != 0:
        return None                                    # liblzma без ARM64 (< 5.4)
    packed = out.raw[:pos.value]
    # распаковываем только LZMA2 (без ARM64) → байты после прямого преобразования
    dec = lzma.LZMADecompressor(format=lzma.FORMAT_RAW, filters=[{"id": lzma.FILTER_LZMA2, "dict_size": 8 << 20}])
    return dec.decompress(packed)


class Arm64(unittest.TestCase):
    def test_pure_python_matches_liblzma(self):
        """Наш декодер ARM64 сверяется с эталоном liblzma на синтетическом коде с BL и ADRP."""
        rng = random.Random(3)
        words = array.array("I")
        for _ in range(60_000):
            r = rng.random()
            if r < 0.25:
                words.append(0x94000000 | rng.getrandbits(26))                                   # BL
            elif r < 0.5:
                lo = rng.getrandbits(2)
                hi = rng.choice([0, 0x7FFFF, rng.getrandbits(19)])
                words.append(0x90000000 | (lo << 29) | (hi << 5) | rng.getrandbits(5))           # ADRP
            else:
                words.append(rng.getrandbits(32))
        raw = words.tobytes() + b"\x01\x02\x03"                                                 # хвост не кратен 4
        encoded = _liblzma_arm64_encode(raw)
        if encoded is None:
            self.skipTest("в системном liblzma нет фильтра ARM64 (нужен xz ≥ 5.4)")
        self.assertNotEqual(encoded, raw, "фильтр что-то преобразовал")
        pieces = [encoded[i:i + 7777] for i in range(0, len(encoded), 7777)]      # нарезка не кратна 4
        self.assertEqual(b"".join(SZ._arm64_stage(iter(pieces))), raw)


if __name__ == "__main__":
    unittest.main()
