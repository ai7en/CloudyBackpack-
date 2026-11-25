#!/bin/bash
# Cloud Port Installer — порт PortMaster: ставит порты из облачных архивов.
# Шаблон запуска — стандартный для PortMaster; Python берётся из рантайма python_3.11.

XDG_DATA_HOME=${XDG_DATA_HOME:-$HOME/.local/share}

if [ -d "/opt/system/Tools/PortMaster/" ]; then
  controlfolder="/opt/system/Tools/PortMaster"
elif [ -d "/opt/tools/PortMaster/" ]; then
  controlfolder="/opt/tools/PortMaster"
elif [ -d "$XDG_DATA_HOME/PortMaster/" ]; then
  controlfolder="$XDG_DATA_HOME/PortMaster"
else
  controlfolder="/roms/ports/PortMaster"
fi

source $controlfolder/control.txt
[ -f "${controlfolder}/mod_${CFW_NAME}.txt" ] && source "${controlfolder}/mod_${CFW_NAME}.txt"

get_controls

GAMEDIR="/$directory/ports/cloudportinstaller"
# Запасной вариант: папка порта лежит рядом со скриптом
[ -d "$GAMEDIR" ] || GAMEDIR="$(cd "$(dirname "$0")" && pwd)/cloudportinstaller"
cd "$GAMEDIR" || exit 1
mkdir -p "$GAMEDIR/data"

> "$GAMEDIR/log.txt" && exec > >(tee "$GAMEDIR/log.txt") 2>&1

# ---- Python (рантайм PortMaster)
python_runtime="python_3.11"
export PYTHONHOME="${PM_PYHOME:-/tmp/python}"

if [ ! -f "$controlfolder/libs/${python_runtime}.squashfs" ]; then
  if [ ! -f "$controlfolder/harbourmaster" ]; then
    pm_message "This port requires the latest PortMaster to run, please go to https://portmaster.games/ for more info."
    sleep 5
    exit 1
  fi
  $ESUDO $controlfolder/harbourmaster --quiet --no-check runtime_check "${python_runtime}.squashfs"
fi

PYSQ="$controlfolder/libs/${python_runtime}.squashfs"
MOUNTED_BY_US=0

# Ищет уже смонтированный рантайм python (остался от прерванного запуска) и возвращает его папку.
find_mounted_python() {
  local mp
  for mp in $(awk '$3=="squashfs"{print $2}' "${PM_MOUNTS:-/proc/mounts}" 2>/dev/null); do
    if [ -x "$mp/bin/python3" ] && [ -d "$mp/lib/python3.11" ]; then echo "$mp"; return 0; fi
  done
  return 1
}

mount_python() {
  local mp dev
  $ESUDO mkdir -p "${PYTHONHOME}"
  $ESUDO mount -o loop "$PYSQ" "${PYTHONHOME}"
  if [ -x "${PYTHONHOME}/bin/python3" ]; then MOUNTED_BY_US=1; return 0; fi
  echo "WARN: не удалось смонтировать python в ${PYTHONHOME}, пробую восстановиться"
  # 1) рантайм уже смонтирован где-то (занятый loop-диск от прошлого запуска) — используем его
  if mp=$(find_mounted_python); then
    echo "Использую уже смонтированный python: $mp"; export PYTHONHOME="$mp"; return 0
  fi
  # 2) освобождаем «зависшие» loop-устройства этого образа и монтируем заново
  $ESUDO umount -l "${PYTHONHOME}" 2>/dev/null
  if command -v losetup >/dev/null 2>&1; then
    for dev in $($ESUDO losetup -j "$PYSQ" 2>/dev/null | cut -d: -f1); do
      $ESUDO umount -l "$dev" 2>/dev/null
      $ESUDO losetup -d "$dev" 2>/dev/null
    done
  fi
  $ESUDO mount -o loop "$PYSQ" "${PYTHONHOME}" || $ESUDO mount -t squashfs -o ro,loop "$PYSQ" "${PYTHONHOME}"
  if [ -x "${PYTHONHOME}/bin/python3" ]; then MOUNTED_BY_US=1; return 0; fi
  return 1
}

if [[ "$PM_CAN_MOUNT" != "N" ]]; then
  $ESUDO umount "${PYTHONHOME}"
fi

if ! mount_python; then
  echo "ОШИБКА: рантайм python не смонтирован. Перезагрузите консоль и запустите снова."
  pm_message "Python runtime could not be mounted. Reboot the console and try again. See log.txt in the port folder."
  sleep 5
  pm_finish
  exit 1
fi
export PATH="${PYTHONHOME}/bin:$PATH"
PYTHON_CMD="${PYTHONHOME}/bin/python3"
export LD_LIBRARY_PATH="${PYTHONHOME}/lib:${LD_LIBRARY_PATH}"

# ---- Окружение: PySDL2 и requests лежат в pylibs самого PortMaster
export PYTHONPATH="$GAMEDIR:$controlfolder/pylibs:$controlfolder/exlibs${PYTHONPATH:+:$PYTHONPATH}"
export PYTHONDONTWRITEBYTECODE=1
export SDL_GAMECONTROLLERCONFIG="$sdl_controllerconfig"
export PM_INSTALLER_HOME="$GAMEDIR/data"
export CFW_NAME

echo "ssl: $($PYTHON_CMD -c 'import ssl;print(ssl.OPENSSL_VERSION)' 2>&1 | tail -1) curl: $(command -v curl || echo нет)"
echo "controlfolder=$controlfolder GAMEDIR=$GAMEDIR CFW_NAME=$CFW_NAME python=$($PYTHON_CMD --version 2>&1)"

$PYTHON_CMD main.py "$@"

if [[ "$PM_CAN_MOUNT" != "N" && "$MOUNTED_BY_US" == "1" ]]; then
  $ESUDO umount "${PYTHONHOME}"
fi

pm_finish
