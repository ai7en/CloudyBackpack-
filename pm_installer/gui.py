"""Экраны и логика GUI (640x480). Рисование и ввод — через бэкенд (pygame или PySDL2).

Действия: up/down/left/right, ok (A), back (B), x (удалить), y (Shift), start (Готово).
Если A и B на вашей консоли перепутаны — «Настройки → Кнопки A/B».
"""
import dataclasses
import logging
import os
import threading
import time
from pathlib import Path
from typing import Callable, List, Optional

from .clouds import available_providers
from .constants import APP_NAME, SCREEN_H as H, SCREEN_W as W
from .diagnostics import DEFAULT_TARGETS, Check, origin_of, run_checks
from .errors import CancelledError, InstallerError
from .firmware import describe_targets, detect, env_cfw_name
from . import cache, updater
from .httpfallback import default_session
from . import __version__
from . import manager as mgr
from . import space
from .history import History, display_name
from .installer import ConflictAction, PortInstaller
from .settings import CONFLICT_LABELS, CONFLICT_POLICIES, cloud_title

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
    ITEMS = ["Облака", "Установленные порты", "Настройки", "Проверка сети", "Пути установки", "Обновление", "Выход"]

    def __init__(self):
        self.sel = 0
        self.info = ""

    def on_show(self, gui):
        key = gui.settings.firmware
        auto = key == "auto"
        if auto:
            key = detect(gui.profiles, gui.root, gui.cfw)
        label = gui.profiles[key].label if key in gui.profiles else "не определена"
        self.info = f"Прошивка: {label}{' (авто)' if auto else ''} · версия {__version__}"

    def handle(self, act, gui):
        self.sel = _move(self.sel, act, len(self.ITEMS))
        if act == "back":
            gui.running = False
        elif act == "ok":
            if self.sel == 0:
                gui.push(CloudsScreen())
            elif self.sel == 1:
                gui.push(ManagerScreen(gui))
            elif self.sel == 2:
                gui.push(SettingsScreen())
            elif self.sel == 3:
                gui.push(NetCheckScreen(gui))
            elif self.sel == 4:
                gui.push(PathsScreen())
            elif self.sel == 5:
                gui.push(UpdateScreen(gui))
            else:
                gui.running = False

    def draw(self, gui):
        gui.header(APP_NAME)
        gui.text(self.info, 20, 62, gui.f_s, DIM)
        gui.rows([(t, "") for t in self.ITEMS], self.sel, y0=95)
        gui.footer("↑↓ выбор   A — открыть   B / Select+Start — выход")


class SettingsScreen(Screen):
    ROWS = [("firmware", "Прошивка"), ("temp_dir", "Временная папка"),
            ("conflict_policy", "При конфликте имён"), ("swap_ab", "Кнопки A/B"),
            ("keep_downloads", "Хранить скачанные архивы"), ("clear_cache", "Очистить кэш загрузок")]
    TEXT_ROWS = ("temp_dir",)

    def __init__(self):
        self.sel = 0

    def _options(self, key, gui):
        if key == "cloud":
            return ["auto"] + [k for k, _ in available_providers()]
        if key == "firmware":
            return ["auto"] + list(gui.profiles)
        if key in ("swap_ab", "keep_downloads"):
            return [False, True]
        return list(CONFLICT_POLICIES)

    def _pretty(self, key, gui):
        if key == "clear_cache":
            n, size = cache.usage(self._temp_root(gui))
            return f"{n} файл(ов), {mgr.format_size(size)} — A: очистить" if n else "пусто"
        v = getattr(gui.settings, key)
        if key == "keep_downloads":
            return "Да — не качать повторно" if v else "Нет — удалять после установки"
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

    @staticmethod
    def _temp_root(gui):
        return Path(gui.settings.temp_dir or gui.store.default_temp)

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
        elif key == "clear_cache":
            if act == "ok":
                freed = cache.clear(self._temp_root(gui))
                gui.push(MessageScreen("Кэш очищен", [f"Освобождено: {mgr.format_size(freed)}"], OKC))
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


