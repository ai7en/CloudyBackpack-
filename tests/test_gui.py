"""GUI-логика на фиктивном бэкенде + PySDL2-бэкенд на заглушке модуля sdl2.
(Реальные pygame/PySDL2 в песочнице не установлены: проверяется наша логика, а не их API.)
"""
import ctypes
import functools
import http.server
import os
import shutil
import sys
import tempfile
import threading
import time
import types
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace as NS
from unittest import mock

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
os.environ["NO_PROXY"] = "127.0.0.1,localhost"

from pm_installer import gui as G  # noqa: E402
from pm_installer.backends import BackendUnavailable, create_backend  # noqa: E402
from pm_installer.firmware import load_profiles  # noqa: E402
from pm_installer.settings import SettingsStore  # noqa: E402

PROFILES = load_profiles(BASE / "config" / "paths.json")


class FakeBackend:
    name = "fake"

    def __init__(self):
        self.swap_ab, self.queue, self.drawn, self.closed = False, [], 0, False

    def load_fonts(self, path, sizes): return [object() for _ in sizes]
    def text_size(self, font, s): return (len(s) * 8, 16)
    def draw_text(self, *a): self.drawn += 1
    def clear(self, c): pass
    def fill_rect(self, *a): pass
    def outline(self, *a): pass
    def present(self): pass
    def ticks(self): return 1000
    def delay(self, ms): pass
    def close(self): self.closed = True

    def poll(self):
        q, self.queue = self.queue, []
        return q


