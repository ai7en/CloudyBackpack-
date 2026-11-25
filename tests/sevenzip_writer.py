"""Минимальный писатель .7z для тестов (по 7zFormat.txt): LZMA2 / LZMA / Copy / BCJ+LZMA2, солидные папки,
пустые файлы и каталоги, упакованный или обычный заголовок, unix-права в атрибутах."""
import lzma
import struct
import zlib


def num(v: int) -> bytes:
    if v < 0x80:
        return bytes([v])
    for extra in range(1, 9):
        if v < (1 << (7 * (extra + 1))) or extra == 8:
            if extra == 8:
                return b"\xff" + v.to_bytes(8, "little")
            first_mask = (0xFF << (8 - extra)) & 0xFF
            high = v >> (8 * extra)
            return bytes([first_mask | high]) + (v & ((1 << (8 * extra)) - 1)).to_bytes(extra, "little")
    raise ValueError(v)


def bitvec(bits):
    out = bytearray((len(bits) + 7) // 8)
    for i, b in enumerate(bits):
        if b:
            out[i >> 3] |= 0x80 >> (i & 7)
    return bytes(out)


class Item:
    def __init__(self, name, data=None, mode=None, is_dir=False):
        self.name, self.data, self.mode, self.is_dir = name, data or b"", mode, is_dir


def _compress(data: bytes, method: str, bcj=None):
    """-> (packed bytes, coders spec list в порядке 7z: [(id, props)], ...)"""
    if method == "copy":
        return data, [(b"\x00", b"")]
    if method == "lzma2":
        filters = ([{"id": bcj[0]}] if bcj else []) + [{"id": lzma.FILTER_LZMA2, "dict_size": 1 << 20}]
        packed = lzma.compress(data, format=lzma.FORMAT_RAW, filters=filters)
        coders = ([(bcj[1], b"")] if bcj else []) + [(b"\x21", bytes([18]))]   # 18 → словарь 1 МБ
        return packed, coders
    if method == "lzma":
        f = {"id": lzma.FILTER_LZMA1, "dict_size": 1 << 20, "lc": 3, "lp": 0, "pb": 2}
        packed = lzma.compress(data, format=lzma.FORMAT_RAW, filters=[f])
        props = bytes([(2 * 5 + 0) * 9 + 3]) + struct.pack("<I", 1 << 20)
        return packed, [(b"\x03\x01\x01", props)]
    raise ValueError(method)


def _folder_bytes(coders):
    out = bytearray(num(len(coders)))
    for cid, props in coders:
        flag = len(cid) | (0x20 if props else 0)
        out.append(flag)
        out += cid
        if props:
            out += num(len(props)) + props
    # цепочка: кодер 0 — итоговый, его вход привязан к выходу кодера 1 и т.д.
    for i in range(len(coders) - 1):
        out += num(i) + num(i + 1)
    return bytes(out)      # единственный упакованный вход — последний кодер, 7z выводит его сам


def build(items, folders=None, method="lzma2", bcj=None, encode_header=False, bad_crc=False):
    """items: список Item; folders — разбиение индексов файлов с данными по папкам (по умолчанию одна солидная)."""
    streams = [it for it in items if not it.is_dir and it.data]
    if folders is None:
        folders = [list(range(len(streams)))] if streams else []
    packed_all, folder_defs, folder_sizes, pack_sizes = b"", [], [], []
    for grp in folders:
        raw = b"".join(streams[i].data for i in grp)
        packed, coders = _compress(raw, method, bcj)
        packed_all += packed
        pack_sizes.append(len(packed))
        folder_defs.append(_folder_bytes(coders))
        folder_sizes.append([len(raw)] * len(coders))

    h = bytearray([1])
    if folders:
        h.append(4)
        h += bytes([6]) + num(0) + num(len(folders)) + bytes([9]) + b"".join(num(s) for s in pack_sizes) + b"\x00"
        h += bytes([7, 0xB]) + num(len(folders)) + b"\x00" + b"".join(folder_defs)
        h.append(0xC)
        for sizes in folder_sizes:
            h += b"".join(num(s) for s in sizes)
        h += b"\x00"
        h += bytes([8, 0xD]) + b"".join(num(len(g)) for g in folders)
        h.append(9)
        for g in folders:
            for i in g[:-1]:
                h += num(len(streams[i].data))
        h.append(0xA)
        h.append(1)
        for g in folders:
            for i in g:
                crc = zlib.crc32(streams[i].data) & 0xFFFFFFFF
                if bad_crc:
                    crc ^= 1
                h += struct.pack("<I", crc)
        h += b"\x00\x00"
    h.append(5)
    h += num(len(items))
    empty_stream = [it.is_dir or not it.data for it in items]
    if any(empty_stream):
        v = bitvec(empty_stream)
        h += bytes([0xE]) + num(len(v)) + v
        empties = [it for it in items if it.is_dir or not it.data]
        ef = [not it.is_dir for it in empties]
        if any(ef):
            v = bitvec(ef)
            h += bytes([0xF]) + num(len(v)) + v
    names = b"".join(it.name.encode("utf-16-le") + b"\x00\x00" for it in items)
    h += bytes([0x11]) + num(len(names) + 1) + b"\x00" + names
    attrs = []
    for it in items:
        a = 0x10 if it.is_dir else 0x20
        if it.mode is not None:
            a |= 0x8000 | (it.mode << 16)
        attrs.append(a)
    body = b"\x01" + b"\x00" + b"".join(struct.pack("<I", a) for a in attrs)
    h += bytes([0x15]) + num(len(body)) + body
    h += b"\x00\x00"
    header = bytes(h)

    if encode_header:
        hp, hc = _compress(header, "lzma2")
        enc = bytearray([0x17, 6]) + num(len(packed_all)) + num(1) + bytes([9]) + num(len(hp)) + b"\x00"
        enc += bytes([7, 0xB]) + num(1) + b"\x00" + _folder_bytes(hc) + bytes([0xC]) + num(len(header))
        enc += bytes([0xA, 1]) + struct.pack("<I", zlib.crc32(header) & 0xFFFFFFFF) + b"\x00\x00"
        packed_all += hp
        header = bytes(enc)

    nh_off = len(packed_all)
    start = struct.pack("<QQI", nh_off, len(header), zlib.crc32(header) & 0xFFFFFFFF)
    sig = b"7z\xbc\xaf\x27\x1c\x00\x04" + struct.pack("<I", zlib.crc32(start) & 0xFFFFFFFF) + start
    return sig + packed_all + header
