"""Распаковка .7z на чистом Python (stdlib: lzma, zlib, bz2) — для консолей без py7zr и без утилиты 7z.

Поддержано: Copy, LZMA, LZMA2, Deflate, BZip2, фильтры BCJ (x86, ARM, ARMT, ARM64, PPC, IA64, SPARC) и Delta,
зашифрованные заголовки/данные — нет (ошибка «защищён паролем»). Потоковая работа: память — порядка словаря
LZMA, а не размера архива. Контрольные суммы CRC32 проверяются, так что обрыв загрузки не пройдёт незамеченным.
Формат: 7zFormat.txt из дистрибутива 7-Zip.
"""
import array
import bz2
import logging
import os
import stat
import struct
import sys
import threading
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, List, Optional, Tuple

from .errors import CancelledError, MissingDependencyError, UnsupportedArchiveError, wrap_oserror
from .extract_common import ProgressCB, Reporter, safe_target

try:
    import lzma
except ImportError:      # Python собран без liblzma
    lzma = None

log = logging.getLogger("pm_installer.sevenzip")

SIGNATURE = b"7z\xbc\xaf\x27\x1c"
_BUF = 1 << 20           # размер порции распакованных данных
_PACK_CHUNK = 256 * 1024

K_END, K_HEADER, K_ARCHIVE_PROPS, K_ADD_STREAMS, K_MAIN_STREAMS, K_FILES = 0, 1, 2, 3, 4, 5
K_PACK_INFO, K_UNPACK_INFO, K_SUBSTREAMS, K_SIZE, K_CRC, K_FOLDER, K_CODERS_UNPACK = 6, 7, 8, 9, 0xA, 0xB, 0xC
K_NUM_UNPACK, K_EMPTY_STREAM, K_EMPTY_FILE, K_ANTI, K_NAMES, K_WIN_ATTRS, K_ENCODED_HEADER = 0xD, 0xE, 0xF, 0x10, 0x11, 0x15, 0x17

ATTR_DIRECTORY = 0x10
ATTR_UNIX_EXT = 0x8000

# id метода → (имя, имя lzma-фильтра или None)
_BCJ = {b"\x04": "x86", b"\x03\x03\x01\x03": "x86", b"\x05": "ppc", b"\x03\x03\x02\x05": "ppc",
        b"\x06": "ia64", b"\x03\x03\x04\x01": "ia64", b"\x07": "arm", b"\x03\x03\x05\x01": "arm",
        b"\x08": "armt", b"\x03\x03\x07\x01": "armt", b"\x09": "sparc", b"\x03\x03\x08\x05": "sparc",
        b"\x0a": "arm64"}
_LZMA_FILTER_NAMES = {"x86": "FILTER_X86", "ppc": "FILTER_POWERPC", "ia64": "FILTER_IA64", "arm": "FILTER_ARM",
                      "armt": "FILTER_ARMTHUMB", "sparc": "FILTER_SPARC", "arm64": "FILTER_ARM64"}


class FeatureNotSupported(UnsupportedArchiveError):
    """Вариант .7z, который этот модуль не умеет (но другая программа, возможно, сможет)."""


def _bad(msg: str = "") -> UnsupportedArchiveError:
    return UnsupportedArchiveError("Архив .7z повреждён или не докачан" + (f" ({msg})." if msg else "."))


# ------------------------------------------------------------------ разбор заголовка

