#!/usr/bin/env python3
"""Точка входа: GUI (pygame) по умолчанию, --cli — консольный режим."""
import argparse
import os
import sys
from pathlib import Path

try:
    import requests  # noqa: F401  (в PortMaster лежит в pylibs)
except ImportError:
    sys.exit("Нужен модуль requests: pip install requests (на консоли он есть в PortMaster/pylibs).")

from pm_installer.backends import BackendUnavailable, create_backend
from pm_installer.clouds import available_providers
from pm_installer.constants import APP_NAME
from pm_installer.errors import CancelledError, InstallerError
from pm_installer.firmware import describe_targets, detect, env_cfw_name, load_profiles
from pm_installer.installer import ConflictAction, PortInstaller
from pm_installer.logger import setup_logging
from pm_installer.settings import CONFLICT_POLICIES, SettingsStore

BASE = Path(__file__).resolve().parent


def cli_resolver(path: Path, kind: str) -> ConflictAction:
    while True:
        a = input(f"«{path.name}» уже существует ({kind}). [o]перезаписать / [s]пропустить / "
                  "[b]копия / [a]прервать: ").strip().lower()
        for ch, act in (("o", ConflictAction.OVERWRITE), ("s", ConflictAction.SKIP),
                        ("b", ConflictAction.BACKUP), ("a", ConflictAction.ABORT)):
            if a.startswith(ch):
                return act


def cli_progress(stage: str, frac: float, msg: str) -> None:
    pct = f" {frac * 100:3.0f}%" if frac >= 0 else ""
    print(f"\r[{stage}]{pct} {msg}".ljust(78), end="\n" if frac >= 1 else "", flush=True)


def cli_choose(files):
    for i, f in enumerate(files, 1):
        print(f"  {i}. {f.name or '(без имени)'}")
    ans = input("Номер архива (или 'all'): ").strip().lower()
    if ans == "all":
        return files
    try:
        return [files[int(ans) - 1]]
    except (ValueError, IndexError):
        raise InstallerError("Неверный выбор архива.")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Установка портов PortMaster из облака (Anbernic H700)")
    ap.add_argument("--cli", action="store_true", help="консольный режим без GUI")
    ap.add_argument("--url", help="ссылка на облако (вместо сохранённой в настройках)")
    ap.add_argument("--archive", help="установить из локального архива, минуя облако")
    ap.add_argument("--firmware", help="auto | muos | rocknix | knulli | ... (на этот запуск)")
    ap.add_argument("--cloud", help="auto | " + " | ".join(k for k, _ in available_providers()))
    ap.add_argument("--conflict", choices=CONFLICT_POLICIES, help="поведение при конфликте имён")
    ap.add_argument("--root", default=os.environ.get("PM_ROOT", ""),
                    help="альтернативный корень ФС (для проверки без устройства)")
    ap.add_argument("--detect", action="store_true", help="показать определённую прошивку и пути")
    args = ap.parse_args(argv)

    store = SettingsStore()
    setup_logging(store.log_dir, console=args.cli)
    settings = store.load()
    for attr, val in (("url", args.url), ("firmware", args.firmware), ("cloud", args.cloud),
                      ("conflict_policy", args.conflict)):
        if val:
            setattr(settings, attr, val)

    try:
        profiles = load_profiles(BASE / "config" / "paths.json", store.home / "paths.user.json")
    except InstallerError as e:
        print(f"Ошибка: {e}", file=sys.stderr)
        return 1

    if args.detect:
        key = detect(profiles, args.root, env_cfw_name(args.root))
        print("Определена прошивка:", profiles[key].label if key else "не удалось определить")
        for k in ([key] if key else profiles):
            for kind, path, exists, writable in describe_targets(profiles[k], args.root):
                print(f"  [{k}] {kind}: {path}  {'есть' if exists else 'нет'}{', запись ок' if writable else ''}")
        return 0

    if not args.cli and not args.archive:
        from pm_installer.gui import Gui, find_font
        try:
            backend = create_backend(os.environ.get("PM_BACKEND", "auto"), APP_NAME,
                                     fullscreen=not os.environ.get("PM_WINDOWED"),
                                     joy_map_path=store.home / "joy_map.json")
            Gui(settings, store, profiles, backend, args.root, find_font()).run()
            return 0
        except BackendUnavailable as e:
            print(f"Ошибка: {e}", file=sys.stderr)
            if not sys.stdin.isatty():
                return 1
            print("Переключаюсь на консольный режим.", file=sys.stderr)

    inst = PortInstaller(settings, profiles, root=args.root, resolver=cli_resolver,
                         progress=cli_progress, temp_default=store.default_temp)
    try:
        if args.archive:
            reports = [inst.install_archive(Path(args.archive))]
        else:
            if not settings.url:
                settings.url = input("Ссылка на облачную папку/файл: ").strip()
                store.save(settings)
            reports = inst.install(choose=cli_choose)
    except CancelledError:
        print("\nОтменено.")
        return 1
    except InstallerError as e:
        print(f"\nОшибка: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nПрервано.")
        return 130
    print()
    for r in reports:
        print("\n".join(r.summary()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
