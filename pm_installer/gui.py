"""Экраны и логика GUI (640x480). Рисование и ввод — через бэкенд (pygame или PySDL2).

Действия: up/down/left/right, ok (A), back (B), x (удалить), y (Shift), start (Готово).
Если A и B на вашей консоли перепутаны — «Настройки → Кнопки A/B».
"""
import logging
import os
import threading
from pathlib import Path
from typing import Callable, List, Optional

from .clouds import available_providers
from .constants import APP_NAME, SCREEN_H as H, SCREEN_W as W
from .diagnostics import DEFAULT_TARGETS, Check, origin_of, run_checks
from .errors import CancelledError, InstallerError
from .firmware import describe_targets, detect, env_cfw_name
from .installer import ConflictAction, PortInstaller
from .settings import CONFLICT_LABELS, CONFLICT_POLICIES

log = logging.getLogger("pm_installer.gui")

BG, PANEL, FG = (18, 20, 28), (32, 36, 50), (232, 234, 240)
DIM, ACC, OKC, ERR, WARN = (135, 140, 158), (88, 160, 255), (110, 210, 130), (240, 92, 92), (240, 190, 80)

_FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/TTF/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
)


def find_font() -> Optional[str]:
    """Шрифт с кириллицей: $PM_FONT → assets/DejaVuSans.ttf в проекте → системные."""
    own = Path(__file__).resolve().parent.parent / "assets" / "DejaVuSans.ttf"
    for p in (os.environ.get("PM_FONT"), str(own), *_FONT_CANDIDATES):
        if p and Path(p).is_file():
            return p
    return None


# ====================================================================== экраны

class Screen:
    def on_show(self, gui: "Gui") -> None: ...
    def handle(self, act: str, gui: "Gui") -> None: ...
    def tick(self, gui: "Gui") -> None: ...
    def draw(self, gui: "Gui") -> None: ...


def _move(sel: int, act: str, n: int) -> int:
    if act == "up":
        return (sel - 1) % n
    if act == "down":
        return (sel + 1) % n
    return sel


class MessageScreen(Screen):
    def __init__(self, title: str, lines: List[str], color=FG):
        self.title, self.lines, self.color = title, lines, color

    def handle(self, act, gui):
        if act in ("ok", "back"):
            gui.pop()

    def draw(self, gui):
        gui.header(self.title)
        y = 70
        for ln in self.lines:
            for part in gui.wrap(ln, gui.f_m, W - 40):
                gui.text(part, 20, y, color=self.color)
                y += 26
        gui.footer("A/B — назад")


class MainMenu(Screen):
    ITEMS = ["Установить порт из облака", "Настройки", "Проверка сети", "Пути установки", "Выход"]

    def __init__(self):
        self.sel = 0
        self.info = ""

    def on_show(self, gui):
        key = gui.settings.firmware
        auto = key == "auto"
        if auto:
            key = detect(gui.profiles, gui.root, gui.cfw)
        label = gui.profiles[key].label if key in gui.profiles else "не определена"
        self.info = f"Прошивка: {label}{' (авто)' if auto else ''}"

    def handle(self, act, gui):
        self.sel = _move(self.sel, act, len(self.ITEMS))
        if act == "back":
            gui.running = False
        elif act == "ok":
            if self.sel == 0:
                if not gui.settings.url:
                    gui.push(MessageScreen("Нет ссылки", ["Сначала укажите ссылку на облако",
                                                         "в разделе «Настройки»."], WARN))
                else:
                    gui.push(InstallScreen(gui))
            elif self.sel == 1:
                gui.push(SettingsScreen())
            elif self.sel == 2:
                gui.push(NetCheckScreen(gui))
            elif self.sel == 3:
                gui.push(PathsScreen())
            else:
                gui.running = False

    def draw(self, gui):
        gui.header(APP_NAME)
        gui.text(self.info, 20, 62, gui.f_s, DIM)
        gui.rows([(t, "") for t in self.ITEMS], self.sel, y0=95)
        gui.footer("↑↓ выбор   A — открыть   B — выход")