class _Reader:
    def __init__(self, data: bytes):
        self.d, self.p = data, 0

    def byte(self) -> int:
        if self.p >= len(self.d):
            raise _bad("заголовок оборван")
        b = self.d[self.p]
        self.p += 1
        return b

    def take(self, n: int) -> bytes:
        if n < 0 or self.p + n > len(self.d):
            raise _bad("заголовок оборван")
        out = self.d[self.p:self.p + n]
        self.p += n
        return out

    def number(self) -> int:
        first = self.byte()
        mask, value = 0x80, 0
        for i in range(8):
            if not first & mask:
                return value + ((first & (mask - 1)) << (8 * i))
            value |= self.byte() << (8 * i)
            mask >>= 1
        return value

    def u32(self) -> int:
        return struct.unpack("<I", self.take(4))[0]

    def bits(self, n: int) -> List[bool]:
        raw = self.take((n + 7) // 8)
        return [bool(raw[i >> 3] & (0x80 >> (i & 7))) for i in range(n)]

    def digests(self, n: int) -> List[Optional[int]]:
        defined = [True] * n if self.byte() else self.bits(n)
        return [self.u32() if d else None for d in defined]


@dataclass
class _Coder:
    cid: bytes
    nin: int
    nout: int
    props: bytes


@dataclass
class _Folder:
    coders: List[_Coder]
    binds: List[Tuple[int, int]]          # (in_index, out_index)
    packed: List[int]                     # индексы входных потоков, взятых прямо из архива
    unpack_sizes: List[int] = field(default_factory=list)
    crc: Optional[int] = None
    first_pack: int = 0                   # номер первого упакованного потока

    def final_out(self) -> int:
        bound = {o for _, o in self.binds}
        for i in range(sum(c.nout for c in self.coders)):
            if i not in bound:
                return i
        raise _bad("цикл в цепочке кодеров")

    @property
    def size(self) -> int:
        return self.unpack_sizes[self.final_out()]


@dataclass
class _Streams:
    pack_pos: int = 0
    pack_sizes: List[int] = field(default_factory=list)
    folders: List[_Folder] = field(default_factory=list)
    nsub: List[int] = field(default_factory=list)
    sub_sizes: List[List[int]] = field(default_factory=list)
    sub_crcs: List[List[Optional[int]]] = field(default_factory=list)


@dataclass
class _Entry:
    name: str = ""
    has_stream: bool = True
    is_dir: bool = False
    attr: Optional[int] = None
    size: int = 0
    crc: Optional[int] = None


def _read_folder(r: _Reader) -> _Folder:
    coders, tin, tout = [], 0, 0
    for _ in range(r.number()):
        flag = r.byte()
        if flag & 0x80:
            raise FeatureNotSupported("Этот вариант .7z (альтернативные методы) не поддерживается.")
        cid = r.take(flag & 0x0F)
        nin = nout = 1
        if flag & 0x10:
            nin, nout = r.number(), r.number()
        props = r.take(r.number()) if flag & 0x20 else b""
        coders.append(_Coder(cid, nin, nout, props))
        tin, tout = tin + nin, tout + nout
    binds = [(r.number(), r.number()) for _ in range(tout - 1)]
    npacked = tin - len(binds)
    if npacked == 1:
        used = {i for i, _ in binds}
        packed = [next(i for i in range(tin) if i not in used)]
    else:
        packed = [r.number() for _ in range(npacked)]
    return _Folder(coders, binds, packed)


def _read_streams(r: _Reader) -> _Streams:
    s = _Streams()
    have_sub = False
    while True:
        t = r.byte()
        if t == K_END:
            break
        if t == K_PACK_INFO:
            s.pack_pos = r.number()
            n = r.number()
            while True:
                t2 = r.byte()
                if t2 == K_END:
                    break
                if t2 == K_SIZE:
                    s.pack_sizes = [r.number() for _ in range(n)]
                elif t2 == K_CRC:
                    r.digests(n)
                else:
                    raise _bad(f"PackInfo: свойство {t2}")
        elif t == K_UNPACK_INFO:
            if r.byte() != K_FOLDER:
                raise _bad("ожидалась Folder")
            n = r.number()
            if r.byte() != 0:
                raise FeatureNotSupported("Этот вариант .7z (внешние данные заголовка) не поддерживается.")
            s.folders = [_read_folder(r) for _ in range(n)]
            pack_idx = 0
            for f in s.folders:
                f.first_pack = pack_idx
                pack_idx += len(f.packed)
            if r.byte() != K_CODERS_UNPACK:
                raise _bad("ожидалась CodersUnpackSize")
            for f in s.folders:
                f.unpack_sizes = [r.number() for _ in range(sum(c.nout for c in f.coders))]
            while True:
                t2 = r.byte()
                if t2 == K_END:
                    break
                if t2 == K_CRC:
                    for f, c in zip(s.folders, r.digests(n)):
                        f.crc = c
                else:
                    raise _bad(f"UnpackInfo: свойство {t2}")
        elif t == K_SUBSTREAMS:
            have_sub = True
            _read_substreams(r, s)
        else:
            raise _bad(f"StreamsInfo: свойство {t}")
    if not have_sub:
        s.nsub = [1] * len(s.folders)
        s.sub_sizes = [[f.size] for f in s.folders]
        s.sub_crcs = [[f.crc] for f in s.folders]
    return s


def _read_substreams(r: _Reader, s: _Streams) -> None:
    folders = s.folders
    nsub = [1] * len(folders)
    sizes: Optional[List[List[int]]] = None
    crcs: Optional[List[List[Optional[int]]]] = None
    t = r.byte()
    if t == K_NUM_UNPACK:
        nsub = [r.number() for _ in folders]
        t = r.byte()
    if t == K_SIZE:
        sizes = []
        for f, n in zip(folders, nsub):
            if n == 0:
                sizes.append([])
                continue
            part = [r.number() for _ in range(n - 1)]
            last = f.size - sum(part)
            if last < 0:
                raise _bad("размеры файлов не сходятся")
            sizes.append(part + [last])
        t = r.byte()
    if sizes is None:
        sizes = []
        for f, n in zip(folders, nsub):
            if n > 1:
                raise _bad("нет размеров файлов")
            sizes.append([f.size] if n == 1 else [])
    unknown = sum(n for f, n in zip(folders, nsub) if not (n == 1 and f.crc is not None))
    while t != K_END:
        if t == K_CRC:
            flat = iter(r.digests(unknown))
            crcs = []
            for f, n in zip(folders, nsub):
                crcs.append([f.crc] if (n == 1 and f.crc is not None) else [next(flat) for _ in range(n)])
        else:
            raise _bad(f"SubStreamsInfo: свойство {t}")
        t = r.byte()
    if crcs is None:
        crcs = [[f.crc] if (n == 1 and f.crc is not None) else [None] * n for f, n in zip(folders, nsub)]
    s.nsub, s.sub_sizes, s.sub_crcs = nsub, sizes, crcs


def _read_files(r: _Reader) -> List[_Entry]:
    n = r.number()
    entries = [_Entry() for _ in range(n)]
    empty_stream = [False] * n
    empty_file: List[bool] = []
    while True:
        t = r.byte()
        if t == K_END:
            break
        size = r.number()
        data = _Reader(r.take(size))
        if t == K_EMPTY_STREAM:
            empty_stream = data.bits(n)
        elif t == K_EMPTY_FILE:
            empty_file = data.bits(sum(empty_stream))
        elif t == K_NAMES:
            if data.byte() != 0:
                raise FeatureNotSupported("Этот вариант .7z (внешние имена) не поддерживается.")
            raw = data.d[data.p:]
            names = raw.decode("utf-16-le", errors="replace").split("\x00")
            if len(names) < n:
                raise _bad("имён меньше, чем файлов")
            for e, nm in zip(entries, names):
                e.name = nm
        elif t == K_WIN_ATTRS:
            defined = [True] * n if data.byte() else data.bits(n)
            if data.byte() != 0:
                raise FeatureNotSupported("Этот вариант .7z (внешние атрибуты) не поддерживается.")
            for e, d in zip(entries, defined):
                if d:
                    e.attr = data.u32()
        # прочее (время, Dummy, Anti, StartPos…) не нужно
    k = 0
    for i, e in enumerate(entries):
        e.has_stream = not empty_stream[i]
        if not e.has_stream:
            is_empty_file = empty_file[k] if k < len(empty_file) else False
            e.is_dir = not is_empty_file
            k += 1
        if e.attr is not None and e.attr & ATTR_DIRECTORY and not e.has_stream:
            e.is_dir = True
    return entries


# ------------------------------------------------------------------ декодеры

def _arm64_stage(chunks: Iterator[bytes]) -> Iterator[bytes]:
    """BCJ ARM64 (декодирование) на чистом Python — для Python < 3.12, где в lzma нет FILTER_ARM64."""
    pos = 0
    carry = b""
    little = sys.byteorder == "little"
    for chunk in chunks:
        data = carry + chunk
        n = len(data) & ~3
        carry = data[n:]
        if n == 0:
            continue
        words = array.array("I")
        words.frombytes(data[:n])
        if not little:
            words.byteswap()
        base = pos >> 2
        for i in range(len(words)):
            v = words[i]
            if (v >> 26) == 0x25:                                  # BL
                words[i] = 0x94000000 | ((v - (base + i)) & 0x03FFFFFF)
            elif (v & 0x9F000000) == 0x90000000:                   # ADRP
                src = ((v >> 29) & 3) | ((v >> 3) & 0x001FFFFC)
                if (src + 0x00020000) & 0x001C0000:
                    continue
                dest = (src - ((pos + 4 * i) >> 12)) & 0xFFFFFFFF
                v &= 0x9000001F
                v |= (dest & 3) << 29
                v |= (dest & 0x0003FFFC) << 3
                v |= (-(dest & 0x00020000)) & 0x00E00000
                words[i] = v & 0xFFFFFFFF
        if not little:
            words.byteswap()
        pos += n
        yield words.tobytes()
    if carry:
        yield carry


def _lzma_available() -> None:
    if lzma is None:
        raise MissingDependencyError(
            "В Python на этой консоли нет модуля lzma, поэтому .7z не распаковать. "
            "Попросите автора выложить порт в .zip или поставьте утилиту 7z.")


def _lzma_filter(coder: _Coder) -> Optional[dict]:
    """Фильтр liblzma для кодера или None, если его надо делать иначе."""
    cid, p = coder.cid, coder.props
    if cid == b"\x21":
        if len(p) < 1:
            raise _bad("свойства LZMA2")
        b = p[0]
        if b > 40:
            raise _bad("словарь LZMA2")
        return {"id": lzma.FILTER_LZMA2, "dict_size": 0xFFFFFFFF if b == 40 else (2 | (b & 1)) << (b // 2 + 11)}
    if cid == b"\x03\x01\x01":
        if len(p) < 5:
            raise _bad("свойства LZMA")
        d = p[0]
        lc, d = d % 9, d // 9
        return {"id": lzma.FILTER_LZMA1, "dict_size": struct.unpack("<I", p[1:5])[0], "lc": lc,
                "lp": d % 5, "pb": d // 5}
    if cid == b"\x03":
        return {"id": lzma.FILTER_DELTA, "dist": (p[0] if p else 0) + 1}
    name = _BCJ.get(cid)
    attr = _LZMA_FILTER_NAMES.get(name or "")
    if attr and hasattr(lzma, attr):
        return {"id": getattr(lzma, attr)}
    return None


def _lzma_stream(chunks: Iterator[bytes], filters: list) -> Iterator[bytes]:
    dec = lzma.LZMADecompressor(format=lzma.FORMAT_RAW, filters=filters)
    try:
        for chunk in chunks:
            out = dec.decompress(chunk, _BUF)
            while True:
                if out:
                    yield out
                if dec.eof or dec.needs_input:
                    break
                out = dec.decompress(b"", _BUF)
            if dec.eof:
                return
    except lzma.LZMAError as e:
        raise _bad(f"ошибка LZMA: {e}") from e


def _simple_stream(chunks: Iterator[bytes], dec, flush=True) -> Iterator[bytes]:
    try:
        for chunk in chunks:
            out = dec.decompress(chunk)
            if out:
                yield out
        if flush and hasattr(dec, "flush"):
            out = dec.flush()
            if out:
                yield out
    except (zlib.error, OSError, ValueError) as e:
        raise _bad(f"ошибка распаковки: {e}") from e


def _decode_chain(folder: _Folder) -> List[_Coder]:
    """Кодеры в порядке декодирования: от упакованного потока к результату."""
    if any(c.nin != 1 or c.nout != 1 for c in folder.coders):
        raise FeatureNotSupported("Этот вариант .7z (BCJ2 и составные кодеры) не поддерживается.")
    if len(folder.packed) != 1:
        raise FeatureNotSupported("Этот вариант .7z (несколько упакованных потоков) не поддерживается.")
    by_out = {o: i for i, o in folder.binds}
    cur = folder.packed[0]               # индекс входа == индекс кодера (по одному входу на кодер)
    order, seen = [], set()
    while True:
        if cur in seen or cur >= len(folder.coders):
            raise _bad("цепочка кодеров")
        seen.add(cur)
        order.append(folder.coders[cur])
        if cur not in by_out:
            break
        cur = by_out[cur]
    if len(order) != len(folder.coders):
        raise _bad("не все кодеры задействованы")
    return order


def _build_pipeline(folder: _Folder, packed: Iterator[bytes]) -> Iterator[bytes]:
    order = _decode_chain(folder)
    stream = packed
    i = 0
    while i < len(order):
        c = order[i]
        cid = c.cid
        if cid[:1] == b"\x06" and len(cid) == 4:
            raise UnsupportedArchiveError("Архив .7z защищён паролем — такие архивы не поддерживаются.")
        if cid == b"\x00":
            i += 1
            continue
        if cid in (b"\x21", b"\x03\x01\x01"):
            _lzma_available()
            group = [_lzma_filter(c)]
            j = i + 1
            while j < len(order):         # следом идущие BCJ/Delta, которые умеет liblzma, — в той же цепочке
                f = _lzma_filter(order[j])
                if f is None or f["id"] in (lzma.FILTER_LZMA1, lzma.FILTER_LZMA2):
                    break
                group.append(f)
                j += 1
            stream = _lzma_stream(stream, list(reversed(group)))
            i = j
            continue
        if cid == b"\x04\x01\x08":
            stream = _simple_stream(stream, zlib.decompressobj(-15))
        elif cid == b"\x04\x02\x02":
            stream = _simple_stream(stream, bz2.BZ2Decompressor(), flush=False)
        elif _BCJ.get(cid) == "arm64":
            stream = _arm64_stage(stream)
        else:
            raise FeatureNotSupported(
                f"Метод сжатия в .7z не поддерживается (код {cid.hex() or '—'}). Пересоберите архив с LZMA2.")
        i += 1
    return stream


def _folder_stream(f, st: _Streams, folder: _Folder, base: int) -> Iterator[bytes]:
    """Распакованные байты одной папки (folder); читает упакованные данные из открытого файла."""
    offs = base + st.pack_pos + sum(st.pack_sizes[:folder.first_pack])
    size = st.pack_sizes[folder.first_pack]

    def packed():
        f.seek(offs)
        left = size
        while left > 0:
            chunk = f.read(min(_PACK_CHUNK, left))
            if not chunk:
                raise _bad("файл оборван")
            left -= len(chunk)
            yield chunk
    total = folder.size
    got = 0
    for out in _build_pipeline(folder, packed()):
        if got + len(out) > total:
            out = out[:total - got]
        got += len(out)
        if out:
            yield out
        if got >= total:
            return
    if got < total:
        raise _bad("данные оборваны")


# ------------------------------------------------------------------ чтение архива

def _read_archive(f, file_size: int):
    head = f.read(32)
    if len(head) < 32 or head[:6] != SIGNATURE:
        raise UnsupportedArchiveError("Это не архив .7z.")
    start_crc = struct.unpack("<I", head[8:12])[0]
    if zlib.crc32(head[12:32]) & 0xFFFFFFFF != start_crc:
        raise _bad("заголовок")
    nh_off, nh_size, nh_crc = struct.unpack("<QQI", head[12:32])
    if nh_size == 0:
        return _Streams(), []
    if 32 + nh_off + nh_size > file_size:
        raise _bad("файл короче, чем должен быть — вероятно, не докачан")
    f.seek(32 + nh_off)
    data = f.read(nh_size)
    if zlib.crc32(data) & 0xFFFFFFFF != nh_crc:
        raise _bad("контрольная сумма заголовка")
    for _ in range(4):                      # заголовок может быть упакован (иногда повторно)
        r = _Reader(data)
        t = r.byte()
        if t == K_HEADER:
            break
        if t != K_ENCODED_HEADER:
            raise _bad("неизвестный заголовок")
        st = _read_streams(r)
        if not st.folders:
            raise _bad("пустой упакованный заголовок")
        fo = st.folders[0]
        data = b"".join(_folder_stream(f, st, fo, 32))
        if fo.crc is not None and zlib.crc32(data) & 0xFFFFFFFF != fo.crc:
            raise _bad("контрольная сумма упакованного заголовка")
    else:
        raise _bad("слишком глубокая вложенность заголовков")
    streams, entries = _Streams(), []
    while True:
        t = r.byte()
        if t == K_END:
            break
        if t == K_ARCHIVE_PROPS:
            while True:
                pt = r.byte()
                if pt == K_END:
                    break
                r.take(r.number())
        elif t == K_ADD_STREAMS:
            _read_streams(r)
        elif t == K_MAIN_STREAMS:
            streams = _read_streams(r)
        elif t == K_FILES:
            entries = _read_files(r)
        else:
            raise _bad(f"Header: свойство {t}")
    return streams, entries


def list_entries(path) -> List[_Entry]:
    """Имена и размеры файлов (для тестов и отладки)."""
    with open(path, "rb") as f:
        st, entries = _read_archive(f, os.path.getsize(path))
    _assign_streams(st, entries)
    return entries


def _assign_streams(st: _Streams, entries: List[_Entry]) -> None:
    flat = [(fi, si) for fi, n in enumerate(st.nsub) for si in range(n)]
    it = iter(flat)
    for e in entries:
        if e.has_stream:
            try:
                fi, si = next(it)
            except StopIteration:
                raise _bad("файлов больше, чем потоков") from None
            e.size, e.crc = st.sub_sizes[fi][si], st.sub_crcs[fi][si]


# ------------------------------------------------------------------ распаковка

def extract_7z(path, dest, progress: ProgressCB = None, cancel: Optional[threading.Event] = None) -> None:
    path, dest = Path(path), Path(dest)
    rep = Reporter(progress)
    try:
        with open(path, "rb") as f:
            st, entries = _read_archive(f, os.path.getsize(path))
            _assign_streams(st, entries)
            rep.p.files_total = sum(1 for e in entries if not e.is_dir)
            rep.p.bytes_total = sum(e.size for e in entries)
            rep(True)
            todo = iter(entries)

            def next_with_stream():
                for e in todo:
                    if e.has_stream:
                        return e
                    _make_entry(dest, e, b"", rep)
                return None

            for fi, folder in enumerate(st.folders):
                if st.nsub[fi] == 0:
                    continue
                stream = _folder_stream(f, st, folder, 32)
                buf = memoryview(b"")
                for _ in range(st.nsub[fi]):
                    e = next_with_stream()
                    if e is None:
                        raise _bad("файлов меньше, чем потоков")
                    buf = _write_stream(dest, e, stream, buf, rep, cancel)
            for e in todo:               # хвост: пустые файлы и папки после последнего потока
                _make_entry(dest, e, b"", rep)
            rep(True)
    except CancelledError:
        raise
    except UnsupportedArchiveError:
        raise
    except MissingDependencyError:
        raise
    except OSError as e:
        raise wrap_oserror(e, dest) from e
    except (MemoryError, ValueError, struct.error) as e:
        raise _bad(str(e) or type(e).__name__) from e


def _apply_attrs(target: Path, e: _Entry, data: bytes = b"") -> None:
    if e.attr is None or not e.attr & ATTR_UNIX_EXT:
        return
    mode = e.attr >> 16
    if stat.S_ISREG(mode):
        os.chmod(target, stat.S_IMODE(mode) | 0o600)
    elif stat.S_ISDIR(mode):
        os.chmod(target, stat.S_IMODE(mode) | 0o700)


def _is_symlink(e: _Entry) -> bool:
    return e.attr is not None and bool(e.attr & ATTR_UNIX_EXT) and stat.S_ISLNK(e.attr >> 16)


def _make_entry(dest: Path, e: _Entry, data: bytes, rep: Reporter) -> None:
    """Папка или пустой файл (записи без потока данных)."""
    target = safe_target(dest, e.name)
    if e.is_dir:
        target.mkdir(parents=True, exist_ok=True)
        _apply_attrs(target, e)
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"")
    _apply_attrs(target, e)
    rep.p.files_done += 1
    rep.p.current = e.name
    rep()


def _write_stream(dest: Path, e: _Entry, stream: Iterator[bytes], buf: memoryview, rep: Reporter,
                  cancel: Optional[threading.Event]) -> memoryview:
    """Записывает e.size байт из потока папки; возвращает остаток буфера для следующего файла."""
    target = safe_target(dest, e.name)
    target.parent.mkdir(parents=True, exist_ok=True)
    rep.p.current = e.name
    rep()
    left, crc = e.size, 0
    link = _is_symlink(e)
    sink = [] if link else None
    out = None if link else open(target, "wb")
    try:
        while left > 0:
            if cancel is not None and cancel.is_set():
                raise CancelledError("Распаковка отменена.")
            if not buf:
                try:
                    buf = memoryview(next(stream))      # срезы memoryview не копируют данные
                except StopIteration:
                    raise _bad("данные оборваны") from None
            piece, buf = buf[:left], buf[left:]
            left -= len(piece)
            crc = zlib.crc32(piece, crc)
            if link:
                sink.append(piece)
            else:
                out.write(piece)
            rep.p.bytes_done += len(piece)
            rep()
    finally:
        if out is not None:
            out.close()
    if e.crc is not None and crc & 0xFFFFFFFF != e.crc:
        raise UnsupportedArchiveError(
            f"Контрольная сумма файла «{e.name}» не сошлась — архив повреждён или скачан не полностью.")
    if link:
        if target.exists() or target.is_symlink():
            target.unlink()
        os.symlink(b"".join(bytes(x) for x in sink).decode("utf-8", "replace"), target)
    else:
        _apply_attrs(target, e)
    rep.p.files_done += 1
    rep()
    return buf
