# Cloud Port Installer — порт PortMaster для Anbernic (H700, ARM64)

Скачивает архив порта (.zip / .7z / .rar) по публичной ссылке из облака, распаковывает
и раскладывает по путям текущей прошивки (muOS, ROCKNIX, KNULLI): папка порта — в одно место,
`.sh` — в другое (на muOS пути разные), `chmod +x` на `.sh`.

Приложение оформлено как **порт PortMaster**: запускается из меню Ports, Python берёт из рантайма
`python_3.11`, GUI рисует через PySDL2 из `pylibs` самого PortMaster (pygame не нужен).

## Установка на консоль

1. `python3 build_port.py` → `dist/cloudportinstaller.zip` (готовый архив уже лежит рядом).
2. Распакуйте архив на SD-карту консоли: `Cloud Port Installer.sh` и папку `cloudportinstaller/`
   в папки портов вашей прошивки (то же, что делает сам установщик):

| Прошивка | Папка `cloudportinstaller/` | Файл `Cloud Port Installer.sh` |
|---|---|---|
| muOS | `/mmc/ports/` (`/mnt/mmc/ports/`) | `/mnt/mmc/ROMS/Ports/` |
| ROCKNIX | `/roms/ports/` | `/roms/ports/` |
| KNULLI | `/userdata/roms/ports/` | `/userdata/roms/ports/` |

   Альтернатива — папка `autoinstall` PortMaster (muOS: `/mmc/MUOS/PortMaster/autoinstall/`, ROCKNIX:
   `/roms/ports/PortMaster/autoinstall/`, KNULLI: `/userdata/system/.local/share/PortMaster/autoinstall/`),
   затем открыть PortMaster. Для сторонних zip этот путь не проверялся — при сбое используйте ручную распаковку.
3. Обновите список игр / перезапустите интерфейс — в разделе Ports появится «Cloud Port Installer».
4. Нужен установленный PortMaster, хотя бы раз запущенный (он распаковывает `pylibs`), и рантайм
   `python_3.11` (PortMaster докачает его сам при запуске порта — нужен интернет).

Если пишет про сеть — откройте в меню **«Проверка сети»**: она показывает дату/время, сертификаты и по шагам
DNS → соединение → HTTPS для Яндекс.Диска, Google Drive и вашей ссылки. Частые причины: Wi-Fi не подключён
или неверная дата на консоли (из-за неё HTTPS не проходит).

Если что-то не запустилось — пришлите `cloudportinstaller/log.txt`.

## Структура проекта

```
portmaster-installer/
├── main.py                    # точка входа: GUI или --cli, --detect, --archive
├── build_port.py              # сборка dist/cloudportinstaller.zip (формат порта PortMaster)
├── run.sh                     # запуск на ПК
├── port/                      # «Cloud Port Installer.sh», port.json, gameinfo.xml, README порта
├── assets/DejaVuSans.ttf      # шрифт с кириллицей (лицензия — в licenses/)
├── requirements.txt
├── config/
│   └── paths.json             # СЛОВАРЬ ПУТЕЙ по прошивкам (не править — см. paths.user.json)
├── pm_installer/
│   ├── settings.py            # Settings, SettingsStore (settings.json), app_home()
│   ├── logger.py              # setup_logging() — файл с ротацией: data/logs/installer.log
│   ├── errors.py              # иерархия InstallerError + wrap_oserror()
│   ├── constants.py
│   ├── firmware.py            # FirmwareProfile, load_profiles(), detect(), resolve_targets()
│   ├── archives.py            # detect_format() по сигнатуре, extract(): zip / py7zr / rarfile + CLI-резерв
│   ├── layout.py              # analyze(): ищет .sh и папку порта → PortLayout
│   ├── installer.py           # PortInstaller, ConflictAction, InstallReport
│   ├── sevenzip.py            # распаковка .7z на чистом Python (stdlib lzma/zlib/bz2), потоково, с проверкой CRC
│   ├── extract_common.py      # ExtractProgress, ограничитель частоты, safe_target (защита от zip-slip)
│   ├── manager.py             # менеджер установленных портов: scan(), compute_size(), delete_port()
│   ├── httpfallback.py        # CurlSession: HTTPS через curl, если в Python нет модуля ssl
│   ├── diagnostics.py         # describe_network_error(), run_checks() — экран «Проверка сети»
│   ├── gui.py                 # Gui и экраны (MainMenu, CloudsScreen, CloudEditScreen, BrowseScreen, ManagerScreen, Confirm, SettingsScreen, TextInput, InstallScreen, NetCheckScreen, PathsScreen)
│   ├── backends/              # pygame_backend.py (ПК), sdl2_backend.py (PySDL2 — как у PortMaster)
│   └── clouds/
│       ├── __init__.py        # @register, get_provider(), available_providers()
│       ├── base.py            # CloudProvider (ABC), RemoteFile, общая загрузка с .part и отменой
│       ├── yandex.py          # YandexDisk — cloud-api.yandex.net/v1/disk/public/resources[/download]
│       ├── gdrive.py          # GoogleDrive — file/d/<ID> напрямую, папки через gdown
│       ├── mailru.py          # MailRuCloud — публичная ссылка на файл
│       └── direct.py          # DirectLink — любая http(s)-ссылка (запасной вариант)
└── tests/                     # test_core.py, test_gui.py, test_port.py, test_diagnostics.py — 144 проверки
```