class SettingsScreen(Screen):
    ROWS = [("url", "Ссылка на облако"), ("cloud", "Тип облака"), ("firmware", "Прошивка"),
            ("temp_dir", "Временная папка"), ("conflict_policy", "При конфликте имён"),
            ("swap_ab", "Кнопки A/B")]
    TEXT_ROWS = ("url", "temp_dir")

    def __init__(self):
        self.sel = 0

    def _options(self, key, gui):
        if key == "cloud":
            return ["auto"] + [k for k, _ in available_providers()]
        if key == "firmware":
            return ["auto"] + list(gui.profiles)
        if key == "swap_ab":
            return [False, True]
        return list(CONFLICT_POLICIES)

    def _pretty(self, key, gui):
        v = getattr(gui.settings, key)
        if key == "url":
            return v or "(не задана)"
        if key == "temp_dir":
            return v or f"(по умолчанию) {gui.store.default_temp}"
        if key == "swap_ab":
            return "Поменять A и B" if v else "Как есть"
        if v == "auto":
            return "Авто"
        if key == "cloud":
            return dict(available_providers()).get(v, v)
        if key == "firmware":
            return gui.profiles[v].label if v in gui.profiles else v
        return CONFLICT_LABELS.get(v, v)

    def _set(self, gui, key, value):
        setattr(gui.settings, key, value)
        try:
            gui.store.save(gui.settings)
        except InstallerError as e:
            gui.push(MessageScreen("Не удалось сохранить", [str(e)], ERR))

    def handle(self, act, gui):
        self.sel = _move(self.sel, act, len(self.ROWS))
        key, title = self.ROWS[self.sel]
        if act == "back":
            gui.pop()
        elif key in self.TEXT_ROWS and act == "ok":
            gui.push(TextInput(title, getattr(gui.settings, key),
                               lambda v, k=key: self._set(gui, k, v.strip())))
        elif key not in self.TEXT_ROWS and act in ("left", "right", "ok"):
            opts = self._options(key, gui)
            cur = getattr(gui.settings, key)
            i = opts.index(cur) if cur in opts else 0
            i = (i + (-1 if act == "left" else 1)) % len(opts)
            self._set(gui, key, opts[i])

    def draw(self, gui):
        gui.header("Настройки")
        gui.rows([(t, self._pretty(k, gui)) for k, t in self.ROWS], self.sel, y0=60, rh=56, two_line=True)
        gui.footer("←→ изменить   A — ввод текста   B — назад")


class TextInput(Screen):
    CHARS = ["1234567890-_", "qwertyuiop:/", "asdfghjkl.?=", "zxcvbnm&%#@+"]
    SPECIAL = ["Shift", "Пробел", "Del", "Готово", "Отмена"]

    def __init__(self, title: str, initial: str, on_done: Callable[[str], None]):
        self.title, self.value, self.on_done = title, initial or "", on_done
        self.r, self.c, self.upper = 0, 0, False

    def _cols(self, r):
        return len(self.SPECIAL) if r == len(self.CHARS) else len(self.CHARS[r])

    def _finish(self, gui, save):
        gui.pop()
        if save:
            self.on_done(self.value)

    def handle(self, act, gui):
        if act.startswith("text:"):
            self.value += act[5:]
        elif act in ("up", "down"):
            step = -1 if act == "up" else 1
            self.r = (self.r + step) % (len(self.CHARS) + 1)
            self.c = min(self.c, self._cols(self.r) - 1)
        elif act == "left":
            self.c = (self.c - 1) % self._cols(self.r)
        elif act == "right":
            self.c = (self.c + 1) % self._cols(self.r)
        elif act == "x":
            self.value = self.value[:-1]
        elif act == "y":
            self.upper = not self.upper
        elif act == "start":
            self._finish(gui, True)
        elif act == "back":
            self._finish(gui, False)
        elif act == "ok":
            if self.r < len(self.CHARS):
                ch = self.CHARS[self.r][self.c]
                self.value += ch.upper() if self.upper else ch
            else:
                name = self.SPECIAL[self.c]
                if name == "Shift":
                    self.upper = not self.upper
                elif name == "Пробел":
                    self.value += " "
                elif name == "Del":
                    self.value = self.value[:-1]
                else:
                    self._finish(gui, name == "Готово")

    def draw(self, gui):
        gui.header(self.title)
        gui.rect(PANEL, 16, 62, W - 32, 44)
        gui.text(self.value[-60:] + "|", 24, 72, maxw=W - 48)
        top, cw, ch = 130, 48, 46
        for r, row in enumerate(self.CHARS):
            for c, key in enumerate(row):
                gui.key_cap(40 + c * (cw + 2), top + r * (ch + 4), cw, ch,
                            key.upper() if self.upper else key, (r, c) == (self.r, self.c))
        sw = 108
        for c, name in enumerate(self.SPECIAL):
            gui.key_cap(20 + c * (sw + 4), top + len(self.CHARS) * (ch + 4), sw, ch, name,
                        (self.r, self.c) == (len(self.CHARS), c))
        gui.footer("A ввод  X удалить  Y регистр  Start готово  B отмена")


