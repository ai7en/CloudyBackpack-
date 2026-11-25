"""Оркестратор: ссылка → загрузка → распаковка → анализ → раскладка по путям прошивки."""
import enum
import logging
import os
import shutil
import stat
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from .archives import extract
from .clouds import CloudProvider, RemoteFile, get_provider, normalize_url
from .constants import is_archive_name
from .errors import (CancelledError, ConflictAbort, InstallerError, InvalidLinkError,
                     wrap_oserror)
from .firmware import FirmwareProfile, detect, env_cfw_name, resolve_targets
from .layout import analyze
from .settings import Settings

log = logging.getLogger("pm_installer.installer")


class ConflictAction(enum.Enum):
    OVERWRITE = "overwrite"
    SKIP = "skip"
    BACKUP = "backup"
    ABORT = "abort"


ConflictResolver = Callable[[Path, str], ConflictAction]      # (существующий путь, "папка"/"файл")
ProgressFn = Callable[[str, float, str], None]                # (этап, доля 0..1 или -1, сообщение)
Chooser = Callable[[List[RemoteFile]], List[RemoteFile]]


@dataclass
class InstallReport:
    archive: str
    firmware: str
    port_dir: Path
    script_dir: Path
    installed: List[str] = field(default_factory=list)
    skipped: List[str] = field(default_factory=list)
    backups: List[str] = field(default_factory=list)
    ignored: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    def summary(self) -> List[str]:
        lines = [f"Архив: {self.archive}", f"Прошивка: {self.firmware}"]
        lines += [f"Установлено: {x}" for x in self.installed]
        lines += [f"Пропущено: {x}" for x in self.skipped]
        lines += [f"Резервная копия: {x}" for x in self.backups]
        lines += [f"Не установлено (не часть порта): {x}" for x in self.ignored]
        lines += [f"Внимание: {x}" for x in self.warnings]
        return lines


