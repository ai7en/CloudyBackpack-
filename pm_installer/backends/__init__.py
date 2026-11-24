"""Графические бэкенды. Интерфейс (утиная типизация):

    name, swap_ab
    load_fonts(path, sizes) -> [font, ...]
    text_size(font, s) -> (w, h);  draw_text(font, s, x, y, color)
    clear(color); fill_rect(color, x, y, w, h); outline(color, x, y, w, h, width)
    present(); poll() -> [action]; ticks() -> ms; delay(ms); close()

Действия: up/down/left/right/ok/back/x/y/start/select/l/r, "text:<символ>", "quit".
"""
from pathlib import Path
from typing import Optional

from ..errors import BackendUnavailable

__all__ = ["create_backend", "BackendUnavailable"]


def create_backend(name: str, title: str, fullscreen: bool, joy_map_path: Optional[Path] = None):
    """name: auto | pygame | sdl2. auto = pygame, затем PySDL2 (он есть в PortMaster/pylibs)."""
    order = ["pygame", "sdl2"] if name in ("", "auto") else [name]
    problems = []
    for n in order:
        try:
            if n == "pygame":
                from .pygame_backend import PygameBackend
                return PygameBackend(title, fullscreen, joy_map_path)
            if n == "sdl2":
                from .sdl2_backend import SDL2Backend
                return SDL2Backend(title, fullscreen)
            raise BackendUnavailable(f"неизвестный бэкенд «{n}»")
        except ImportError as e:
            problems.append(f"{n}: модуль не найден ({e})")
        except BackendUnavailable as e:
            problems.append(f"{n}: {e}")
    raise BackendUnavailable("Не удалось запустить графику — " + "; ".join(problems))
