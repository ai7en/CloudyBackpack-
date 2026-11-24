"""Логирование в файл (с ротацией)."""
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path


def setup_logging(log_dir: Path, console: bool = False, level: int = logging.INFO) -> logging.Logger:
    logger = logging.getLogger("pm_installer")
    if logger.handlers:
        return logger
    logger.setLevel(level)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    try:
        Path(log_dir).mkdir(parents=True, exist_ok=True)
        fh = RotatingFileHandler(Path(log_dir) / "installer.log", maxBytes=512_000,
                                 backupCount=3, encoding="utf-8")
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    except OSError:
        logger.addHandler(logging.NullHandler())  # журнал недоступен — работаем без него
    if console:
        sh = logging.StreamHandler()
        sh.setLevel(logging.WARNING)   # подробности — в файле, в консоли только проблемы
        sh.setFormatter(fmt)
        logger.addHandler(sh)
    return logger
