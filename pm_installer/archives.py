"""Распаковка .zip (zipfile), .7z (py7zr), .rar (rarfile) с резервом на системные утилиты."""
import logging
import os
import shutil
import stat
import subprocess
import threading
import zipfile
from pathlib import Path
from typing import Callable, Optional

from .errors import (CancelledError, MissingDependencyError, UnsupportedArchiveError,
                     wrap_oserror)

log = logging.getLogger("pm_installer.archives")

Progress = Optional[Callable[[float], None]]

_MAGIC = ((b"PK\x03\x04", "zip"), (b"PK\x05\x06", "zip"),
          (b"7z\xbc\xaf\x27\x1c", "7z"), (b"Rar!\x1a\x07", "rar"))

_CLI_7Z = [["7z", "x", "-y", "-bd", "-o{dest}", "{src}"],
           ["7zz", "x", "-y", "-bd", "-o{dest}", "{src}"],
           ["7za", "x", "-y", "-bd", "-o{dest}", "{src}"],
           ["bsdtar", "-xf", "{src}", "-C", "{dest}"]]
_CLI_RAR = [["unrar", "x", "-y", "-idq", "{src}", "{dest}/"],
            ["bsdtar", "-xf", "{src}", "-C", "{dest}"]] + _CLI_7Z[:3]


def detect_format(path: Path) -> str:
    """Формат определяется по сигнатуре, а не по расширению."""
    try:
        with open(path, "rb") as f:
            head = f.read(8)
    except OSError as e:
        raise wrap_oserror(e, path) from e
    for magic, fmt in _MAGIC:
        if head.startswith(magic):
            return fmt
    raise UnsupportedArchiveError(
        f"«{Path(path).name}» не является архивом .zip/.7z/.rar (возможно, скачалась страница "
        "ошибки или файл повреждён).")


def extract(path: Path, dest: Path, progress: Progress = None,
            cancel: Optional[threading.Event] = None) -> str:
    path, dest = Path(path), Path(dest)
    fmt = detect_format(path)
    dest.mkdir(parents=True, exist_ok=True)
    log.info("Распаковка %s (%s) → %s", path.name, fmt, dest)
    {"zip": _extract_zip, "7z": _extract_7z, "rar": _extract_rar}[fmt](path, dest, progress, cancel)
    _purge_escaping_links(dest)
    return fmt


# ------------------------------------------------------------------ zip

def _extract_zip(path, dest, progress, cancel):
    root = dest.resolve()
    try:
        with zipfile.ZipFile(path) as zf:
            infos = zf.infolist()
            for i, info in enumerate(infos):
                if cancel is not None and cancel.is_set():
                    raise CancelledError("Распаковка отменена.")
                target = (root / info.filename).resolve()
                if target != root and root not in target.parents:
                    raise UnsupportedArchiveError(f"Небезопасный путь в архиве: {info.filename}")
                if info.flag_bits & 0x1:
                    raise UnsupportedArchiveError("Архив защищён паролем — такие архивы не поддерживаются.")
                zf.extract(info, root)
                mode = info.external_attr >> 16
                if info.create_system == 3 and mode and stat.S_ISREG(mode):
                    os.chmod(target, stat.S_IMODE(mode) | 0o600)  # сохраняем +x у бинарников
                if progress:
                    progress((i + 1) / len(infos))
    except zipfile.BadZipFile as e:
        raise UnsupportedArchiveError("Архив .zip повреждён или не докачан.") from e
    except (NotImplementedError, RuntimeError) as e:
        raise UnsupportedArchiveError(f"Не удалось распаковать .zip: {e}") from e
    except OSError as e:
        raise wrap_oserror(e, dest) from e


# ------------------------------------------------------------------ 7z

def _extract_7z(path, dest, progress, cancel):
    try:
        import py7zr
    except ImportError:
        py7zr = None
    err: Optional[Exception] = None
    if py7zr is not None:
        try:
            with py7zr.SevenZipFile(path, "r") as z:
                if z.needs_password():
                    raise UnsupportedArchiveError("Архив защищён паролем — такие архивы не поддерживаются.")
                z.extractall(path=dest)
            return
        except UnsupportedArchiveError:
            raise
        except OSError as e:
            raise wrap_oserror(e, dest) from e
        except Exception as e:  # py7zr бросает много разных исключений
            err = e
            log.warning("py7zr не справился (%s) — пробую системные утилиты", e)
            _clean_dir(dest)
    if _run_cli(_CLI_7Z, path, dest):
        return
    if py7zr is None:
        raise MissingDependencyError("Для .7z нужна библиотека py7zr (pip install py7zr) или утилита 7z.")
    raise UnsupportedArchiveError(f"Не удалось распаковать .7z: {err}")


# ------------------------------------------------------------------ rar

def _extract_rar(path, dest, progress, cancel):
    try:
        import rarfile
    except ImportError:
        rarfile = None
    err: Optional[Exception] = None
    if rarfile is not None:
        try:
            with rarfile.RarFile(str(path)) as rf:
                if rf.needs_password():
                    raise UnsupportedArchiveError("Архив защищён паролем — такие архивы не поддерживаются.")
                rf.extractall(str(dest))
            return
        except UnsupportedArchiveError:
            raise
        except OSError as e:
            raise wrap_oserror(e, dest) from e
        except Exception as e:  # rarfile.RarCannotExec и пр. — пробуем unrar/bsdtar/7z напрямую
            err = e
            log.warning("rarfile не справился (%s) — пробую системные утилиты", e)
            _clean_dir(dest)
    if _run_cli(_CLI_RAR, path, dest):
        return
    if rarfile is None:
        raise MissingDependencyError(
            "Для .rar нужна библиотека rarfile (pip install rarfile) и утилита unrar/bsdtar/7z.")
    raise UnsupportedArchiveError(
        f"Не удалось распаковать .rar ({err}). Установите unrar, bsdtar или 7z.")


# ------------------------------------------------------------------ утилиты

def _run_cli(templates, src: Path, dest: Path) -> bool:
    for tpl in templates:
        exe = shutil.which(tpl[0])
        if not exe:
            continue
        argv = [exe] + [a.format(src=str(src), dest=str(dest)) for a in tpl[1:]]
        try:
            r = subprocess.run(argv, capture_output=True, timeout=900)
        except (OSError, subprocess.SubprocessError) as e:
            log.warning("%s: %s", tpl[0], e)
            continue
        if r.returncode == 0:
            return True
        log.warning("%s завершился с кодом %s: %s", tpl[0], r.returncode, r.stderr[-300:])
        _clean_dir(dest)
    return False


def _clean_dir(d: Path) -> None:
    for p in Path(d).iterdir():
        if p.is_dir() and not p.is_symlink():
            shutil.rmtree(p, ignore_errors=True)
        else:
            try:
                p.unlink()
            except OSError:
                pass


def _purge_escaping_links(dest: Path) -> None:
    """Удаляет симлинки, ведущие за пределы распакованного каталога."""
    root = os.path.realpath(dest)
    for base, dirs, files in os.walk(dest):
        for name in dirs + files:
            p = os.path.join(base, name)
            if os.path.islink(p):
                real = os.path.realpath(p)
                if real != root and not real.startswith(root + os.sep):
                    log.warning("Удалена небезопасная ссылка из архива: %s", p)
                    os.unlink(p)
