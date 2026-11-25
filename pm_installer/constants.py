"""Общие константы."""

APP_NAME = "Cloud Port Installer"
SCREEN_W, SCREEN_H = 640, 480
ARCHIVE_EXTS = (".zip", ".7z", ".rar")
JUNK_NAMES = frozenset({"__MACOSX", ".DS_Store", "Thumbs.db", "desktop.ini"})


def is_archive_name(name: str) -> bool:
    return name.lower().endswith(ARCHIVE_EXTS)

APP_FOLDER = "cloudportinstaller"          # папка этого приложения (менеджер её не удаляет)
APP_SCRIPT = "Cloud Port Installer.sh"
