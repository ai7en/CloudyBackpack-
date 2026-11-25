"""Проверки ядра без устройства: поддельный корень ФС + локальный HTTP-сервер.
Запуск: python3 -I -m unittest discover -s tests -v   (из корня проекта)
"""
import functools
import http.server
import io
import os
import shutil
import stat
import sys
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path
from unittest import mock

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
os.environ["NO_PROXY"] = "127.0.0.1,localhost"
os.environ["no_proxy"] = "127.0.0.1,localhost"

from pm_installer.archives import detect_format, extract  # noqa: E402
from pm_installer.clouds import get_provider  # noqa: E402
from pm_installer.clouds.gdrive import GoogleDrive  # noqa: E402
from pm_installer.clouds.mailru import MailRuCloud  # noqa: E402
from pm_installer.errors import (ArchiveLayoutError, ConflictAbort, InstallerError,  # noqa: E402
                                 InvalidLinkError, MissingDependencyError,
                                 UnsupportedArchiveError)
from pm_installer.firmware import detect, env_cfw_name, load_profiles  # noqa: E402
from pm_installer.installer import ConflictAction, PortInstaller  # noqa: E402
from pm_installer.settings import Settings, SettingsStore  # noqa: E402

PROFILES = load_profiles(BASE / "config" / "paths.json")


def make_zip(path: Path, wrapper: str = "", sh="Cool Port.sh", folder="coolport", with_sh=True):
    with zipfile.ZipFile(path, "w") as z:
        if with_sh:
            zi = zipfile.ZipInfo(f"{wrapper}{sh}")
            zi.create_system = 3
            zi.external_attr = (stat.S_IFREG | 0o644) << 16
            z.writestr(zi, "#!/bin/bash\necho hi\n")
        zb = zipfile.ZipInfo(f"{wrapper}{folder}/bin/game")
        zb.create_system = 3
        zb.external_attr = (stat.S_IFREG | 0o755) << 16
        z.writestr(zb, "BIN")
        z.writestr(f"{wrapper}{folder}/data.bin", "DATA")
        z.writestr(f"{wrapper}README.txt", "readme")
        z.writestr("__MACOSX/junk", "x")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="pmtest_"))
        self.root = str(self.tmp / "root")
        self.zip = self.tmp / "port.zip"
        make_zip(self.zip)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def installer(self, fw, policy="ask", resolver=None):
        s = Settings(firmware=fw, conflict_policy=policy, temp_dir=str(self.tmp / "work"))
        return PortInstaller(s, PROFILES, root=self.root, resolver=resolver, allow_sudo=False)

    def p(self, rel):
        return Path(self.root + rel)


class TestLayoutPerFirmware(Base):
    def test_muos_separate_dirs(self):
        rep = self.installer("muos").install_archive(self.zip)
        self.assertTrue(self.p("/mnt/mmc/ports/coolport/data.bin").is_file())
        sh = self.p("/mnt/mmc/ROMS/Ports/Cool Port.sh")
        self.assertTrue(sh.is_file())
        self.assertTrue(os.access(sh, os.X_OK), "у .sh должен быть +x")
        self.assertFalse(self.p("/mnt/mmc/ports/Cool Port.sh").exists())
        self.assertIn("README.txt", rep.ignored)
        self.assertTrue(os.access(self.p("/mnt/mmc/ports/coolport/bin/game"), os.X_OK),
                        "права из zip должны сохраняться")

    def test_rocknix_same_dir(self):
        self.installer("rocknix").install_archive(self.zip)
        self.assertTrue(self.p("/roms/ports/coolport/data.bin").is_file())
        self.assertTrue(self.p("/roms/ports/Cool Port.sh").is_file())

    def test_knulli_userdata(self):
        self.installer("knulli").install_archive(self.zip)
        self.assertTrue(self.p("/userdata/roms/ports/coolport/data.bin").is_file())
        self.assertTrue(self.p("/userdata/roms/ports/Cool Port.sh").is_file())

    def test_knulli_prefers_existing_share(self):
        self.p("/SHARE/roms/ports").mkdir(parents=True)
        self.installer("knulli").install_archive(self.zip)
        self.assertTrue(self.p("/SHARE/roms/ports/Cool Port.sh").is_file())
        self.assertFalse(self.p("/userdata").exists())

    def test_muos_alt_port_dir(self):
        self.p("/mmc/ports").mkdir(parents=True)
        self.installer("muos").install_archive(self.zip)
        self.assertTrue(self.p("/mmc/ports/coolport").is_dir())

    def test_wrapper_dir_is_stripped(self):
        z = self.tmp / "wrapped.zip"
        make_zip(z, wrapper="Outer/")
        self.installer("rocknix").install_archive(z)
        self.assertTrue(self.p("/roms/ports/coolport/data.bin").is_file())
        self.assertFalse(self.p("/roms/ports/Outer").exists())

    def test_workdir_cleaned(self):
        self.installer("rocknix").install_archive(self.zip)
        left = [x for x in (self.tmp / "work").iterdir()]
        self.assertEqual(left, [])