## Ключевые классы

| Класс / функция | Роль |
|---|---|
| `CloudProvider` | `matches(url)`, `list_files(url) -> [RemoteFile]`, `open_stream(remote)`; `download()` общий |
| `FirmwareProfile` | ключ, подпись, кандидаты `port_dirs` / `script_dirs`, признаки для автоопределения |
| `detect(profiles, root)` | по `/etc/os-release` и маркерным путям; при ничьей возвращает `None` (тогда выбор вручную) |
| `resolve_targets()` | существующий путь → иначе путь с существующим родителем → создаёт (`mkdir 0755`, `sudo -n` только для mkdir) |
| `extract()` | формат по сигнатуре; защита от zip-slip; сохраняет права `+x` из zip; пароль → понятная ошибка |
| `analyze()` | `.sh` в корне (снимает папку-обёртку), папка порта по имени `.sh`, иначе самая «тяжёлая» |
| `PortInstaller` | `list_remote()`, `install(url)`, `install_archive(path)`; конфликт: перезапись / пропуск / бэкап / вопрос |

## Примеры конфигурации путей (`config/paths.json`)

```json
{
  "firmwares": {
    "muos":    { "label": "muOS",
                 "detect": {"os_release": ["muos","mustard"], "markers": ["/opt/muos","/mnt/mmc/MUOS"]},
                 "port_dirs":   ["/mnt/mmc/ports", "/mmc/ports"],
                 "script_dirs": ["/mnt/mmc/ROMS/Ports"] },
    "rocknix": { "label": "ROCKNIX",
                 "detect": {"os_release": ["rocknix"], "markers": ["/storage/.config"]},
                 "port_dirs":   ["/roms/ports"],
                 "script_dirs": ["/roms/ports"] },
    "knulli":  { "label": "KNULLI",
                 "detect": {"os_release": ["knulli","batocera"], "markers": ["/userdata/system"]},
                 "port_dirs":   ["/userdata/roms/ports", "/SHARE/roms/ports"],
                 "script_dirs": ["/userdata/roms/ports", "/SHARE/roms/ports"] }
  }
}
```

### Новая прошивка или устройство — без правки кода

Создайте `data/paths.user.json` (та же схема; профили с тем же ключом дополняют/переопределяют встроенные):

```json
{
  "firmwares": {
    "muos_sd2": { "extends": "muos", "label": "muOS (SD2)",
                  "port_dirs":   ["/mnt/sdcard/ports"],
                  "script_dirs": ["/mnt/sdcard/ROMS/Ports"] }
  }
}
```

`extends` наследует подпись и пути (но не `detect` — такой профиль выбирается вручную).

### Новое облако

Подкласс `CloudProvider` с `@register` в `pm_installer/clouds/` и импорт в `clouds/__init__.py`
(`direct` должен оставаться последним). Образец минимального провайдера — `direct.py`.