class TestGui(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="pmgui_"))
        self.root = str(self.tmp / "root")
        self.store = SettingsStore(self.tmp / "home")
        self.st = self.store.load()
        self.st.firmware, self.st.temp_dir = "rocknix", str(self.tmp / "work")
        self.be = FakeBackend()
        self.g = G.Gui(self.st, self.store, PROFILES, self.be, self.root, font_path="x.ttf")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def press(self, *acts):
        self.be.queue += list(acts)
        self.g.step()

    def test_add_cloud_via_keyboard_and_persist(self):
        self.press("ok")                               # «Облака»
        self.assertIsInstance(self.g.top, G.CloudsScreen)
        self.press("ok")                               # «+ Добавить облако»
        self.assertIsInstance(self.g.top, G.CloudEditScreen)
        self.press("ok")                               # курсор сразу на «Ссылка» -> клавиатура
        self.assertIsInstance(self.g.top, G.TextInput)
        self.press(*["text:" + c for c in "disk.yandex.ru/d/Ab"], "y", "ok", "x", "start")
        self.press("back")                             # выход из редактора сохраняет облако
        self.assertIsInstance(self.g.top, G.CloudsScreen)
        self.assertEqual(len(self.st.clouds), 1)
        self.assertEqual(self.st.clouds[0]["url"], "disk.yandex.ru/d/Ab")   # Shift+символ добавил «Q», X его стёр
        self.assertEqual(self.store.load().clouds, self.st.clouds, "сохраняется в JSON")
        self.assertGreater(self.be.drawn, 0)

    def test_empty_cloud_is_discarded_and_delete_needs_confirmation(self):
        self.press("ok", "ok", "back")                 # новое облако без ссылки не сохраняется
        self.assertEqual(self.st.clouds, [])
        self.st.clouds = [{"name": "A", "url": "https://x/a", "cloud": "auto"}]
        self.press("x")                                # подтверждение, по умолчанию «Нет»
        self.assertIsInstance(self.g.top, G.Confirm)
        self.press("ok")
        self.assertEqual(len(self.st.clouds), 1)
        self.press("x", "down", "ok")                  # «Да, удалить»
        self.assertEqual(self.st.clouds, [])
        self.assertEqual(self.store.load().clouds, [])

    def test_edit_cloud_type_cycles(self):
        self.st.clouds = [{"name": "A", "url": "https://x/a", "cloud": "auto"}]
        self.press("ok", "y")                          # изменить
        self.assertIsInstance(self.g.top, G.CloudEditScreen)
        self.press("down", "down", "right")            # «Тип облака»
        self.press("back")
        self.assertNotEqual(self.st.clouds[0]["cloud"], "auto")
        self.assertEqual(self.st.clouds[0]["url"], "https://x/a")

    def test_swap_ab_option(self):
        self.press("down", "down", "ok")               # в настройки
        for _ in range(3):
            self.press("down")
        self.press("right")
        self.assertTrue(self.st.swap_ab)
        self.g.step()
        self.assertTrue(self.be.swap_ab, "бэкенд получает флаг на следующем кадре")
        self.assertTrue(self.store.load().swap_ab)

    def test_paths_and_quit(self):
        self.press("down", "down", "down", "down", "ok")
        self.assertIsInstance(self.g.top, G.PathsScreen)
        self.press("back")
        self.press("quit")
        self.assertFalse(self.g.running)

    def test_run_closes_backend(self):
        self.be.queue = ["quit"]
        self.g.run()
        self.assertTrue(self.be.closed)

    def wait_browse(self, scr):
        for _ in range(100):
            self.g.step()
            if scr.state != "loading":
                return
            time.sleep(0.05)

    def test_install_flow_with_conflict_dialog(self):
        port = self.tmp / "p.zip"
        with zipfile.ZipFile(port, "w") as z:
            z.writestr("Game.sh", "x")
            z.writestr("game/a", "1")
        handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(self.tmp))
        handler.log_message = lambda *a, **k: None
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        self.st.clouds = [{"name": "Тест", "url": f"http://127.0.0.1:{srv.server_address[1]}/p.zip",
                           "cloud": "direct"}]
        self.st.conflict_policy = "ask"

        def run_install():
            self.press("ok")                            # открыть облако
            browse = self.g.top
            self.assertIsInstance(browse, G.BrowseScreen)
            self.wait_browse(browse)
            self.assertEqual(browse.state, "ready", browse.error)
            self.press("ok")                            # установить выделенный архив
            scr = self.g.top
            self.assertIsInstance(scr, G.InstallScreen)
            for _ in range(200):
                self.g.step()
                if scr.dialog:
                    self.be.queue += ["down", "ok"]     # «Пропустить»
                if scr.state != "running":
                    break
                time.sleep(0.05)
            return scr

        self.press("ok")                                # главное меню -> «Облака»
        s1 = run_install()
        self.assertEqual(s1.state, "done", s1.error)
        self.press("ok", "back")                        # из установки в список облака и обратно к облакам
        s2 = run_install()
        self.assertEqual(s2.state, "done", s2.error)
        self.assertTrue(any("Пропущено" in ln for ln in s2.lines))
        self.assertTrue(Path(self.root + "/roms/ports/Game.sh").is_file())

    def test_install_error_is_shown(self):
        from pm_installer.clouds.base import CloudProvider
        patcher = mock.patch.object(CloudProvider, "RETRIES", 0)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.st.clouds = [{"name": "Мёртвое", "url": "http://127.0.0.1:1/x.zip", "cloud": "direct"}]
        self.press("ok", "ok")
        self.wait_browse(self.g.top)
        self.press("ok")                                # установить
        scr = self.g.top
        self.assertIsInstance(scr, G.InstallScreen)
        for _ in range(100):
            self.g.step()
            if scr.state != "running":
                break
            time.sleep(0.05)
        self.assertEqual(scr.state, "error")
        self.assertIn("отклонил соединение", scr.error)
        self.assertIn("127.0.0.1", scr.error)

    def test_browse_error_then_retry(self):
        from pm_installer.errors import InvalidLinkError
        fake_inst = mock.Mock()
        fake_inst.open_dir.side_effect = InvalidLinkError("Ссылка не найдена (404)")
        with mock.patch.object(G, "PortInstaller", return_value=fake_inst):
            self.st.clouds = [{"name": "Мёртвое", "url": "https://x/y", "cloud": "auto"}]
            self.press("ok", "ok")
            scr = self.g.top
            self.wait_browse(scr)
            self.assertEqual(scr.state, "error")
            self.assertIn("404", scr.error)
            self.press("ok")                            # повторить
            self.wait_browse(scr)
            self.assertEqual(fake_inst.open_dir.call_count, 2)
        self.press("back")
        self.assertIsInstance(self.g.top, G.CloudsScreen)

    def test_browse_lists_and_marks_several(self):
        from pm_installer.clouds import RemoteFile
        files = [RemoteFile(f"p{i}.zip", 1000 * i, {"folder": True, "display": f"d/p{i}.zip"}) for i in range(1, 4)]
        fake_inst = mock.Mock()
        fake_inst.open_dir.return_value = (mock.Mock(), [], files)
        with mock.patch.object(G, "PortInstaller", return_value=fake_inst):
            self.st.clouds = [{"name": "N", "url": "https://x/y", "cloud": "auto"}]
            self.press("ok", "ok")
            scr = self.g.top
            self.wait_browse(scr)
        self.assertEqual(scr.state, "ready")
        self.press("x", "down", "x")                   # отметить 1-й и 2-й
        self.assertEqual(set(scr.marked), {"d/p1.zip", "d/p2.zip"})
        self.press("y")                                # все
        self.assertEqual(len(scr.marked), 3)
        self.press("y")                                # снять все
        self.assertEqual(scr.marked, {})
        self.press("x", "down", "down", "x")           # выделенная — 2-я; затем по кругу на 1-ю
        with mock.patch.object(G, "InstallScreen") as inst_cls:
            self.press("ok")
        chosen = inst_cls.call_args[0][2]
        self.assertEqual([f.name for f in chosen], ["p1.zip", "p2.zip"])

    def test_quit_cancels_running_install_and_waits_for_cleanup(self):
        gate = threading.Event()
        seen = {}

        class SlowInstaller:
            def __init__(self, *a, **kw):
                seen["cancel"] = kw["cancel"]

            def install_remote(self, provider, files, source=None):
                gate.wait(5)
                seen["cancelled"] = seen["cancel"].is_set()
                raise G.CancelledError("Установка отменена.")
        with mock.patch.object(G, "PortInstaller", SlowInstaller):
            self.g.push(G.InstallScreen(self.g, object(), []))
            scr = self.g.top
            self.be.queue = ["quit"]
            threading.Timer(0.2, gate.set).start()
            self.g.run()
        self.assertFalse(self.g.running)
        self.assertTrue(seen["cancelled"], "выход отправляет отмену в поток установки")
        self.assertFalse(scr.thread.is_alive(), "поток успел завершиться до закрытия окна")
        self.assertTrue(self.be.closed)

    def test_install_screen_shows_stage_details(self):
        with mock.patch.object(G, "PortInstaller") as inst_cls:
            inst_cls.return_value.install_remote.side_effect = lambda *a: (time.sleep(0.5), [])[1]
            self.g.push(G.InstallScreen(self.g, object(), []))
            scr = self.g.top
            scr._progress("extract", 0.42, "Распаковка A.7z\nФайлов: 3 из 10\ngame/bin\n12 МБ из 30 МБ")
            texts = []
            self.be.draw_text = lambda f, s, *a: texts.append(s)
            self.g.step()
            scr.cancel.set()
        joined = "\n".join(texts)
        for part in ("2 Распаковка", "Распаковка A.7z", "42%", "Файлов: 3 из 10", "game/bin", "12 МБ из 30 МБ", "Прошло 0:0"):
            self.assertIn(part, joined)

    def test_netcheck_screen(self):
        from pm_installer.diagnostics import Check
        fake = lambda targets, on_progress=None: [on_progress([Check("Дата и время", "ok", "x"),
                                                                Check("DNS a", "fail", "boom")])] and []
        with mock.patch.object(G, "run_checks", fake):
            self.press("down", "down", "down", "ok")
            scr = self.g.top
            self.assertIsInstance(scr, G.NetCheckScreen)
            for _ in range(40):
                self.g.step()
                if scr.done:
                    break
                time.sleep(0.05)
        self.assertTrue(scr.done)
        self.assertEqual([c.status for c in scr.checks], ["ok", "fail"])
        self.press("down", "up", "back")
        self.assertIsInstance(self.g.top, G.MainMenu)