class TestConflicts(Base):
    def setUp(self):
        super().setUp()
        self.installer("rocknix").install_archive(self.zip)
        (self.p("/roms/ports/coolport/old.txt")).write_text("old")

    def test_ask_without_resolver_aborts(self):
        with self.assertRaises(ConflictAbort):
            self.installer("rocknix", "ask").install_archive(self.zip)

    def test_skip(self):
        rep = self.installer("rocknix", "skip").install_archive(self.zip)
        self.assertTrue(self.p("/roms/ports/coolport/old.txt").exists())
        self.assertEqual(len(rep.skipped), 2)

    def test_overwrite(self):
        self.installer("rocknix", "overwrite").install_archive(self.zip)
        self.assertFalse(self.p("/roms/ports/coolport/old.txt").exists())
        self.assertTrue(self.p("/roms/ports/coolport/data.bin").exists())
        self.assertEqual([n for n in os.listdir(self.p("/roms/ports")) if n.startswith(".")], [])

    def test_backup(self):
        rep = self.installer("rocknix", "backup").install_archive(self.zip)
        baks = [n for n in os.listdir(self.p("/roms/ports")) if ".bak-" in n]
        self.assertEqual(len(baks), 2)
        self.assertEqual(len(rep.backups), 2)
        self.assertFalse(any(n.endswith(".sh") for n in baks), "бэкап не должен выглядеть как .sh")
        bak_dir = [n for n in baks if n.startswith("coolport")][0]
        self.assertTrue(self.p(f"/roms/ports/{bak_dir}/old.txt").exists())
        self.assertFalse(self.p("/roms/ports/coolport/old.txt").exists())

    def test_resolver_called_and_abort(self):
        calls = []

        def res(path, kind):
            calls.append((path.name, kind))
            return ConflictAction.ABORT
        with self.assertRaises(ConflictAbort):
            self.installer("rocknix", "ask", res).install_archive(self.zip)
        self.assertEqual(calls[0], ("coolport", "папка"))