class UpdateScreen(Screen):
    """Проверка и установка обновления приложения."""

    def __init__(self, gui: "Gui"):
        self.state = "checking"          # checking | ready | latest | applying | done | error
        self.info = None
        self.error = ""
        self.msg = ""
        self.done_bytes = 0
        threading.Thread(target=self._check, args=(gui,), daemon=True).start()

    def _check(self, gui):
        try:
            self.info = updater.check(default_session(), gui.settings.update_url)
            self.state = "ready" if self.info.newer else "latest"
        except InstallerError as e:
            self.error, self.state = str(e), "error"
        except Exception as e:  # noqa: BLE001
            log.exception("Сбой проверки обновлений")
            self.error, self.state = f"Непредвиденная ошибка: {e}", "error"

    def _apply(self, gui):
        root = Path(__file__).resolve().parent.parent
        script = os.environ.get("PM_SCRIPT_PATH")
        try:
            files = updater.apply(default_session(), self.info, root, gui.store.home / "backup",
                                  Path(script) if script else None,
                                  lambda what, n: (setattr(self, "msg", what), setattr(self, "done_bytes", n)))
            self.msg = f"Обновлено файлов: {len(files)}"
            self.state = "done"
        except InstallerError as e:
            self.error, self.state = str(e), "error"
        except Exception as e:  # noqa: BLE001
            log.exception("Сбой обновления")
            self.error, self.state = f"Непредвиденная ошибка: {e}", "error"

    def handle(self, act, gui):
        if self.state in ("checking", "applying"):
            return
        if self.state == "ready" and act == "ok":
            self.state, self.msg = "applying", "Загрузка"
            threading.Thread(target=self._apply, args=(gui,), daemon=True).start()
        elif self.state == "done" and act == "ok":
            gui.running = False
        elif act in ("back", "ok"):
            gui.pop()

    def draw(self, gui):
        gui.header("Обновление")
        y = 70
        gui.text(f"Установлена версия: {__version__}", 20, y)
        if self.state == "checking":
            gui.text("Проверяю наличие обновления…", 20, y + 40, color=DIM)
            gui.footer("")
        elif self.state == "latest":
            gui.text(f"Последняя версия: {self.info.version}. Обновление не требуется.", 20, y + 40, color=OKC,
                     maxw=W - 40)
            gui.footer("A/B — назад")
        elif self.state == "ready":
            gui.text(f"Доступна версия: {self.info.version}", 20, y + 36, color=WARN)
            for i, part in enumerate(gui.wrap(self.info.notes or "Описание не указано.", gui.f_s, W - 40)[:8]):
                gui.text(part, 20, y + 76 + 22 * i, gui.f_s, DIM)
            gui.footer("A — обновить   B — назад")
        elif self.state == "applying":
            gui.text(f"{self.msg}… {mgr.format_size(self.done_bytes)}", 20, y + 40)
            gui.bar(20, y + 80, W - 40, 22, -1)
            gui.footer("")
        elif self.state == "done":
            gui.text("Готово. Обновление установлено.", 20, y + 40, color=OKC)
            gui.text(self.msg, 20, y + 70, gui.f_s, DIM)
            gui.text("Нажмите A — приложение закроется; запустите его снова.", 20, y + 100, gui.f_s, maxw=W - 40)
            gui.footer("A — закрыть приложение")
        else:
            for i, part in enumerate(gui.wrap(self.error, gui.f_m, W - 40)):
                gui.text(part, 20, y + 40 + 26 * i, color=ERR)
            gui.footer("A/B — назад")


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

    def __init__(self, gui: "Gui", provider=None, files=None, source=None):
        self.provider, self.files, self.source = provider, files, source
        self.state = "running"
        self.stage, self.frac, self.msg = "", -1.0, "Запуск…"
        self.started = time.monotonic()
        self.lines: List[str] = []
        self.error = ""
        self.dialog: Optional[dict] = None
        self.cancel = threading.Event()
        self.installer = PortInstaller(
            gui.settings, gui.profiles, root=gui.root, resolver=self._resolve,
            progress=self._progress, cancel=self.cancel, temp_default=gui.store.default_temp)
        self.installer.history = gui.history
        self.thread = threading.Thread(target=self._work, daemon=True)
        self.thread.start()

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
            if self.files is not None:     # список выбран на экране облака
                reports = self.installer.install_remote(self.provider, self.files, self.source)
            else:
                reports = self.installer.install(choose=self._choose)
            for rep in reports:
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
            self._draw_running(gui)
            gui.footer("B — отмена   Select+Start — выход")
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

    STEPS = (("download", "Загрузка"), ("extract", "Распаковка"), ("install", "Копирование"))

    def _draw_running(self, gui):
        order = [k for k, _ in self.STEPS]
        cur = order.index(self.stage) if self.stage in order else -1
        x = 20
        for i, (_, label) in enumerate(self.STEPS):
            col = OKC if i < cur else FG if i == cur else DIM
            t = f"{i + 1} {label}"
            gui.text(t, x, 62, gui.f_s, col)
            x += gui.be.text_size(gui.f_s, t)[0] + 14
            if i < len(self.STEPS) - 1:
                gui.text("›", x - 8, 62, gui.f_s, DIM)
                x += 10
        lines = self.msg.split("\n")
        gui.text(lines[0], 20, 92, maxw=W - 110)
        if self.frac >= 0:
            pct = f"{int(self.frac * 100)}%"
            gui.text(pct, W - 20 - gui.be.text_size(gui.f_m, pct)[0], 92)
        gui.bar(20, 128, W - 40, 22, self.frac)
        y = 168
        for ln in lines[1:5]:
            gui.text(ln, 20, y, gui.f_s, FG if y == 168 else DIM, maxw=W - 40)
            y += 26
        el = int(time.monotonic() - self.started)
        gui.text(f"Прошло {el // 60}:{el % 60:02d}", 20, H - 68, gui.f_s, DIM)

    def _draw_dialog(self, gui, d):
        gui.rect(PANEL, 30, 90, W - 60, 300)
        gui.outline(ACC, 30, 90, W - 60, 300, 2)
        gui.text(d["title"], 46, 104, maxw=W - 92)
        gui.rows([(o, "") for o in d["options"]], d["sel"], y0=146, rh=38, x=46, w=W - 92, max_rows=6)


