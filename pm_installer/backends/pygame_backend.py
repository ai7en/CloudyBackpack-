"""Бэкенд pygame (удобен для отладки на ПК)."""
import json
from pathlib import Path
from typing import List, Optional

import pygame

from ..constants import SCREEN_H, SCREEN_W
from ..errors import BackendUnavailable
from .combo import ExitCombo

KEY_MAP = {pygame.K_UP: "up", pygame.K_DOWN: "down", pygame.K_LEFT: "left", pygame.K_RIGHT: "right",
           pygame.K_RETURN: "ok", pygame.K_KP_ENTER: "ok", pygame.K_ESCAPE: "back",
           pygame.K_BACKSPACE: "x", pygame.K_SPACE: "y", pygame.K_TAB: "start"}
DEFAULT_JOY_MAP = {0: "ok", 1: "back", 2: "x", 3: "y", 4: "l", 5: "r", 6: "select", 7: "start"}
_SWAP = {"ok": "back", "back": "ok"}


class PygameBackend:
    name = "pygame"

    def __init__(self, title: str, fullscreen: bool, joy_map_path: Optional[Path] = None):
        self.swap_ab = False
        self._combo = ExitCombo()
        try:
            pygame.init()
            flags = pygame.SCALED | (pygame.FULLSCREEN if fullscreen else 0)
            try:
                self.surf = pygame.display.set_mode((SCREEN_W, SCREEN_H), flags)
            except pygame.error:
                self.surf = pygame.display.set_mode((SCREEN_W, SCREEN_H))
        except pygame.error as e:
            raise BackendUnavailable(str(e)) from e
        pygame.display.set_caption(title)
        pygame.mouse.set_visible(False)
        pygame.key.set_repeat(350, 70)
        self.joy_map = dict(DEFAULT_JOY_MAP)
        if joy_map_path:
            try:
                self.joy_map.update({int(k): v for k, v in json.loads(Path(joy_map_path).read_text("utf-8")).items()})
            except (OSError, ValueError):
                pass
        pygame.joystick.init()
        self.joys = []
        for i in range(pygame.joystick.get_count()):
            j = pygame.joystick.Joystick(i)
            j.init()
            self.joys.append(j)
        self._axis = {}
        self._cache = {}
        self._clock = pygame.time.Clock()

    # ---- шрифты и текст
    def load_fonts(self, path, sizes):
        return [pygame.font.Font(str(path) if path else None, s) for s in sizes]

    def text_size(self, font, s):
        return font.size(s)

    def draw_text(self, font, s, x, y, color):
        if not s:
            return
        key = (id(font), s, color)
        img = self._cache.get(key)
        if img is None:
            if len(self._cache) > 400:
                self._cache.clear()
            img = self._cache[key] = font.render(s, True, color)
        self.surf.blit(img, (x, y))

    # ---- примитивы
    def clear(self, color):
        self.surf.fill(color)

    def fill_rect(self, color, x, y, w, h):
        pygame.draw.rect(self.surf, color, (x, y, w, h))

    def outline(self, color, x, y, w, h, width=1):
        pygame.draw.rect(self.surf, color, (x, y, w, h), width)

    def present(self):
        pygame.display.flip()

    def ticks(self):
        return pygame.time.get_ticks()

    def delay(self, ms):
        self._clock.tick(1000 // max(ms, 1))

    def close(self):
        pygame.quit()

    # ---- ввод
    def _pad(self, action: str) -> str:
        return _SWAP.get(action, action) if self.swap_ab else action

    def poll(self) -> List[str]:
        out: List[str] = []
        for ev in pygame.event.get():
            if ev.type == pygame.QUIT:
                out.append("quit")
            elif ev.type == pygame.KEYDOWN and ev.key in KEY_MAP:
                out.append(KEY_MAP[ev.key])
            elif ev.type == pygame.TEXTINPUT and ev.text.isprintable() and ev.text.strip():
                out.append("text:" + ev.text)
            elif ev.type == pygame.JOYBUTTONDOWN and ev.button in self.joy_map:
                act = self.joy_map[ev.button]
                out += self._combo.press(act, self._pad(act))
            elif ev.type == pygame.JOYBUTTONUP and ev.button in self.joy_map:
                self._combo.release(self.joy_map[ev.button])
            elif ev.type == pygame.JOYHATMOTION:
                x, y = ev.value
                out += [a for a, on in (("left", x < 0), ("right", x > 0), ("up", y > 0), ("down", y < 0)) if on]
            elif ev.type == pygame.JOYAXISMOTION and ev.axis in (0, 1):
                names = ("left", "right") if ev.axis == 0 else ("up", "down")
                state = -1 if ev.value < -0.6 else 1 if ev.value > 0.6 else 0
                prev, self._axis[ev.axis] = self._axis.get(ev.axis, 0), state
                if state and state != prev:
                    out.append(names[0] if state < 0 else names[1])
        return out