# ------------------------------------------------------------------ заглушка sdl2

class Stub(types.ModuleType):
    """Любая константа SDL_* / SDLK_* получает уникальное число; функции задаются явно."""

    def __getattr__(self, n):
        if n.startswith("__"):
            raise AttributeError(n)
        c = self.__dict__.setdefault("_c", {})
        return c.setdefault(n, 1000 + len(c))


def make_sdl2():
    sdl2, ttf = Stub("sdl2"), Stub("sdl2.sdlttf")
    events = []
    sdl2.events = events
    sdl2.log = []

    class Event:
        pass

    def poll(ev):
        if not events:
            return 0
        ev.__dict__.update(events.pop(0).__dict__)
        return 1
    d = sdl2.__dict__
    d.update(
        sdlttf=ttf, SDL_Event=Event, SDL_PollEvent=poll, SDL_Init=lambda f: 0, SDL_GetError=lambda: b"",
        SDL_CreateWindow=lambda *a: object(), SDL_CreateRenderer=lambda *a: object(),
        SDL_RenderSetLogicalSize=lambda *a: 0, SDL_ShowCursor=lambda *a: 0, SDL_StartTextInput=lambda: None,
        SDL_NumJoysticks=lambda: 0, SDL_Color=lambda *a: a, SDL_Rect=lambda *a: a,
        SDL_CreateTextureFromSurface=lambda r, s: object(), SDL_FreeSurface=lambda s: None,
        SDL_RenderCopy=lambda *a: sdl2.log.append(("copy", a[3])), SDL_SetRenderDrawColor=lambda *a: 0,
        SDL_RenderClear=lambda r: 0, SDL_RenderFillRect=lambda r, rc: sdl2.log.append(("fill", rc)),
        SDL_RenderDrawRect=lambda r, rc: sdl2.log.append(("rect", rc)), SDL_RenderPresent=lambda r: None,
        SDL_GetTicks=lambda: 5, SDL_Delay=lambda ms: None, SDL_DestroyTexture=lambda t: None,
        SDL_DestroyRenderer=lambda r: None, SDL_DestroyWindow=lambda w: None, SDL_Quit=lambda: None,
        SDL_GameControllerClose=lambda p: None, SDL_JoystickClose=lambda p: None,
        SDL_HAT_UP=1, SDL_HAT_RIGHT=2, SDL_HAT_DOWN=4, SDL_HAT_LEFT=8,   # настоящие битовые маски SDL
    )
    ttf.__dict__.update(
        TTF_Init=lambda: 0, TTF_Quit=lambda: None, TTF_OpenFont=lambda p, s: object(), TTF_CloseFont=lambda f: None,
        TTF_SizeUTF8=lambda f, b, w, h: (setattr(w, "value", len(b) * 8), setattr(h, "value", 16), 0)[2],
        TTF_RenderUTF8_Blended=lambda f, b, c: NS(contents=NS(w=10, h=12)),
    )
    return sdl2, ttf