class TestBadArchives(Base):
    def test_no_sh(self):
        z = self.tmp / "nosh.zip"
        make_zip(z, with_sh=False)
        with self.assertRaises(ArchiveLayoutError):
            self.installer("rocknix").install_archive(z)

    def test_not_an_archive(self):
        f = self.tmp / "fake.zip"
        f.write_text("<html>error</html>")
        with self.assertRaises(UnsupportedArchiveError):
            self.installer("rocknix").install_archive(f)

    def test_truncated_zip(self):
        f = self.tmp / "cut.zip"
        f.write_bytes(self.zip.read_bytes()[:60])
        with self.assertRaises(InstallerError):
            extract(f, self.tmp / "out")

    def test_zip_slip_rejected(self):
        z = self.tmp / "evil.zip"
        with zipfile.ZipFile(z, "w") as zf:
            zf.writestr("../../evil.sh", "x")
        with self.assertRaises(UnsupportedArchiveError):
            extract(z, self.tmp / "out")
        self.assertFalse((self.tmp.parent / "evil.sh").exists())

    def test_encrypted_zip_flag(self):
        z = self.tmp / "enc.zip"
        make_zip(z)
        raw = bytearray(z.read_bytes())
        raw[6] |= 0x01                       # general purpose flag в локальном заголовке
        idx = raw.find(b"PK\x01\x02")
        raw[idx + 8] |= 0x01                 # и в центральном каталоге
        z.write_bytes(bytes(raw))
        with self.assertRaises(UnsupportedArchiveError):
            extract(z, self.tmp / "out")

    def test_7z_rar_garbage_gives_clean_error(self):
        for magic, ext in ((b"7z\xbc\xaf\x27\x1c", "7z"), (b"Rar!\x1a\x07\x00", "rar")):
            f = self.tmp / f"bad.{ext}"
            f.write_bytes(magic + b"\x00" * 40)
            self.assertEqual(detect_format(f), ext)
            with self.assertRaises((UnsupportedArchiveError, MissingDependencyError)):
                extract(f, self.tmp / f"out_{ext}")

    def test_unknown_firmware(self):
        with self.assertRaises(InstallerError):
            self.installer("nope").install_archive(self.zip)

    def test_auto_without_signals_errors(self):
        with self.assertRaises(InstallerError):
            self.installer("auto").install_archive(self.zip)


class TestDetectAndConfig(Base):
    def test_os_release(self):
        (self.p("/etc")).mkdir(parents=True)
        self.p("/etc/os-release").write_text('NAME="ROCKNIX"\nID=rocknix\n')
        self.assertEqual(detect(PROFILES, self.root), "rocknix")

    def test_marker(self):
        self.p("/opt/muos").mkdir(parents=True)
        self.assertEqual(detect(PROFILES, self.root), "muos")

    def test_autodetect_install(self):
        self.p("/etc").mkdir(parents=True)
        self.p("/etc/os-release").write_text("NAME=Knulli\n")
        self.installer("auto").install_archive(self.zip)
        self.assertTrue(self.p("/userdata/roms/ports/Cool Port.sh").is_file())

    def test_cfw_name_hint_from_portmaster(self):
        self.assertEqual(detect(PROFILES, self.root, "ROCKNIX"), "rocknix")
        self.assertEqual(detect(PROFILES, self.root, "muOS"), "muos")
        self.assertEqual(detect(PROFILES, self.root, "knulli"), "knulli")
        self.assertIsNone(detect(PROFILES, self.root, "SomethingElse"))
        self.p("/opt/muos").mkdir(parents=True)          # подсказка сильнее слабого маркера
        self.assertEqual(detect(PROFILES, self.root, "ROCKNIX"), "rocknix")
        self.p("/userdata/system").mkdir(parents=True)   # неоднозначные маркеры + подсказка = ответ
        self.assertEqual(detect(PROFILES, self.root, "knulli"), "knulli")

    def test_env_cfw_name(self):
        with mock.patch.dict(os.environ, {"CFW_NAME": " ROCKNIX "}):
            self.assertEqual(env_cfw_name(""), "ROCKNIX")
            self.assertEqual(env_cfw_name("/fake"), "", "с --root переменная окружения игнорируется")

    def test_ambiguous_is_none(self):
        self.p("/opt/muos").mkdir(parents=True)
        self.p("/userdata/system").mkdir(parents=True)
        self.assertIsNone(detect(PROFILES, self.root))

    def test_user_profile_extends(self):
        user = self.tmp / "paths.user.json"
        user.write_text('{"firmwares": {"muos_sd2": {"extends": "muos", "label": "muOS SD2", '
                        '"port_dirs": ["/mnt/sdcard/ports"], "script_dirs": ["/mnt/sdcard/ROMS/Ports"]}}}')
        prof = load_profiles(BASE / "config" / "paths.json", user)
        self.assertIn("muos_sd2", prof)
        self.assertEqual(prof["muos_sd2"].port_dirs, ("/mnt/sdcard/ports",))
        self.assertEqual(prof["muos_sd2"].os_release, ())   # detect не наследуется
        s = Settings(firmware="muos_sd2", temp_dir=str(self.tmp / "w"))
        PortInstaller(s, prof, root=self.root, allow_sudo=False).install_archive(self.zip)
        self.assertTrue(self.p("/mnt/sdcard/ROMS/Ports/Cool Port.sh").is_file())

    def test_settings_roundtrip_and_corrupt(self):
        st = SettingsStore(self.tmp / "home")
        s = Settings(url="https://yadi.sk/d/x", cloud="yandex", firmware="muos", conflict_policy="backup",
                     swap_ab=True)
        st.save(s)
        self.assertEqual(st.load(), s)
        st.path.write_text("{not json")
        self.assertEqual(st.load(), Settings())
        st.path.write_text('{"conflict_policy": "zzz", "extra": 1}')
        self.assertEqual(st.load().conflict_policy, "ask")