class Confirm(Screen):
    """Полноэкранное подтверждение. По умолчанию выбрано «Нет»."""

    def __init__(self, title: str, lines: List[str], on_yes: Callable[[], None], yes_label="Да, удалить"):
        self.title, self.lines, self.on_yes, self.yes_label = title, lines, on_yes, yes_label
        self.sel = 0

    def handle(self, act, gui):
        if act in ("up", "down", "left", "right"):
            self.sel = 1 - self.sel
        elif act == "back":
            gui.pop()
        elif act == "ok":
            gui.pop()
            if self.sel == 1:
                self.on_yes()

    def draw(self, gui):
        gui.header(self.title)
        y = 70
        for ln in self.lines:
            for part in gui.wrap(ln, gui.f_m, W - 40):
                gui.text(part, 20, y, color=FG)
                y += 26
        gui.rows([("Нет", ""), (self.yes_label, "")], self.sel, y0=max(y + 20, 250), rh=44)
        gui.footer("↑↓ выбор   A — подтвердить   B — отмена")


class CloudsScreen(Screen):
    """Список сохранённых облаков."""

    def __init__(self):
        self.sel = 0

    @staticmethod
    def _type_label(entry):
        c = entry.get("cloud") or "auto"
        return "Авто" if c == "auto" else dict(available_providers()).get(c, c)

    def handle(self, act, gui):
        clouds = gui.settings.clouds
        n = len(clouds) + 1
        self.sel = min(self.sel, n - 1)
        self.sel = _move(self.sel, act, n)
        if act == "back":
            gui.pop()
        elif act == "ok":
            if self.sel == len(clouds):
                gui.push(CloudEditScreen(None))
            else:
                gui.push(BrowseScreen(gui, clouds[self.sel]))
        elif act == "y" and self.sel < len(clouds):
            gui.push(CloudEditScreen(self.sel))
        elif act == "x" and self.sel < len(clouds):
            entry = clouds[self.sel]
            gui.push(Confirm("Удалить облако?", [cloud_title(entry), entry["url"]],
                             lambda i=self.sel: self._remove(gui, i)))

    def _remove(self, gui, idx):
        if 0 <= idx < len(gui.settings.clouds):
            del gui.settings.clouds[idx]
            _save(gui)
            self.sel = max(0, min(self.sel, len(gui.settings.clouds)))

    def draw(self, gui):
        gui.header("Облака")
        clouds = gui.settings.clouds
        n = len(clouds) + 1
        self.sel = min(self.sel, n - 1)

        def item(i, x, y, w):
            if i == len(clouds):
                gui.text("+ Добавить облако", x, y + 12, color=ACC)
                return
            e = clouds[i]
            gui.text(cloud_title(e), x, y + 3, maxw=w)
            gui.text(f"{self._type_label(e)} · {e['url']}", x, y + 29, gui.f_s, DIM, maxw=w)
        if not clouds:
            gui.text("Облаков пока нет. Добавьте ссылку на папку", 20, 62, gui.f_s, DIM)
        gui.list_view(n, self.sel, 84 if not clouds else 62, 56, item)
        gui.footer("A открыть   Y изменить   X удалить   B назад")


