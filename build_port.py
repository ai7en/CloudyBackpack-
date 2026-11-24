#!/usr/bin/env python3
"""Собирает dist/cloudportinstaller.zip в формате порта PortMaster:

    Cloud Port Installer.sh          <- запускается из меню Ports
    cloudportinstaller/              <- папка порта (код, config, шрифт, метаданные)

Запуск: python3 build_port.py
"""
import stat
import subprocess
import sys
import zipfile
from pathlib import Path

BASE = Path(__file__).resolve().parent
PORT_DIR = "cloudportinstaller"
SCRIPT = "Cloud Port Installer.sh"
OUT = BASE / "dist" / f"{PORT_DIR}.zip"


def files():
    """(путь на диске, путь в архиве, права)"""
    script = BASE / "port" / SCRIPT
    yield script, SCRIPT, 0o755
    yield BASE / "main.py", f"{PORT_DIR}/main.py", 0o755
    for sub in ("pm_installer", "config", "assets", "licenses"):
        for p in sorted((BASE / sub).rglob("*")):
            if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc" and p.name != "screenshot.png":
                yield p, f"{PORT_DIR}/{p.relative_to(BASE).as_posix()}", 0o644
    for meta in ("port.json", "gameinfo.xml", "README.md"):
        yield BASE / "port" / meta, f"{PORT_DIR}/{meta}", 0o644
    yield BASE / "assets" / "screenshot.png", f"{PORT_DIR}/screenshot.png", 0o644


def main() -> int:
    script = (BASE / "port" / SCRIPT).read_bytes()
    if b"\r" in script:
        print("Ошибка: в .sh есть CRLF — скрипт не запустится на консоли.", file=sys.stderr)
        return 1
    if subprocess.run(["bash", "-n", str(BASE / "port" / SCRIPT)]).returncode != 0:
        print("Ошибка: синтаксис .sh неверный.", file=sys.stderr)
        return 1
    OUT.parent.mkdir(exist_ok=True)
    with zipfile.ZipFile(OUT, "w", zipfile.ZIP_DEFLATED) as z:
        for src, arc, mode in files():
            zi = zipfile.ZipInfo(arc, date_time=(2026, 11, 24, 0, 0, 0))
            zi.create_system = 3
            zi.compress_type = zipfile.ZIP_DEFLATED
            zi.external_attr = (stat.S_IFREG | mode) << 16
            z.writestr(zi, src.read_bytes())
    names = zipfile.ZipFile(OUT).namelist()
    print(f"{OUT}: {len(names)} файлов, {OUT.stat().st_size // 1024} КБ")
    return 0


if __name__ == "__main__":
    sys.exit(main())
