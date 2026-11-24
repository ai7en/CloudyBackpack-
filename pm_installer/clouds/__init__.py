"""Реестр облачных провайдеров."""
from typing import Dict, List, Optional, Tuple, Type

from ..errors import InstallerError, InvalidLinkError
from .base import CloudProvider, RemoteFile

_REGISTRY: Dict[str, Type[CloudProvider]] = {}


def register(cls: Type[CloudProvider]) -> Type[CloudProvider]:
    """Декоратор: добавляет провайдер в реестр. Порядок регистрации = порядок автоопределения."""
    _REGISTRY[cls.key] = cls
    return cls


def available_providers() -> List[Tuple[str, str]]:
    return [(k, c.label) for k, c in _REGISTRY.items()]


def get_provider(url: str, kind: str = "auto", session=None) -> CloudProvider:
    url = (url or "").strip()
    if not url:
        raise InvalidLinkError("Ссылка не задана. Укажите её в настройках.")
    if kind != "auto":
        cls: Optional[Type[CloudProvider]] = _REGISTRY.get(kind)
        if cls is None:
            raise InstallerError(f"Неизвестный тип облака: {kind}")
        return cls(session)
    for cls in _REGISTRY.values():
        if cls.matches(url):
            return cls(session)
    raise InvalidLinkError("Не удалось определить облако по ссылке. Выберите тип облака в настройках.")


# Импорт после определения register(); direct — последним (запасной вариант для http(s)-ссылок)
from . import yandex, gdrive, mailru, direct  # noqa: E402,F401

__all__ = ["CloudProvider", "RemoteFile", "register", "available_providers", "get_provider"]