class CloudEditScreen(Screen):
    """Название / ссылка / тип облака. Сохраняется при выходе, если ссылка не пустая."""
    ROWS = [("name", "Название"), ("url", "Ссылка"), ("cloud", "Тип облака")]

    def __init__(self, index: Optional[int]):
        self.index = index
        self.draft = {"name": "", "url": "", "cloud": "auto"}
        self.loaded = False
        self.sel = 1 if index is None else 0

    def on_show(self, gui):
        if not self.loaded:
            self.loaded = True
            if self.index is not None and self.index < len(gui.settings.clouds):
                self.draft = dict(gui.settings.clouds[self.index])

    def _commit(self, gui):
        d = {k: (v or "").strip() for k, v in self.draft.items()}
        if not d["url"]:
            return
        if self.index is None:
            gui.settings.clouds.append(d)
        elif self.index < len(gui.settings.clouds):
            gui.settings.clouds[self.index] = d
        _save(gui)

    def handle(self, act, gui):
        self.sel = _move(self.sel, act, len(self.ROWS))
        key, title = self.ROWS[self.sel]
        if act in ("back", "start"):
            self._commit(gui)
            gui.pop()
        elif key != "cloud" and act == "ok":
            gui.push(TextInput(title, self.draft.get(key, ""),
                               lambda v, k=key: self.draft.__setitem__(k, v.strip())))
        elif key == "cloud" and act in ("left", "right", "ok"):
            opts = ["auto"] + [k for k, _ in available_providers()]
            cur = self.draft.get("cloud") or "auto"
            i = (opts.index(cur) if cur in opts else 0) + (-1 if act == "left" else 1)
            self.draft["cloud"] = opts[i % len(opts)]

    def draw(self, gui):
        gui.header("Изменить облако" if self.index is not None else "Новое облако")
        vals = {"name": self.draft.get("name") or "(по адресу ссылки)",
                "url": self.draft.get("url") or "(не задана)",
                "cloud": CloudsScreen._type_label(self.draft)}
        gui.rows([(t, vals[k]) for k, t in self.ROWS], self.sel, y0=60, rh=56, two_line=True)
        gui.text("Для Яндекс.Диска, Google Drive и Mail.ru тип можно оставить «Авто».", 20, 240, gui.f_s, DIM, maxw=W - 40)
        gui.footer("A ввод   ←→ тип   B/Start сохранить и назад")