class FakeResp:
    def __init__(self, text="", js=None, headers=None, status=200):
        self.text, self._js, self.headers, self.status_code = text, js, headers or {}, status

    def json(self):
        return self._js

    def close(self):
        pass


class TestProviders(unittest.TestCase):
    def test_autodetect(self):
        cases = {"https://disk.yandex.ru/d/abc": "yandex", "https://yadi.sk/d/abc": "yandex",
                 "https://drive.google.com/file/d/AbC_1-x/view?usp=sharing": "gdrive",
                 "https://cloud.mail.ru/public/aB3d/Ef5g": "mailru",
                 "https://example.com/files/port.zip": "direct"}
        for url, key in cases.items():
            self.assertEqual(get_provider(url).key, key, url)
        with self.assertRaises(InvalidLinkError):
            get_provider("ftp://x/y")
        with self.assertRaises(InvalidLinkError):
            get_provider("  ")

    def test_yandex_file_and_folder(self):
        prov = get_provider("https://yadi.sk/d/abc")
        with mock.patch.object(type(prov), "_request",
                               return_value=FakeResp(js={"type": "file", "name": "a.zip", "size": 5})):
            f = prov.list_files("https://yadi.sk/d/abc")
        self.assertEqual((f[0].name, f[0].size), ("a.zip", 5))
        items = [{"type": "file", "name": "p1.7z", "path": "/p1.7z", "size": 1},
                 {"type": "file", "name": "notes.txt", "path": "/notes.txt"},
                 {"type": "dir", "name": "sub", "path": "/sub"}]
        sub = [{"type": "file", "name": "deep.zip", "path": "/sub/deep.zip", "size": 2}]

        def fake(self_, method, url, **kw):
            if (kw.get("params") or {}).get("path") == "/sub":
                return FakeResp(js={"type": "dir", "_embedded": {"items": sub, "total": 1}})
            return FakeResp(js={"type": "dir", "_embedded": {"items": items, "total": 3}})
        with mock.patch.object(type(prov), "_request", fake):
            f = prov.list_files("https://yadi.sk/d/abc")
        self.assertEqual([x.name for x in f], ["p1.7z", "deep.zip"])      # подпапки обходятся
        self.assertEqual([x.meta["display"] for x in f], ["p1.7z", "sub/deep.zip"])
        self.assertEqual(f[0].meta["path"], "/p1.7z")
        self.assertTrue(all(x.meta["folder"] for x in f))

    def test_folder_with_single_archive_still_shows_list(self):
        from pm_installer.clouds import RemoteFile
        inst = PortInstaller.__new__(PortInstaller)
        inst.cancel = None
        one = [RemoteFile("a.zip", 1, {"folder": True, "display": "x/a.zip"})]
        asked = []
        inst.list_remote = lambda url=None: (mock.Mock(download=mock.Mock(side_effect=RuntimeError("stop"))), one)
        inst._check_cancel = lambda: None
        inst.temp_root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, inst.temp_root, True)
        with self.assertRaises(RuntimeError):
            inst.install(choose=lambda files: asked.append(files) or files)
        self.assertEqual(len(asked), 1)      # список показан, хотя архив один
        asked.clear()
        one[0].meta.pop("folder")            # одиночный файл по ссылке — без вопросов
        with self.assertRaises(RuntimeError):
            inst.install(choose=lambda files: asked.append(files) or files)
        self.assertEqual(asked, [])

    def test_direct_setting_still_uses_yandex_for_yandex_link(self):
        self.assertEqual(get_provider("https://disk.yandex.ru/d/abc", "direct").key, "yandex")
        self.assertEqual(get_provider("https://example.com/a.zip", "direct").key, "direct")

    def test_direct_html_page_is_explained(self):
        from pm_installer.clouds.direct import DirectLink
        from pm_installer.clouds import RemoteFile
        prov = DirectLink()
        resp = FakeResp(js={})
        resp.headers = {"Content-Type": "text/html; charset=utf-8"}
        resp.close = lambda: None
        with mock.patch.object(DirectLink, "_request", return_value=resp):
            with self.assertRaises(InvalidLinkError) as cm:
                prov.open_stream(RemoteFile("", None, {"url": "https://x/y"}))
        self.assertIn("веб-страница", str(cm.exception))

    def test_yandex_download_calls_documented_endpoint(self):
        prov = get_provider("https://yadi.sk/d/abc")
        seen = []

        def fake(self_, method, url, **kw):
            seen.append((url, kw.get("params")))
            return FakeResp(js={"href": "https://dl.example/x"})
        with mock.patch.object(type(prov), "_request", fake):
            from pm_installer.clouds import RemoteFile
            prov.open_stream(RemoteFile("a.zip", None, {"public_key": "K", "path": "/a.zip"}))
        self.assertEqual(seen[0][0], "https://cloud-api.yandex.net/v1/disk/public/resources/download")
        self.assertEqual(seen[0][1], {"public_key": "K", "path": "/a.zip"})
        self.assertEqual(seen[1][0], "https://dl.example/x")

    def test_gdrive_parse(self):
        self.assertEqual(GoogleDrive.parse("https://drive.google.com/file/d/1AbC_d-E/view"), ("file", "1AbC_d-E"))
        self.assertEqual(GoogleDrive.parse("https://drive.google.com/open?id=XYZ123"), ("file", "XYZ123"))
        self.assertEqual(GoogleDrive.parse("https://drive.google.com/drive/folders/F0ld3r?usp=x"), ("folder", "F0ld3r"))
        with self.assertRaises(InvalidLinkError):
            GoogleDrive.parse("https://drive.google.com/")

    def test_gdrive_confirm_form(self):
        page = ('<form id="download-form" action="https://drive.usercontent.google.com/download" method="get">'
                '<input type="hidden" name="id" value="FID"><input type="hidden" name="export" value="download">'
                '<input type="hidden" name="confirm" value="t"><input type="hidden" name="uuid" value="u&amp;1">'
                '</form>')
        action, fields = GoogleDrive._parse_form(page)
        self.assertEqual(action, "https://drive.usercontent.google.com/download")
        self.assertEqual(fields, {"id": "FID", "export": "download", "confirm": "t", "uuid": "u&1"})
        self.assertIsNone(GoogleDrive._parse_form("<html>quota</html>"))

    def test_mailru_parse(self):
        self.assertEqual(MailRuCloud.parse("https://cloud.mail.ru/public/aB3d/Ef5g"), "aB3d/Ef5g")
        with self.assertRaises(InvalidLinkError):
            MailRuCloud.parse("https://cloud.mail.ru/home/")
        page = '..."weblink_get": [{"count": "1", "url": "https:\\/\\/cloclo5.cloud.mail.ru\\/weblink\\/get"}]...'
        self.assertEqual(MailRuCloud.find_base(page), "https://cloclo5.cloud.mail.ru/weblink/get")
        with self.assertRaises(InvalidLinkError):
            MailRuCloud.find_base("<html></html>")


