"""Исключения. str(e) всегда пригоден для показа пользователю."""
import errno
from typing import Optional


class InstallerError(Exception):
    """Базовая ошибка установщика."""


class NetworkError(InstallerError):
    """Нет сети, обрыв загрузки, таймаут."""


class InvalidLinkError(InstallerError):
    """Ссылка неверна, закрыта или не содержит архивов."""


class UnsupportedArchiveError(InstallerError):
    """Неподдерживаемый, повреждённый или защищённый паролем архив."""


class ArchiveLayoutError(InstallerError):
    """Структура архива не похожа на порт PortMaster."""


class PermissionDeniedError(InstallerError):
    """Недостаточно прав для записи."""


class MissingDependencyError(InstallerError):
    """Не хватает библиотеки или утилиты."""


class ConflictAbort(InstallerError):
    """Установка прервана из-за конфликта имён."""


class CancelledError(InstallerError):
    """Операция отменена пользователем."""


class BackendUnavailable(InstallerError):
    """Не удалось запустить графический бэкенд (pygame / PySDL2)."""


def wrap_oserror(e: OSError, path: Optional[object] = None) -> InstallerError:
    where = f" ({path})" if path else ""
    if e.errno == errno.ENOSPC:
        return InstallerError(
            f"Недостаточно места на диске{where}. Освободите место или выберите "
            "другую временную папку в настройках.")
    if e.errno == errno.EROFS:
        return PermissionDeniedError(f"Файловая система только для чтения{where}.")
    if e.errno in (errno.EACCES, errno.EPERM):
        return PermissionDeniedError(
            f"Недостаточно прав для записи{where}. Запустите установщик с нужными "
            "правами или проверьте, что карта памяти не защищена от записи.")
    return InstallerError(f"Ошибка файловой системы{where}: {e.strerror or e}")