## Настройки (`data/settings.json`)

```json
{
  "clouds": [
    {"name": "Мои порты", "url": "https://disk.yandex.ru/d/XXXXXXXX", "cloud": "auto"}
  ],
  "firmware": "auto",
  "temp_dir": "",
  "conflict_policy": "ask"
}
```

`clouds[].cloud`: `auto | yandex | gdrive | mailru | direct` (облака добавляются в меню «Облака»; старые `url`/`cloud` переносятся в список сами); `firmware`: `auto | muos | rocknix | knulli | …ваши`;
`conflict_policy`: `ask | overwrite | skip | backup`. Пустой `temp_dir` = `data/tmp` (на SD-карте:
`/tmp` на консолях обычно в RAM и не вместит большой порт). Каталог данных меняется через `PM_INSTALLER_HOME`.
Журнал: `data/logs/installer.log`.

## Запуск

```sh
pip install -r requirements.txt          # только для запуска на ПК; на консоли всё берётся из PortMaster
./run.sh                                  # GUI (на ПК нужен pygame или PySDL2)
./run.sh --cli --url "https://yadi.sk/d/XXXX" --firmware muos --conflict backup
./run.sh --cli --archive /path/port.zip   # из локального архива
./run.sh --detect                         # какая прошивка определена, какие пути
```

Для проверки без устройства: `--root /tmp/fakeroot` (все пути берутся внутри этого корня).
Тесты: `python3 -I -m unittest discover -s tests -v`.

Если путь не создаётся из-за прав, для `mkdir` пробуется `sudo -n` (без пароля), после чего
папка передаётся текущему пользователю. Всё остальное делается без root.

## Что проверено, а что нет

Проверено в песочнице (144 теста): раскладка по трём прошивкам, конфликты (4 режима), автоопределение
(включая подсказку `CFW_NAME` от PortMaster), пользовательские профили, безопасность zip, загрузка по http
с отменой/404/обрывом, разбор ссылок и ответов трёх облаков **на подставных ответах**, логика GUI на
фиктивном бэкенде, PySDL2-бэкенд на заглушке модуля `sdl2`, скрипт запуска целиком с подделанным
PortMaster (порядок: `control.txt` → `get_controls` → mount рантайма → запуск → umount → `pm_finish`),
сборка пакета и установка этого пакета самим установщиком на все три прошивки.

**Не проверено** (нет устройства, нет доступа к облакам, нет pygame/PySDL2 в песочнице):
- запуск на реальной консоли; соответствие скрипта запуска вашей версии PortMaster (структура взята из
  документации и открытого порта на Python, но не прогонялась на железе);
- что рантайм `python_3.11` и `pylibs` PortMaster содержат нужное: PySDL2, SDL2_ttf (для текста) и `requests`;
  вызовы PySDL2 написаны по документации и проверены только на заглушке;
- реальные запросы к Яндекс.Диску, Google Drive и Mail.ru (Mail.ru — разбор страницы, может сломаться;
  папки Mail.ru и WebDAV не поддерживаются);
- `.7z`: встроенный распаковщик на стандартном `lzma` (LZMA/LZMA2, BCJ x86/ARM/ARM64/…, Deflate, BZip2, Copy,
  проверка CRC) проверен на архивах, собранных по спецификации формата, и сверен с эталонным liblzma для ARM64;
  на архивах, созданных настоящим 7-Zip, и на самой консоли — не проверен (нужен модуль `lzma` в рантайме Python;
  не поддерживаются PPMd, BCJ2, шифрование — для них пробуются py7zr и утилиты 7z/bsdtar, если они есть);
  `.rar` работает только если на консоли есть `unrar` / `bsdtar` / `7z`; `.zip` работает всегда;
- какая кнопка у вас «A» — для этого есть переключатель «Кнопки A/B» в настройках;
- автоопределение прошивки по `/etc/os-release` — эвристика; при запуске из PortMaster основным сигналом служит
  `CFW_NAME`, а если не сработало — выберите прошивку вручную.