class TestEndToEndHttp(Base):
    """Скачивание через DirectLink с локального HTTP-сервера, затем установка."""

    def setUp(self):
        super().setUp()
        srv_dir = self.tmp / "srv"
        srv_dir.mkdir()
        shutil.copy(self.zip, srv_dir / "My Port.zip")
        shutil.copy(self.zip, srv_dir / "noext")
        handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(srv_dir))
        handler.log_message = lambda *a, **k: None
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.port = self.httpd.server_address[1]

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        super().tearDown()

    def test_download_and_install(self):
        events = []
        inst = self.installer("rocknix")
        inst._progress_cb = lambda st, fr, msg: events.append(st)
        reps = inst.install(f"http://127.0.0.1:{self.port}/My%20Port.zip")
        self.assertEqual(len(reps), 1)
        self.assertTrue(self.p("/roms/ports/Cool Port.sh").is_file())
        self.assertIn("download", events)
        self.assertIn("extract", events)
        self.assertEqual(list((self.tmp / "work" / "download").iterdir()), [], "архив удаляется после установки")

    def test_extensionless_url_detected_by_magic(self):
        self.installer("rocknix").install(f"http://127.0.0.1:{self.port}/noext")
        self.assertTrue(self.p("/roms/ports/Cool Port.sh").is_file())

    def test_404_is_invalid_link(self):
        with self.assertRaises(InvalidLinkError):
            self.installer("rocknix").install(f"http://127.0.0.1:{self.port}/missing.zip")

    def test_connection_refused_is_network_error(self):
        from pm_installer.errors import NetworkError
        self.httpd.shutdown()
        self.httpd.server_close()
        with self.assertRaises(NetworkError):
            self.installer("rocknix").install(f"http://127.0.0.1:{self.port}/x.zip")

    def test_cancel(self):
        from pm_installer.errors import CancelledError
        inst = self.installer("rocknix")
        inst.cancel.set()
        with self.assertRaises(CancelledError):
            inst.install(f"http://127.0.0.1:{self.port}/My%20Port.zip")