class BrowseScreen(Screen):
    """Облако как проводник: папки, поиск, отметки архивов (отметки сохраняются при переходах)."""

    def __init__(self, gui: "Gui", entry: dict):
        self.entry = dict(entry)
        self.state = "loading"
        self.dirs: list = []
        self.files: list = []
        self.stack: list = []            # [(path, prefix, name)] — путь от корня до текущей папки
        self.marked: dict = {}           # ключ (имя с подпапкой) → RemoteFile
        self.filter = ""
        self.rows: list = []
        self.sel = 0
        self.error = ""
        self.states: dict = {}           # ключ → new/same/changed
        self.provider = None
        self._token = 0
        self._load(gui)

    # ---- загрузка текущей папки
    def _load(self, gui):
        self._token += 1
        token = self._token
        self.state, self.error = "loading", ""
        path, prefix = (self.stack[-1][0], self.stack[-1][1]) if self.stack else (None, "")
        st = dataclasses.replace(gui.settings, url=self.entry["url"], cloud=self.entry.get("cloud") or "auto")
        inst = PortInstaller(st, gui.profiles, root=gui.root, temp_default=gui.store.default_temp)

        def work():
            try:
                provider, dirs, files = inst.open_dir(path, prefix)
                if token != self._token:
                    return
                self.provider, self.dirs, self.files = provider, dirs, files
                self.states = {key(f): gui.history.status(self.entry["url"], f) for f in files}
                self.filter, self.sel = "", 0
                self._rebuild()
                self.state = "ready"
            except InstallerError as e:
                log.error("Ошибка чтения облака: %s", e)
                if token == self._token:
                    self.error, self.state = str(e), "error"
            except Exception as e:  # noqa: BLE001
                log.exception("Непредвиденная ошибка чтения облака")
                if token == self._token:
                    self.error, self.state = f"Непредвиденная ошибка: {e}", "error"
        threading.Thread(target=work, daemon=True).start()

    def _rebuild(self):
        q = self.filter.lower()
        rows = [("up", None)] if self.stack else []
        rows += [("dir", d) for d in self.dirs if q in d.name.lower()]
        rows += [("file", f) for f in self.files if q in (display_name(f) or "").lower()]
        self.rows = rows
        self.sel = min(self.sel, max(0, len(rows) - 1))

    def _files_in_view(self):
        return [f for k, f in self.rows if k == "file"]

    def _enter(self, gui, d):
        self.stack.append((d.path, d.meta.get("prefix", ""), d.name))
        self._load(gui)

    def _up(self, gui):
        self.stack.pop()
        self._load(gui)

    def handle(self, act, gui):
        if act == "back":
            if self.state == "ready" and self.filter:
                self.filter = ""
                self._rebuild()
            elif self.stack:
                self._up(gui)
            else:
                self._token += 1
                gui.pop()
            return
        if self.state == "error":
            if act == "ok":
                self._load(gui)
            return
        if self.state != "ready":
            return
        n = len(self.rows)
        if act == "select":
            def done(v):
                self.filter, self.sel = v.strip(), 0
                self._rebuild()
            gui.push(TextInput("Поиск по названию", self.filter, done))
            return
        if not n:
            return
        self.sel = _move(self.sel, act, n)
        kind, obj = self.rows[self.sel]
        view = self._files_in_view()
        if act == "x" and kind == "file":
            self.marked.pop(key(obj), None) if key(obj) in self.marked else self.marked.__setitem__(key(obj), obj)
        elif act == "y":
            if view and all(key(f) in self.marked for f in view):
                for f in view:
                    self.marked.pop(key(f), None)
            else:
                self.marked.update({key(f): f for f in view})
        elif act == "r":                              # отметить всё, что изменилось в облаке (в этой папке)
            self.marked.update({key(f): f for f in view if self.states.get(key(f)) == "changed"})
        elif act == "ok" and kind == "up":
            self._up(gui)
        elif act == "ok" and kind == "dir":
            self._enter(gui, obj)
        elif act in ("ok", "start"):
            chosen = sorted(self.marked.values(), key=key) or ([obj] if kind == "file" else [])
            if chosen:
                gui.push(InstallScreen(gui, self.provider, chosen, source=self.entry))

    def draw(self, gui):
        where = "/".join(n for _, _, n in self.stack)
        gui.header(cloud_title(self.entry) + (f" / {where}" if where else ""))
        if self.state == "loading":
            gui.text("Читаю список файлов…", 20, 80)
            gui.bar(20, 120, W - 40, 22, -1)
            gui.footer("B — назад")
        elif self.state == "error":
            y = 80
            for part in gui.wrap(self.error, gui.f_m, W - 40):
                gui.text(part, 20, y, color=ERR)
                y += 26
            gui.footer("A — повторить   B — назад")
        else:
            chosen = list(self.marked.values()) or [obj for k, obj in self.rows[self.sel:self.sel + 1] if k == "file"]
            need = max([f.size or 0 for f in chosen] or [0])          # архивы ставятся по одному
            free = space.free_bytes(gui.settings.temp_dir or gui.store.default_temp)
            low = free is not None and need and free < space.needed(need)
            upd = sum(1 for st in self.states.values() if st == "changed")
            head = (f"Папок: {len(self.dirs)} · " if self.dirs else "") + f"архивов: {len(self.files)}"
            if upd:
                head += f" · обновлений: {upd}"
            if self.filter:
                head += f" · поиск: «{self.filter}»"
            head += f" · отмечено: {len(self.marked)} · свободно: {mgr.format_size(free)}"
            gui.text(head + (" — МАЛО МЕСТА" if low else ""), 20, 58, gui.f_s, ERR if low else DIM, maxw=W - 40)

            def item(i, x, y, w):
                kind, obj = self.rows[i]
                if kind == "up":
                    gui.text("..  (назад)", x, y + 8, color=DIM, maxw=w)
                    return
                if kind == "dir":
                    gui.text(f"> {obj.name}/", x, y + 8, color=ACC, maxw=w)
                    return
                f = obj
                mark = "[x]" if key(f) in self.marked else "[ ]"
                size = mgr.format_size(f.size) if f.size else ""
                st = self.states.get(key(f), "new")
                tag = {"same": "✓ установлен", "changed": "↑ обновить"}.get(st, "")
                right = "  ".join(t for t in (tag, size) if t)
                sw = gui.be.text_size(gui.f_s, right)[0] if right else 0
                gui.text(f"{mark} {f.name or display_name(f) or '(без имени)'}", x, y + 8,
                         color=OKC if key(f) in self.marked else FG, maxw=w - sw - 12)
                if right:
                    gui.text(right, x + w - sw, y + 11, gui.f_s, {"same": OKC, "changed": WARN}.get(st, DIM))
            if self.rows:
                gui.list_view(len(self.rows), self.sel, 84, 40, item)
            else:
                gui.text("Ничего не найдено." if self.filter else "Здесь нет архивов.", 20, 90, color=DIM)
            gui.footer("A открыть/установить  X отметить  Y все  R обновления  SELECT поиск  B назад")