class NetCheckScreen(Screen):
    """Проверка сети в фоновом потоке: время, сертификаты, DNS, TCP, HTTPS."""
    COLORS = {"ok": OKC, "warn": WARN, "fail": ERR}
    MARKS = {"ok": "OK", "warn": "!", "fail": "ОШИБКА"}

    def __init__(self, gui: "Gui"):
        self.checks: List[Check] = []
        self.done = False
        self.off = 0
        targets = list(DEFAULT_TARGETS)
        extra = origin_of(gui.settings.url)
        if extra and extra not in targets:
            targets.append(extra)
        threading.Thread(target=self._run, args=(targets,), daemon=True).start()

    def _run(self, targets):
        try:
            run_checks(targets, on_progress=self._progress)
        except Exception as e:  # noqa: BLE001
            log.exception("Сбой диагностики сети")
            self._progress(self.checks + [Check("Диагностика", "fail", str(e))])
        self.done = True

    def _progress(self, checks):
        self.checks = checks

    def handle(self, act, gui):
        n = len(self.checks)
        if act == "down":
            self.off = min(self.off + 1, max(0, n - 7))
        elif act == "up":
            self.off = max(self.off - 1, 0)
        elif act in ("ok", "back"):
            gui.pop()

    def draw(self, gui):
        gui.header("Проверка сети" + ("" if self.done else "…"))
        y = 62
        for c in self.checks[self.off:self.off + 7]:
            gui.text(f"[{self.MARKS[c.status]}] {c.name}", 20, y, gui.f_s, self.COLORS[c.status], maxw=W - 40)
            gui.text(c.detail, 40, y + 20, gui.f_s, DIM, maxw=W - 60)
            y += 48
        if not self.checks:
            gui.text("Проверяю…", 20, y)
        gui.footer("↑↓ прокрутка   A/B — назад")


class PathsScreen(Screen):
    def handle(self, act, gui):
        if act in ("ok", "back"):
            gui.pop()

    def draw(self, gui):
        gui.header("Пути установки")
        key = gui.settings.firmware
        if key == "auto":
            key = detect(gui.profiles, gui.root, gui.cfw)
        if key not in gui.profiles:
            gui.text("Прошивка не определена — выберите её в настройках.", 20, 70, color=WARN)
        else:
            y = 66
            gui.text(f"Профиль: {gui.profiles[key].label}", 20, y, color=ACC)
            last = None
            for kind, path, exists, writable in describe_targets(gui.profiles[key], gui.root):
                y += 30
                if kind != last:
                    gui.text(kind + ":", 20, y, gui.f_s, DIM)
                    y += 24
                    last = kind
                state, col = (("есть, запись ок", OKC) if writable else
                              ("есть, нет прав", ERR)) if exists else ("будет создана", WARN)
                gui.text(str(path), 30, y, gui.f_s, maxw=380)
                gui.text(state, 430, y, gui.f_s, col)
        gui.footer("A/B — назад")


