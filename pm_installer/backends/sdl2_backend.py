"""Бэкенд PySDL2 + SDL2_ttf — то, на чём работает сам PortMaster (PortMaster/pylibs).

Ввод идёт через SDL GameController: кнопки названы логически (A/B/X/Y, крестик),
раскладку конкретной консоли задаёт $sdl_controllerconfig из control.txt PortMaster.
Для устройств без маппинга есть запасной путь через «сырые» кнопки джойстика.
"""
import ctypes
from typing import List

import sdl2
import sdl2.sdlttf as ttf

from ..constants import SCREEN_H, SCREEN_W
from ..errors import BackendUnavailable

_KEYS = {sdl2.SDLK_UP: "up", sdl2.SDLK_DOWN: "down", sdl2.SDLK_LEFT: "left", sdl2.SDLK_RIGHT: "right",
         sdl2.SDLK_RETURN: "ok", sdl2.SDLK_KP_ENTER: "ok", sdl2.SDLK_ESCAPE: "back",
         sdl2.SDLK_BACKSPACE: "x", sdl2.SDLK_SPACE: "y", sdl2.SDLK_TAB: "start"}
_BUTTONS = {sdl2.SDL_CONTROLLER_BUTTON_A: "ok", sdl2.SDL_CONTROLLER_BUTTON_B: "back",
            sdl2.SDL_CONTROLLER_BUTTON_X: "x", sdl2.SDL_CONTROLLER_BUTTON_Y: "y",
            sdl2.SDL_CONTROLLER_BUTTON_START: "start", sdl2.SDL_CONTROLLER_BUTTON_BACK: "select",
            sdl2.SDL_CONTROLLER_BUTTON_LEFTSHOULDER: "l", sdl2.SDL_CONTROLLER_BUTTON_RIGHTSHOULDER: "r",
            sdl2.SDL_CONTROLLER_BUTTON_DPAD_UP: "up", sdl2.SDL_CONTROLLER_BUTTON_DPAD_DOWN: "down",
            sdl2.SDL_CONTROLLER_BUTTON_DPAD_LEFT: "left", sdl2.SDL_CONTROLLER_BUTTON_DPAD_RIGHT: "right"}
_RAW_BUTTONS = {0: "ok", 1: "back", 2: "x", 3: "y", 4: "l", 5: "r", 6: "select", 7: "start"}
_SWAP = {"ok": "back", "back": "ok"}


def _err() -> str:
    msg = sdl2.SDL_GetError()
    return msg.decode("utf-8", "replace") if msg else "неизвестная ошибка SDL"