class TestSdl2Backend(unittest.TestCase):
    def setUp(self):
        self.sdl2, ttf = make_sdl2()
        patches = [mock.patch.dict(sys.modules, {"sdl2": self.sdl2, "sdl2.sdlttf": ttf}),
                   mock.patch.object(ctypes, "byref", lambda x: x)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        sys.modules.pop("pm_installer.backends.sdl2_backend", None)
        self.addCleanup(sys.modules.pop, "pm_installer.backends.sdl2_backend", None)
        from pm_installer.backends.sdl2_backend import SDL2Backend
        self.be = SDL2Backend("t", True)

    def ev(self, **kw):
        self.sdl2.events.append(NS(**kw))

    def test_draw_paths(self):
        fonts = self.be.load_fonts("f.ttf", (16, 20, 28))
        self.assertEqual(len(fonts), 3)
        self.assertEqual(self.be.text_size(fonts[0], "abc"), (24, 16))
        self.be.clear((1, 2, 3))
        self.be.fill_rect((1, 2, 3), 1, 2, 3, 4)
        self.be.outline((1, 2, 3), 0, 0, 10, 10, 2)
        self.be.draw_text(fonts[0], "привет", 5, 6, (9, 9, 9))
        self.be.draw_text(fonts[0], "привет", 5, 6, (9, 9, 9))      # из кэша
        self.be.draw_text(fonts[0], "", 0, 0, (9, 9, 9))
        self.be.present()
        kinds = [k for k, _ in self.sdl2.log]
        self.assertEqual(kinds.count("copy"), 2)
        self.assertEqual(kinds.count("rect"), 2)
        self.assertEqual(len(self.be._tex), 1)
        self.be.close()

    def test_font_required(self):
        with self.assertRaises(BackendUnavailable):
            self.be.load_fonts(None, (16,))

    def test_input_mapping(self):
        s = self.sdl2
        self.ev(type=s.SDL_QUIT)
        self.ev(type=s.SDL_KEYDOWN, key=NS(keysym=NS(sym=s.SDLK_RETURN)))
        self.ev(type=s.SDL_TEXTINPUT, text=NS(text=b"a"))
        self.ev(type=s.SDL_CONTROLLERBUTTONDOWN, cbutton=NS(button=s.SDL_CONTROLLER_BUTTON_A))
        self.ev(type=s.SDL_CONTROLLERBUTTONDOWN, cbutton=NS(button=s.SDL_CONTROLLER_BUTTON_DPAD_UP))
        self.ev(type=s.SDL_CONTROLLERAXISMOTION, caxis=NS(axis=s.SDL_CONTROLLER_AXIS_LEFTX, value=32767))
        self.ev(type=s.SDL_CONTROLLERAXISMOTION, caxis=NS(axis=s.SDL_CONTROLLER_AXIS_LEFTX, value=32767))
        self.ev(type=s.SDL_CONTROLLERAXISMOTION, caxis=NS(axis=s.SDL_CONTROLLER_AXIS_LEFTX, value=0))
        self.assertEqual(self.be.poll(), ["quit", "ok", "text:a", "ok", "up", "right"])

    def test_swap_ab_only_for_pad(self):
        s = self.sdl2
        self.be.swap_ab = True
        self.ev(type=s.SDL_CONTROLLERBUTTONDOWN, cbutton=NS(button=s.SDL_CONTROLLER_BUTTON_A))
        self.ev(type=s.SDL_CONTROLLERBUTTONDOWN, cbutton=NS(button=s.SDL_CONTROLLER_BUTTON_B))
        self.ev(type=s.SDL_KEYDOWN, key=NS(keysym=NS(sym=s.SDLK_RETURN)))
        self.assertEqual(self.be.poll(), ["back", "ok", "ok"])

    def test_select_start_combo_quits(self):
        s = self.sdl2
        B = s.SDL_CONTROLLER_BUTTON_BACK
        S = s.SDL_CONTROLLER_BUTTON_START
        self.ev(type=s.SDL_CONTROLLERBUTTONDOWN, cbutton=NS(button=B))
        self.ev(type=s.SDL_CONTROLLERBUTTONDOWN, cbutton=NS(button=S))
        self.assertEqual(self.be.poll(), ["select", "quit"], "Start при удержании Select не даёт действия «start»")
        # отпустили обе — снова обычные кнопки
        self.ev(type=s.SDL_CONTROLLERBUTTONUP, cbutton=NS(button=B))
        self.ev(type=s.SDL_CONTROLLERBUTTONUP, cbutton=NS(button=S))
        self.ev(type=s.SDL_CONTROLLERBUTTONDOWN, cbutton=NS(button=S))
        self.assertEqual(self.be.poll(), ["start"])
        # Start уже удерживается, затем Select — тоже выход; одиночное нажатие после отпускания не выходит
        self.ev(type=s.SDL_CONTROLLERBUTTONDOWN, cbutton=NS(button=B))
        self.assertEqual(self.be.poll(), ["quit"])
        self.ev(type=s.SDL_CONTROLLERBUTTONUP, cbutton=NS(button=B))
        self.ev(type=s.SDL_CONTROLLERBUTTONUP, cbutton=NS(button=S))
        self.ev(type=s.SDL_CONTROLLERBUTTONDOWN, cbutton=NS(button=B))
        self.assertEqual(self.be.poll(), ["select"])

    def test_select_start_combo_raw_joystick(self):
        s = self.sdl2
        self.be._raw[5] = object()
        self.ev(type=s.SDL_JOYBUTTONDOWN, jbutton=NS(which=5, button=6))      # select
        self.ev(type=s.SDL_JOYBUTTONDOWN, jbutton=NS(which=5, button=7))      # start
        self.assertEqual(self.be.poll(), ["select", "quit"])

    def test_raw_joystick_fallback_ignores_foreign_ids(self):
        s = self.sdl2
        self.be._raw[5] = object()
        self.ev(type=s.SDL_JOYBUTTONDOWN, jbutton=NS(which=5, button=0))
        self.ev(type=s.SDL_JOYBUTTONDOWN, jbutton=NS(which=9, button=0))   # уже обработан как GameController
        self.ev(type=s.SDL_JOYHATMOTION, jhat=NS(which=5, value=s.SDL_HAT_UP))
        self.assertEqual(self.be.poll(), ["ok", "up"])

    def test_factory_auto_falls_back_to_sdl2(self):
        be = create_backend("auto", "t", True)       # pygame в песочнице нет -> sdl2
        self.assertEqual(be.name, "sdl2")
        with self.assertRaises(BackendUnavailable):
            create_backend("nope", "t", True)

    def test_gui_runs_on_sdl2_backend(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        store = SettingsStore(tmp)
        g = G.Gui(store.load(), store, PROFILES, self.be, str(tmp / "root"), "f.ttf")
        for scr in (g.top, G.SettingsScreen(), G.PathsScreen(), G.TextInput("t", "abc", lambda v: None)):
            g.stack = [g.stack[0], scr]
            g.step()
        self.assertTrue(any(k == "copy" for k, _ in self.sdl2.log))


class TestFactoryWithoutBackends(unittest.TestCase):
    def test_clear_error_when_nothing_available(self):
        with mock.patch.dict(sys.modules, {"pygame": None, "sdl2": None, "sdl2.sdlttf": None}):
            sys.modules.pop("pm_installer.backends.pygame_backend", None)
            sys.modules.pop("pm_installer.backends.sdl2_backend", None)
            with self.assertRaises(BackendUnavailable) as cm:
                create_backend("auto", "t", True)
        self.assertIn("pygame", str(cm.exception))
        self.assertIn("sdl2", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