class InstallScreen(Screen):
    """Установка в фоновом потоке; диалоги конфликта/выбора блокируют поток до ответа."""
    CONFLICT_OPTS = [("Перезаписать", ConflictAction.OVERWRITE), ("Пропустить", ConflictAction.SKIP),
                     ("Резервная копия", ConflictAction.BACKUP), ("Прервать", ConflictAction.ABORT)]

    def __init__(self, gui: "Gui"):
        self.state = "running"
        self.stage, self.frac, self.msg = "", -1.0, "Запуск…"
        self.lines: List[str] = []
        self.error = ""
        self.dialog: Optional[dict] = None
        self.cancel = threading.Event()
        self.installer = PortInstaller(
            gui.settings, gui.profiles, root=gui.root, resolver=self._resolve,
            progress=self._progress, cancel=self.cancel, temp_default=gui.store.default_temp)
        threading.Thread(target=self._work, daemon=True).start()

    # ---- поток установки
    def _progress(self, stage, frac, msg):
        self.stage, self.frac, self.msg = stage, frac, msg

    def _ask(self, kind, title, options):
        d = {"kind": kind, "title": title, "options": options, "sel": 0,
             "event": threading.Event(), "answer": None}
        self.dialog = d
        while not d["event"].wait(0.2):
            if self.cancel.is_set():
                raise CancelledError("Установка отменена.")
        self.dialog = None
        return d["answer"]

    def _resolve(self, path, kind):
        return self._ask("conflict", f"«{path.name}» уже существует ({kind})",
                         [n for n, _ in self.CONFLICT_OPTS])

    def _choose(self, files):
        def label(f):
            size = f" ({f.size / 1048576:.0f} МБ)" if f.size else ""
            return (f.meta.get("display") or f.name or "(без имени)") + size
        names = [label(f) for f in files]
        if len(files) > 1:
            names.append("Установить все")
        idx = self._ask("choice", "Выберите архив", names)
        return files if idx == len(files) else [files[idx]]

    def _work(self):
        try:
            for rep in self.installer.install(choose=self._choose):
                self.lines += rep.summary() + [""]
            self.state = "done"
        except CancelledError:
            self.state = "cancelled"
        except InstallerError as e:
            log.error("Ошибка установки: %s", e)
            self.error, self.state = str(e), "error"
        except Exception as e:  # noqa: BLE001 — непредвиденное не должно убить GUI
            log.exception("Непредвиденная ошибка")
            self.error, self.state = f"Непредвиденная ошибка: {e}. Подробности — в журнале.", "error"

    # ---- интерфейс
    def handle(self, act, gui):
        d = self.dialog
        if d:
            d["sel"] = _move(d["sel"], act, len(d["options"]))
            if act == "ok":
                d["answer"] = d["sel"] if d["kind"] == "choice" else self.CONFLICT_OPTS[d["sel"]][1]
                d["event"].set()
            elif act == "back" and d["kind"] == "conflict":
                d["answer"] = ConflictAction.ABORT
                d["event"].set()
            return
        if self.state == "running":
            if act == "back":
                self.cancel.set()
                self.msg = "Отмена…"
        elif act in ("ok", "back"):
            gui.pop()

    def draw(self, gui):
        titles = {"running": "Установка", "done": "Готово", "error": "Ошибка", "cancelled": "Отменено"}
        gui.header(titles[self.state])
        if self.state == "running":
            gui.text(self.msg, 20, 80, maxw=W - 40)
            gui.bar(20, 120, W - 40, 22, self.frac)
            gui.footer("B — отмена")
        elif self.state == "done":
            y = 70
            for ln in self.lines[:14]:
                gui.text(ln, 20, y, gui.f_s, WARN if ln.startswith("Внимание") else FG, maxw=W - 40)
                y += 22
            gui.footer("A/B — назад")
        elif self.state == "error":
            y = 80
            for part in gui.wrap(self.error, gui.f_m, W - 40):
                gui.text(part, 20, y, color=ERR)
                y += 26
            gui.footer("A/B — назад")
        else:
            gui.text("Установка отменена пользователем.", 20, 80, color=WARN)
            gui.footer("A/B — назад")
        if self.dialog:
            self._draw_dialog(gui, self.dialog)

    def _draw_dialog(self, gui, d):
        gui.rect(PANEL, 30, 90, W - 60, 300)
        gui.outline(ACC, 30, 90, W - 60, 300, 2)
        gui.text(d["title"], 46, 104, maxw=W - 92)
        gui.rows([(o, "") for o in d["options"]], d["sel"], y0=146, rh=38, x=46, w=W - 92, max_rows=6)


