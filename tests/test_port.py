"""Проверка пакета порта: сборка, структура zip, скрипт запуска и «установщик ставит сам себя»."""
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import build_port  # noqa: E402
from pm_installer.firmware import load_profiles  # noqa: E402
from pm_installer.installer import PortInstaller  # noqa: E402
from pm_installer.layout import analyze  # noqa: E402
from pm_installer.settings import Settings  # noqa: E402
from pm_installer.archives import extract  # noqa: E402

PROFILES = load_profiles(BASE / "config" / "paths.json")
SCRIPT = BASE / "port" / "Cloud Port Installer.sh"


class TestLauncherScript(unittest.TestCase):
    def setUp(self):
        self.text = SCRIPT.read_text("utf-8")

    def test_syntax_and_line_endings(self):
        self.assertEqual(subprocess.run(["bash", "-n", str(SCRIPT)]).returncode, 0)
        self.assertNotIn("\r", self.text)
        self.assertTrue(self.text.startswith("#!/bin/bash\n"))

    def test_follows_portmaster_template(self):
        for needle in ('controlfolder="/opt/system/Tools/PortMaster"', 'controlfolder="/opt/tools/PortMaster"',
                       'controlfolder="$XDG_DATA_HOME/PortMaster"', 'controlfolder="/roms/ports/PortMaster"',
                       "source $controlfolder/control.txt", "get_controls", "GAMEDIR=", "pm_finish",
                       'SDL_GAMECONTROLLERCONFIG="$sdl_controllerconfig"', "runtime_check", 'PM_CAN_MOUNT'):
            self.assertIn(needle, self.text, needle)
        self.assertLess(self.text.index("source $controlfolder/control.txt"), self.text.index("get_controls"))
        self.assertLess(self.text.index("get_controls"), self.text.index("GAMEDIR="))
        self.assertTrue(self.text.rstrip().endswith("pm_finish"))

    def test_mounts_python_before_running_and_unmounts_after(self):
        run = self.text.index("$PYTHON_CMD main.py")
        self.assertLess(self.text.index("mount -o loop"), run)
        self.assertGreater(self.text.rindex("umount"), run)

    def _run_launcher(self, mount_body='echo "MOUNT $*"', mounts_line=None, make_py_home=True):
        """Скрипт целиком, с поддельным PortMaster. Возвращает (CompletedProcess, log, tmp, pyhome)."""
        tmp = Path(tempfile.mkdtemp(prefix="pmsh_"))
        self.addCleanup(shutil.rmtree, tmp, True)
        ctl = tmp / "PortMaster"
        (ctl / "libs").mkdir(parents=True)
        (ctl / "libs" / "python_3.11.squashfs").write_text("fake")
        (ctl / "control.txt").write_text(
            'directory="data"\nESUDO=""\nsdl_controllerconfig="PAD"\nget_controls(){ echo GET_CONTROLS; }\n'
            'pm_message(){ echo "PM_MESSAGE $*"; }\npm_finish(){ echo PM_FINISH; }\n')
        fake_bin = tmp / "bin"
        fake_bin.mkdir()
        pyhome = tmp / "pyhome"
        pybody = 'echo "PY args=$* HOME=$PM_INSTALLER_HOME PAD=$SDL_GAMECONTROLLERCONFIG PP=$PYTHONPATH PH=$PYTHONHOME"'
        for name, body in (("mount", mount_body), ("umount", 'echo "UMOUNT $*"')):
            f = fake_bin / name
            f.write_text("#!/bin/sh\n" + body + "\n")
            f.chmod(0o755)
        if make_py_home:   # «смонтированный» рантайм: mount-заглушка его не создаёт, поэтому кладём заранее
            (pyhome / "bin").mkdir(parents=True)
            (pyhome / "lib" / "python3.11").mkdir(parents=True)
            py = pyhome / "bin" / "python3"
            py.write_text("#!/bin/sh\n" + pybody + "\n")
            py.chmod(0o755)
        pkg = tmp / "pkg"
        (pkg / "cloudportinstaller").mkdir(parents=True)
        shutil.copy(SCRIPT, pkg / SCRIPT.name)
        (pkg / "cloudportinstaller" / "main.py").write_text("")
        env = {"PATH": f"{fake_bin}:/usr/bin:/bin", "HOME": str(tmp), "XDG_DATA_HOME": str(tmp),
               "PM_CAN_MOUNT": "Y", "PM_PYHOME": str(tmp / ("pyhome" if make_py_home else "nopy"))}
        if mounts_line is not None:
            mf = tmp / "mounts"
            mf.write_text(mounts_line.replace("@PY@", str(pyhome)) + "\n")
            env["PM_MOUNTS"] = str(mf)
        r = subprocess.run(["bash", str(pkg / SCRIPT.name)], capture_output=True, text=True, env=env,
                           cwd=str(tmp), timeout=60)
        log = (pkg / "cloudportinstaller" / "log.txt").read_text("utf-8")
        return r, log, tmp, pyhome

    def test_runs_with_fake_portmaster(self):
        r, log, tmp, _ = self._run_launcher()
        out = r.stdout + r.stderr
        pkg = tmp / "pkg"
        self.assertEqual(r.returncode, 0, out)
        self.assertIn("GET_CONTROLS", out)          # get_controls вызывается до перенаправления в log.txt
        for line in ("MOUNT -o loop", "PY args=main.py", "PAD=PAD", "PM_FINISH"):
            self.assertIn(line, log, line)
        self.assertIn(str(pkg / "cloudportinstaller" / "data"), log)
        self.assertIn(f"{tmp / 'PortMaster'}/pylibs", log)
        self.assertLess(log.index("MOUNT -o loop"), log.index("PY args"))
        self.assertLess(log.index("PY args"), log.rindex("UMOUNT"))
        self.assertLess(log.rindex("UMOUNT"), log.index("PM_FINISH"))

    def test_busy_loop_without_python_fails_cleanly(self):
        """mount падает «busy», рабочего python нигде нет: аккуратный отказ, а не системный python."""
        r, log, _, _ = self._run_launcher(
            mount_body='echo "mount: already mounted or mount point busy" >&2; exit 32',
            mounts_line="", make_py_home=False)
        self.assertEqual(r.returncode, 1, log)
        self.assertIn("ОШИБКА: рантайм python не смонтирован", log)
        self.assertNotIn("PY args", log)

    def test_busy_loop_reuses_mount_found_elsewhere(self):
        """Как на консоли: mount падает «busy», а рантайм уже смонтирован в другой папке — берём его."""
        other = Path(tempfile.mkdtemp(prefix="pmsh_other_"))
        self.addCleanup(shutil.rmtree, other, True)
        (other / "bin").mkdir()
        (other / "lib" / "python3.11").mkdir(parents=True)
        py = other / "bin" / "python3"
        py.write_text('#!/bin/sh\necho "PY args=$* PH=$PYTHONHOME"\n')
        py.chmod(0o755)
        r, log, _, _ = self._run_launcher(
            mount_body='echo "mount: busy" >&2; exit 32',
            mounts_line=f"/dev/loop3 {other} squashfs ro 0 0", make_py_home=False)
        self.assertEqual(r.returncode, 0, log)
        self.assertIn(f"PY args=main.py PH={other}", log)
        self.assertIn("Использую уже смонтированный python", log)