def key(f) -> str:
    return display_name(f) or f.name


class ManagerScreen(Screen):
    """Установленные на устройстве порты: что есть (.sh / папка), размер, дата, удаление."""
    SORTS = [("по имени", lambda p: p.name.lower()),
             ("по размеру", lambda p: -(p.size or -1)),
             ("по дате", lambda p: -p.mtime)]

    def __init__(self, gui: "Gui"):
        self.state = "loading"
        self.ports: List[mgr.InstalledPort] = []
        self.sel = 0
        self.sort = 0
        self.error = ""
        self.profile = None
        self.fw_label = ""
        self.root = gui.root
        self.history = gui.history
        self.free: Optional[int] = None
        self.pending: Optional[tuple] = None     # (заголовок, строки, цвет) — показать из основного потока
        threading.Thread(target=self._scan, args=(gui,), daemon=True).start()

    def _scan(self, gui):
        try:
            key = PortInstaller(gui.settings, gui.profiles, root=gui.root).firmware_key()
            self.profile = gui.profiles[key]
            self.fw_label = self.profile.label
            self.ports = mgr.scan(self.profile, gui.root)
            for p in self.ports:
                p.source = gui.history.source_of(p.names)
            dirs = mgr.existing_dirs(self.profile.port_dirs, gui.root)
            self.free = space.free_bytes(dirs[0]) if dirs else None
            self.state = "ready"
        except InstallerError as e:
            self.error, self.state = str(e), "error"
            return
        except Exception as e:  # noqa: BLE001
            log.exception("Сбой просмотра портов")
            self.error, self.state = f"Не удалось прочитать порты: {e}", "error"
            return
        self._resort()
        for p in list(self.ports):         # размеры считаются по очереди, список уже виден
            mgr.compute_size(p)

    def _resort(self):
        self.ports.sort(key=self.SORTS[self.sort][1])

    def _delete(self, port):
        def work():
            try:
                freed = mgr.delete_port(port, self.profile, self.root)
                self.history.forget_port(port.names)
                self.ports = [p for p in self.ports if p is not port]
                dirs = mgr.existing_dirs(self.profile.port_dirs, self.root)
                self.free = space.free_bytes(dirs[0]) if dirs else None
                self.pending = ("Удалено", [f"«{port.name}» удалён.", f"Освобождено: {mgr.format_size(freed)}"], OKC)
            except InstallerError as e:
                self.pending = ("Не удалось удалить", [str(e)], ERR)
            except Exception as e:  # noqa: BLE001
                log.exception("Сбой удаления")
                self.pending = ("Не удалось удалить", [str(e)], ERR)
            self.state = "ready"
        self.state = "busy"
        threading.Thread(target=work, daemon=True).start()

    def tick(self, gui):
        if self.pending and gui.top is self:
            t, lines, color = self.pending
            self.pending = None
            self.sel = max(0, min(self.sel, len(self.ports) - 1))
            gui.push(MessageScreen(t, lines, color))

    def handle(self, act, gui):
        if act == "back":
            if self.state != "busy":
                gui.pop()
            return
        if self.state != "ready" or not self.ports:
            return
        self.sel = _move(self.sel, act, len(self.ports))
        port = self.ports[self.sel]
        if act == "y":
            self.sort = (self.sort + 1) % len(self.SORTS)
            self._resort()
            self.sel = 0
        elif act == "ok":
            lines = [f"Тип: {port.kind}",
                     f"Скрипт: {port.script or '—'}", f"Папка: {port.folder or '—'}",
                     f"Размер: {mgr.format_size(port.size)}", f"Дата: {mgr.format_date(port.mtime)}"]
            if port.source:
                src = port.source
                lines += [f"Облако: {src.get('cloud') or src.get('url')}", f"Файл: {src.get('file') or '—'}",
                          f"Установлен приложением: {src.get('date', '—')}"]
            gui.push(MessageScreen(port.name, lines))
        elif act == "x":
            if port.protected:
                gui.push(MessageScreen("Нельзя удалить", ["Это само приложение — оно нужно для установки портов."], WARN))
                return
            parts = []
            if port.script:
                parts.append("файл .sh")
            if port.folder:
                parts.append(f"папка ({mgr.format_size(port.size)})")
            gui.push(Confirm("Удалить порт?", [port.name, "Будет удалено: " + " и ".join(parts) + "."],
                             lambda p=port: self._delete(p)))

    def draw(self, gui):
        gui.header("Установленные порты")
        if self.state == "loading":
            gui.text("Ищу порты…", 20, 80)
            gui.footer("B — назад")
            return
        if self.state == "error":
            y = 80
            for part in gui.wrap(self.error, gui.f_m, W - 40):
                gui.text(part, 20, y, color=ERR)
                y += 26
            gui.footer("B — назад")
            return
        known = [p.size for p in self.ports if p.size is not None]
        total = mgr.format_size(sum(known)) + ("" if len(known) == len(self.ports) else "…")
        gui.text(f"{self.fw_label} · портов: {len(self.ports)} · занято: {total} · свободно: "
                 f"{mgr.format_size(self.free)} · {self.SORTS[self.sort][0]}", 20, 58, gui.f_s, DIM, maxw=W - 40)
        if not self.ports:
            gui.text("Установленных портов не найдено.", 20, 100)
        kind_color = {"sh + папка": OKC}

        def item(i, x, y, w):
            p = self.ports[i]
            size = mgr.format_size(p.size)
            sw = gui.be.text_size(gui.f_m, size)[0]
            gui.text(p.name, x, y + 3, maxw=w - sw - 12)
            gui.text(size, x + w - sw, y + 3, color=DIM if p.size is None else FG)
            gui.text(f"{p.kind} · {mgr.format_date(p.mtime)}" + (" · это приложение" if p.protected else "")
                     + (f" · {p.source.get('cloud') or 'облако'}" if p.source else ""),
                     x, y + 29, gui.f_s, DIM if p.protected else kind_color.get(p.kind, WARN), maxw=w)
        gui.list_view(len(self.ports), self.sel, 84, 52, item)
        if self.state == "busy":
            gui.rect(PANEL, 120, 190, W - 240, 80)
            gui.outline(ACC, 120, 190, W - 240, 80, 2)
            gui.text("Удаление…", 150, 218)
        gui.footer("A подробно   X удалить   Y сортировка   B назад")