# ====================================================================== приложение

class Gui:
    def __init__(self, settings, store, profiles, backend, root: str = "", font_path=None):
        self.settings, self.store, self.profiles, self.root = settings, store, profiles, root
        self.be = backend
        self.cfw = env_cfw_name(root)
        self.f_s, self.f_m, self.f_l = backend.load_fonts(font_path or find_font(), (16, 20, 28))
        self.stack: List[Screen] = [MainMenu()]
        self.stack[0].on_show(self)
        self.running = True

    # ---- стек экранов
    @property
    def top(self) -> Screen:
        return self.stack[-1]

    def push(self, s: Screen):
        self.stack.append(s)
        s.on_show(self)

    def pop(self):
        if len(self.stack) > 1:
            self.stack.pop()
            self.top.on_show(self)

    def step(self):
        """Один кадр: ввод → логика → отрисовка."""
        self.be.swap_ab = bool(self.settings.swap_ab)
        for act in self.be.poll():
            if act == "quit":
                self.running = False
            else:
                self.top.handle(act, self)
        self.top.tick(self)
        self.be.clear(BG)
        self.top.draw(self)
        self.be.present()

    def run(self):
        try:
            while self.running:
                self.step()
                self.be.delay(33)
        finally:
            self.be.close()

    # ---- рисование
    def rect(self, color, x, y, w, h):
        self.be.fill_rect(color, x, y, w, h)

    def outline(self, color, x, y, w, h, width=1):
        self.be.outline(color, x, y, w, h, width)

    def text(self, s, x, y, font=None, color=FG, maxw=None):
        font = font or self.f_m
        if maxw:
            s = self.fit(s, font, maxw)
        self.be.draw_text(font, s, x, y, color)

    def fit(self, s: str, font, maxw: int) -> str:
        if self.be.text_size(font, s)[0] <= maxw:
            return s
        n = len(s)
        while n > 6:
            n -= 1
            cand = s[:n // 2] + "…" + s[-(n - n // 2):]
            if self.be.text_size(font, cand)[0] <= maxw:
                return cand
        return s[:5] + "…"

    def wrap(self, s: str, font, maxw: int) -> List[str]:
        out, cur = [], ""
        for word in s.split(" "):
            t = (cur + " " + word).strip()
            if self.be.text_size(font, t)[0] <= maxw or not cur:
                cur = t
            else:
                out.append(cur)
                cur = word
        return out + ([cur] if cur else [])

    def header(self, title: str):
        self.rect(PANEL, 0, 0, W, 50)
        self.text(title, 20, 10, self.f_l, ACC)

    def footer(self, hint: str):
        self.rect(PANEL, 0, H - 34, W, 34)
        self.text(hint, 12, H - 28, self.f_s, DIM, maxw=W - 24)

    def rows(self, rows, sel, y0=70, rh=44, x=20, w=W - 40, two_line=False, max_rows=None):
        vis = max_rows or (H - 34 - y0) // rh
        top = max(0, min(sel - vis + 1, len(rows) - vis))
        for i, (label, value) in enumerate(rows[top:top + vis], start=top):
            y = y0 + (i - top) * rh
            if i == sel:
                self.rect(PANEL, x - 8, y, w + 16, rh - 4)
                self.rect(ACC, x - 8, y, 4, rh - 4)
            if two_line:
                self.text(label, x, y + 3, self.f_s, DIM)
                self.text(value, x, y + 23, self.f_m, FG, maxw=w)
            else:
                self.text(label, x, y + 8, maxw=w)

    def key_cap(self, x, y, w, h, label, active):
        self.rect(ACC if active else PANEL, x, y, w, h)
        tw, th = self.be.text_size(self.f_m, label)
        self.be.draw_text(self.f_m, label, x + (w - tw) // 2, y + (h - th) // 2, BG if active else FG)

    def bar(self, x, y, w, h, frac):
        self.rect(PANEL, x, y, w, h)
        if frac < 0:   # неизвестный прогресс — «бегунок»
            pos = (self.be.ticks() // 8) % (w - 80)
            self.rect(ACC, x + pos, y, 80, h)
        else:
            self.rect(ACC, x, y, int(w * min(frac, 1.0)), h)