class SDL2Backend:
    name = "sdl2"

    def __init__(self, title: str, fullscreen: bool):
        self.swap_ab = False
        if sdl2.SDL_Init(sdl2.SDL_INIT_VIDEO | sdl2.SDL_INIT_GAMECONTROLLER | sdl2.SDL_INIT_JOYSTICK) != 0:
            raise BackendUnavailable("SDL_Init: " + _err())
        if ttf.TTF_Init() != 0:
            raise BackendUnavailable("SDL2_ttf: " + _err())
        flags = sdl2.SDL_WINDOW_SHOWN | (sdl2.SDL_WINDOW_FULLSCREEN_DESKTOP if fullscreen else 0)
        self.win = sdl2.SDL_CreateWindow(title.encode("utf-8"), sdl2.SDL_WINDOWPOS_CENTERED,
                                         sdl2.SDL_WINDOWPOS_CENTERED, SCREEN_W, SCREEN_H, flags)
        if not self.win:
            raise BackendUnavailable("SDL_CreateWindow: " + _err())
        self.ren = (sdl2.SDL_CreateRenderer(self.win, -1, sdl2.SDL_RENDERER_ACCELERATED)
                    or sdl2.SDL_CreateRenderer(self.win, -1, sdl2.SDL_RENDERER_SOFTWARE))
        if not self.ren:
            raise BackendUnavailable("SDL_CreateRenderer: " + _err())
        sdl2.SDL_RenderSetLogicalSize(self.ren, SCREEN_W, SCREEN_H)   # масштаб под экран любой консоли
        sdl2.SDL_ShowCursor(sdl2.SDL_DISABLE)
        sdl2.SDL_StartTextInput()
        self._fonts = []
        self._tex = {}
        self._pads = {}        # instance_id -> указатель GameController
        self._raw = {}         # instance_id -> указатель Joystick (без маппинга)
        self._axis = {}
        for i in range(sdl2.SDL_NumJoysticks()):
            self._open_device(i)

    # ---- устройства
    def _open_device(self, index: int) -> None:
        if sdl2.SDL_IsGameController(index):
            pad = sdl2.SDL_GameControllerOpen(index)
            if pad:
                iid = sdl2.SDL_JoystickInstanceID(sdl2.SDL_GameControllerGetJoystick(pad))
                self._pads.setdefault(iid, pad)
        else:
            joy = sdl2.SDL_JoystickOpen(index)
            if joy:
                self._raw.setdefault(sdl2.SDL_JoystickInstanceID(joy), joy)

    # ---- шрифты и текст
    def load_fonts(self, path, sizes):
        if not path:
            raise BackendUnavailable("не найден шрифт .ttf (положите DejaVuSans.ttf в assets/)")
        fonts = []
        for s in sizes:
            f = ttf.TTF_OpenFont(str(path).encode("utf-8"), s)
            if not f:
                raise BackendUnavailable(f"не удалось открыть шрифт {path}: {_err()}")
            fonts.append(f)
        self._fonts += fonts
        return fonts

    def text_size(self, font, s):
        w, h = ctypes.c_int(0), ctypes.c_int(0)
        ttf.TTF_SizeUTF8(font, s.encode("utf-8"), ctypes.byref(w), ctypes.byref(h))
        return w.value, h.value

    def draw_text(self, font, s, x, y, color):
        if not s:
            return
        key = (id(font), s, color)
        entry = self._tex.get(key)
        if entry is None:
            surf = ttf.TTF_RenderUTF8_Blended(font, s.encode("utf-8"), sdl2.SDL_Color(color[0], color[1], color[2], 255))
            if not surf:
                return
            w, h = surf.contents.w, surf.contents.h
            tex = sdl2.SDL_CreateTextureFromSurface(self.ren, surf)
            sdl2.SDL_FreeSurface(surf)
            if not tex:
                return
            if len(self._tex) > 300:
                self._drop_textures()
            entry = self._tex[key] = (tex, w, h)
        sdl2.SDL_RenderCopy(self.ren, entry[0], None, sdl2.SDL_Rect(x, y, entry[1], entry[2]))

    def _drop_textures(self):
        for tex, _, _ in self._tex.values():
            sdl2.SDL_DestroyTexture(tex)
        self._tex.clear()

    # ---- примитивы
    def _color(self, c):
        sdl2.SDL_SetRenderDrawColor(self.ren, c[0], c[1], c[2], 255)

    def clear(self, color):
        self._color(color)
        sdl2.SDL_RenderClear(self.ren)

    def fill_rect(self, color, x, y, w, h):
        self._color(color)
        sdl2.SDL_RenderFillRect(self.ren, sdl2.SDL_Rect(x, y, w, h))

    def outline(self, color, x, y, w, h, width=1):
        self._color(color)
        for i in range(width):
            sdl2.SDL_RenderDrawRect(self.ren, sdl2.SDL_Rect(x + i, y + i, w - 2 * i, h - 2 * i))

    def present(self):
        sdl2.SDL_RenderPresent(self.ren)

    def ticks(self):
        return sdl2.SDL_GetTicks()

    def delay(self, ms):
        sdl2.SDL_Delay(ms)

    def close(self):
        self._drop_textures()
        for f in self._fonts:
            ttf.TTF_CloseFont(f)
        for pad in self._pads.values():
            sdl2.SDL_GameControllerClose(pad)
        for joy in self._raw.values():
            sdl2.SDL_JoystickClose(joy)
        sdl2.SDL_DestroyRenderer(self.ren)
        sdl2.SDL_DestroyWindow(self.win)
        ttf.TTF_Quit()
        sdl2.SDL_Quit()

    # ---- ввод
    def _pad(self, action: str) -> str:
        return _SWAP.get(action, action) if self.swap_ab else action

    def poll(self) -> List[str]:
        out: List[str] = []
        ev = sdl2.SDL_Event()
        while sdl2.SDL_PollEvent(ctypes.byref(ev)):
            t = ev.type
            if t == sdl2.SDL_QUIT:
                out.append("quit")
            elif t == sdl2.SDL_KEYDOWN:
                act = _KEYS.get(ev.key.keysym.sym)
                if act:
                    out.append(act)
            elif t == sdl2.SDL_TEXTINPUT:
                s = ev.text.text.decode("utf-8", "ignore")
                if s and s.isprintable() and s.strip():
                    out.append("text:" + s)
            elif t == sdl2.SDL_CONTROLLERDEVICEADDED or t == sdl2.SDL_JOYDEVICEADDED:
                self._open_device(ev.cdevice.which if t == sdl2.SDL_CONTROLLERDEVICEADDED else ev.jdevice.which)
            elif t == sdl2.SDL_CONTROLLERBUTTONDOWN:
                act = _BUTTONS.get(ev.cbutton.button)
                if act:
                    out.append(self._pad(act))
            elif t == sdl2.SDL_CONTROLLERAXISMOTION:
                out += self._stick(ev.caxis.axis == sdl2.SDL_CONTROLLER_AXIS_LEFTX,
                                   ev.caxis.axis in (sdl2.SDL_CONTROLLER_AXIS_LEFTX, sdl2.SDL_CONTROLLER_AXIS_LEFTY),
                                   ev.caxis.axis, ev.caxis.value / 32767.0)
            elif t == sdl2.SDL_JOYBUTTONDOWN and ev.jbutton.which in self._raw:
                act = _RAW_BUTTONS.get(ev.jbutton.button)
                if act:
                    out.append(self._pad(act))
            elif t == sdl2.SDL_JOYHATMOTION and ev.jhat.which in self._raw:
                v = ev.jhat.value
                out += [a for a, bit in (("up", sdl2.SDL_HAT_UP), ("right", sdl2.SDL_HAT_RIGHT),
                                         ("down", sdl2.SDL_HAT_DOWN), ("left", sdl2.SDL_HAT_LEFT)) if v & bit]
        return out

    def _stick(self, horizontal: bool, is_left_stick: bool, axis: int, value: float) -> List[str]:
        if not is_left_stick:
            return []
        names = ("left", "right") if horizontal else ("up", "down")
        state = -1 if value < -0.6 else 1 if value > 0.6 else 0
        prev, self._axis[axis] = self._axis.get(axis, 0), state
        return [names[0] if state < 0 else names[1]] if state and state != prev else []