if __name__ == "__main__":
    unittest.main()


class ProgressMessages(unittest.TestCase):
    def test_extract_and_install_details(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        arc = tmp / "Game.zip"
        with zipfile.ZipFile(arc, "w") as z:
            z.writestr("Game.sh", "x")
            z.writestr("game/data.bin", b"0" * 3000)
        msgs = []
        st = Settings(firmware="rocknix", temp_dir=str(tmp / "t"))
        inst = PortInstaller(st, load_profiles(BASE / "config" / "paths.json"), root=str(tmp / "root"),
                             progress=lambda stage, frac, msg: msgs.append((stage, frac, msg)))
        inst.install_archive(arc)
        ext = [m for st_, f, m in msgs if st_ == "extract" and "Файлов: 2 из 2" in m]
        self.assertTrue(ext, msgs)
        self.assertIn("Распаковка Game.zip", ext[-1])
        self.assertIn("2.9 КБ из 2.9 КБ", ext[-1])          # 3001 байт
        self.assertTrue(any("Копирование" in m and "из 2" in m for st_, f, m in msgs if st_ == "install"))

    def test_batch_prefix_and_download_speed(self):
        inst = PortInstaller(Settings(), {}, root="")
        inst._batch = (2, 3)
        self.assertEqual(inst._title("Загрузка X"), "[2 из 3] Загрузка X")
        inst._batch = (1, 1)
        self.assertEqual(inst._title("Загрузка X"), "Загрузка X")
        self.assertEqual(inst._clock(125), "2:05")


class NormalizeUrl(unittest.TestCase):
    def test_adds_scheme(self):
        from pm_installer.clouds import normalize_url, get_provider
        self.assertEqual(normalize_url("disk.yandex.ru/d/AbC"), "https://disk.yandex.ru/d/AbC")
        self.assertEqual(normalize_url(" http://x.y/z "), "http://x.y/z")
        self.assertEqual(normalize_url("abc"), "abc")
        self.assertEqual(get_provider("disk.yandex.ru/d/AbC").key, "yandex")