class PortInstaller:
    def __init__(self, settings: Settings, profiles: Dict[str, FirmwareProfile], *,
                 root: str = "", resolver: Optional[ConflictResolver] = None,
                 progress: Optional[ProgressFn] = None,
                 cancel: Optional[threading.Event] = None,
                 temp_default: Optional[Path] = None, allow_sudo: bool = True,
                 session=None):
        self.settings = settings
        self.profiles = profiles
        self.root = root
        self.resolver = resolver
        self._progress_cb = progress
        self.cancel = cancel or threading.Event()
        self.allow_sudo = allow_sudo
        self.session = session
        self.temp_root = Path(settings.temp_dir) if settings.temp_dir else Path(
            temp_default or Path(tempfile.gettempdir()) / "pm_installer")

    # ------------------------------------------------------------ вспомогательное

    def _progress(self, stage: str, frac: float, msg: str = "") -> None:
        if self._progress_cb:
            self._progress_cb(stage, frac, msg)

    def _check_cancel(self) -> None:
        if self.cancel.is_set():
            raise CancelledError("Установка отменена.")

    def firmware_key(self) -> str:
        want = self.settings.firmware
        if want and want != "auto":
            if want not in self.profiles:
                raise InstallerError(f"Неизвестная прошивка «{want}». Доступны: {', '.join(self.profiles)}.")
            return want
        key = detect(self.profiles, self.root, env_cfw_name(self.root))
        if key is None:
            raise InstallerError("Не удалось определить прошивку автоматически. Выберите её в настройках.")
        log.info("Прошивка определена автоматически: %s", key)
        return key

    # ------------------------------------------------------------ облако

    def list_remote(self, url: Optional[str] = None) -> Tuple[CloudProvider, List[RemoteFile]]:
        url = normalize_url(self.settings.url if url is None else url)
        provider = get_provider(url, self.settings.cloud, self.session)
        log.info("Облако: %s, ссылка: %s", provider.key, url)
        self._progress("list", -1, "Чтение ссылки…")
        files = [f for f in provider.list_files(url) if not f.name or is_archive_name(f.name)]
        if not files:
            raise InvalidLinkError("По ссылке не найдено архивов .zip / .7z / .rar.")
        return provider, files

    def install(self, url: Optional[str] = None, choose: Optional[Chooser] = None) -> List[InstallReport]:
        """Полный цикл по ссылке. Для папки с несколькими архивами нужен choose()."""
        provider, files = self.list_remote(url)
        if len(files) > 1 or (files and files[0].meta.get("folder") and choose is not None):
            if choose is None:
                raise InvalidLinkError("В папке несколько архивов — выберите нужный.")
            files = choose(files)
        return self.install_remote(provider, files)

    def install_remote(self, provider: CloudProvider, files: List[RemoteFile]) -> List[InstallReport]:
        """Скачивает и устанавливает выбранные файлы (список уже показан пользователю)."""
        reports = []
        dl_dir = self.temp_root / "download"
        for rf in files:
            self._check_cancel()
            label = rf.name or "архив"

            def on_dl(done: int, total: Optional[int], _l=label):
                frac = done / total if total else -1
                self._progress("download", frac, f"Загрузка {_l}: {done // 1024 // 1024} МБ")

            path = provider.download(rf, dl_dir, on_dl, self.cancel)
            try:
                reports.append(self.install_archive(path))
            finally:
                try:
                    path.unlink()
                except OSError:
                    pass
        return reports

    # ------------------------------------------------------------ установка архива

    def install_archive(self, archive: Path) -> InstallReport:
        archive = Path(archive)
        profile = self.profiles[self.firmware_key()]
        targets = resolve_targets(profile, self.root, self.allow_sudo)   # рано падаем при нехватке прав
        report = InstallReport(archive.name, profile.label, targets.port_dir, targets.script_dir)

        try:
            self.temp_root.mkdir(parents=True, exist_ok=True)
            work = Path(tempfile.mkdtemp(prefix="extract_", dir=self.temp_root))
        except OSError as e:
            raise wrap_oserror(e, self.temp_root) from e
        try:
            self._progress("extract", -1, f"Распаковка {archive.name}…")
            extract(archive, work, lambda f: self._progress("extract", f, f"Распаковка {archive.name}…"),
                    self.cancel)
            self._check_cancel()
            layout = analyze(work)
            report.warnings += layout.warnings
            report.ignored += [p.name for p in layout.ignored]
            log.info("Порт: sh=%s, папки=%s", [s.name for s in layout.scripts],
                     [d.name for d in layout.port_dirs])

            items = [(d, targets.port_dir, "папка") for d in layout.port_dirs] + \
                    [(s, targets.script_dir, "файл") for s in layout.scripts]
            for i, (src, dst_dir, kind) in enumerate(items):
                self._check_cancel()
                self._progress("install", i / len(items), f"Копирование: {src.name}")
                self._place(src, dst_dir, kind, report)
            self._progress("install", 1.0, "Готово")
        finally:
            shutil.rmtree(work, ignore_errors=True)
        log.info("Установка завершена: %s", report.summary())
        return report

    # ------------------------------------------------------------ раскладка

    def _decide(self, dst: Path, kind: str) -> ConflictAction:
        pol = self.settings.conflict_policy
        if pol == "overwrite":
            return ConflictAction.OVERWRITE
        if pol == "skip":
            return ConflictAction.SKIP
        if pol == "backup":
            return ConflictAction.BACKUP
        if self.resolver is None:
            raise ConflictAbort(f"«{dst.name}» уже существует. Выберите в настройках, что делать при конфликте.")
        return self.resolver(dst, kind)

    def _place(self, src: Path, dst_dir: Path, kind: str, report: InstallReport) -> None:
        dst = dst_dir / src.name
        is_script = src.suffix.lower() == ".sh" and kind == "файл"
        try:
            if os.path.lexists(dst):
                action = self._decide(dst, kind)
                log.info("Конфликт %s → %s", dst, action.value)
                if action is ConflictAction.ABORT:
                    raise ConflictAbort("Установка прервана пользователем.")
                if action is ConflictAction.SKIP:
                    report.skipped.append(f"{dst.name} (уже существует)")
                    return
                if action is ConflictAction.BACKUP:
                    bak = dst.with_name(f"{dst.name}.bak-{time.strftime('%Y%m%d-%H%M%S')}")
                    os.rename(dst, bak)
                    report.backups.append(bak.name)
                    shutil.move(str(src), str(dst))
                else:   # OVERWRITE: старое сначала откладываем, чтобы откатиться при сбое
                    old = dst.with_name(f".{dst.name}.old-{os.getpid()}")
                    os.rename(dst, old)
                    try:
                        shutil.move(str(src), str(dst))
                    except Exception:
                        if os.path.lexists(dst):
                            shutil.rmtree(dst, ignore_errors=True) if dst.is_dir() else dst.unlink()
                        os.rename(old, dst)
                        raise
                    shutil.rmtree(old, ignore_errors=True) if old.is_dir() else old.unlink()
            else:
                shutil.move(str(src), str(dst))
        except OSError as e:
            raise wrap_oserror(e, dst) from e
        report.installed.append(f"{dst.name} → {dst_dir}")
        if is_script:
            self._make_executable(dst, report)

    @staticmethod
    def _make_executable(path: Path, report: InstallReport) -> None:
        try:
            os.chmod(path, path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        except OSError as e:
            log.warning("chmod +x %s: %s", path, e)
        if not os.access(path, os.X_OK):
            report.warnings.append(
                f"Не удалось выставить право на исполнение для «{path.name}» "
                "(файловая система может не поддерживать права).")