class TestPackage(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        assert build_port.main() == 0
        cls.zip = build_port.OUT

    def test_structure_and_modes(self):
        z = zipfile.ZipFile(self.zip)
        names = z.namelist()
        self.assertEqual(names[0], "Cloud Port Installer.sh")
        self.assertEqual([n for n in names if "/" not in n], ["Cloud Port Installer.sh"], "в корне только скрипт")
        for must in ("cloudportinstaller/main.py", "cloudportinstaller/pm_installer/gui.py",
                     "cloudportinstaller/pm_installer/backends/sdl2_backend.py",
                     "cloudportinstaller/config/paths.json", "cloudportinstaller/assets/DejaVuSans.ttf",
                     "cloudportinstaller/licenses/DejaVu-LICENSE.txt", "cloudportinstaller/port.json",
                     "cloudportinstaller/gameinfo.xml", "cloudportinstaller/README.md",
                     "cloudportinstaller/screenshot.png"):
            self.assertIn(must, names)
        self.assertFalse([n for n in names if "__pycache__" in n or n.endswith(".pyc") or "/tests/" in n
                          or "/data/" in n])
        mode = z.getinfo("Cloud Port Installer.sh").external_attr >> 16
        self.assertTrue(mode & stat.S_IXUSR)

    def test_port_json_valid(self):
        import json
        j = json.loads(zipfile.ZipFile(self.zip).read("cloudportinstaller/port.json"))
        self.assertEqual(j["version"], 2)
        self.assertEqual(j["items"], ["Cloud Port Installer.sh", "cloudportinstaller"])
        self.assertEqual(j["attr"]["runtime"], "python_3.11.squashfs")

    def test_installer_installs_itself_on_all_firmwares(self):
        cases = {"muos": ("/mnt/mmc/ports", "/mnt/mmc/ROMS/Ports"),
                 "rocknix": ("/roms/ports", "/roms/ports"),
                 "knulli": ("/userdata/roms/ports", "/userdata/roms/ports")}
        for fw, (pdir, sdir) in cases.items():
            tmp = Path(tempfile.mkdtemp(prefix="pmself_"))
            self.addCleanup(shutil.rmtree, tmp, True)
            root = str(tmp / "root")
            s = Settings(firmware=fw, temp_dir=str(tmp / "w"))
            PortInstaller(s, PROFILES, root=root, allow_sudo=False).install_archive(self.zip)
            sh = Path(root + sdir) / "Cloud Port Installer.sh"
            self.assertTrue(sh.is_file(), fw)
            self.assertTrue(os.access(sh, os.X_OK), fw)
            self.assertTrue((Path(root + pdir) / "cloudportinstaller" / "main.py").is_file(), fw)

    def test_extracted_port_starts_and_reports_missing_graphics(self):
        """main.py из распакованного порта: без pygame/PySDL2 — понятная ошибка и код 1."""
        tmp = Path(tempfile.mkdtemp(prefix="pmrun_"))
        self.addCleanup(shutil.rmtree, tmp, True)
        extract(self.zip, tmp / "x")
        port = tmp / "x" / "cloudportinstaller"
        env = {**os.environ, "PM_INSTALLER_HOME": str(tmp / "data"), "PM_BACKEND": "sdl2"}
        r = subprocess.run([sys.executable, "main.py"], cwd=port, env=env, stdin=subprocess.DEVNULL,
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("sdl2", r.stderr)
        r = subprocess.run([sys.executable, "main.py", "--detect"], cwd=port, env={**env, "CFW_NAME": "ROCKNIX"},
                           stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("ROCKNIX", r.stdout)


if __name__ == "__main__":
    unittest.main()