def _save(gui):
    try:
        gui.store.save(gui.settings)
    except InstallerError as e:
        gui.push(MessageScreen("Не удалось сохранить", [str(e)], ERR))



# ====================================================================== приложение

class Gui:
    def __init__(self, settings, store, profiles, backend, root: str = "", font_path=None):
        self.settings, self.store, self.profiles, self.root = settings, store, profiles, root
        self.be = backend
        self.cfw = env_cfw_name(root)
        self.f_s, self.f_m, self.f_l = backend.load_fonts(font_path or find_font(), (16, 20, 28))
        self.history = History(Path(getattr(store, 'home', '.')) / 'installed.json')
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
                self.quit()
            else:
                self.top.handle(act, self)
        self.top.tick(self)
        self.be.clear(BG)
        self.top.draw(self)
        self.be.present()

    def quit(self):
        """Выход из любого места (Select+Start, закрытие окна): отменяет идущую установку."""
        self.running = False
        for scr in self.stack:
            ev = getattr(scr, "cancel", None)
            if isinstance(ev, threading.Event):
                ev.set()

    def run(self):
        try:
            while self.running:
                self.step()
                self.be.delay(33)
        finally:
            for scr in self.stack:             # даём рабочему потоку убрать временные файлы
                t = getattr(scr, "thread", None)
                if t is not None and t.is_alive():
                    t.join(3)
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

    def list_view(self, n, sel, y0, rh, draw_item, x=20, w=W - 40):
        """Прокручиваемый список: подсветка выбранной строки, полоса прокрутки; draw_item(i, x, y, w)."""
        vis = max(1, (H - 34 - y0) // rh)
        top = max(0, min(sel - vis + 1, n - vis))
        for i in range(top, min(n, top + vis)):
            y = y0 + (i - top) * rh
            if i == sel:
                self.rect(PANEL, x - 8, y, w + 16, rh - 4)
                self.rect(ACC, x - 8, y, 4, rh - 4)
            draw_item(i, x, y, w - 10 if n > vis else w)
        if n > vis:
            track = vis * rh - 4
            bar_h = max(16, track * vis // n)
            self.rect(PANEL, W - 10, y0, 4, track)
            self.rect(DIM, W - 10, y0 + (track - bar_h) * top // max(1, n - vis), 4, bar_h)

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
