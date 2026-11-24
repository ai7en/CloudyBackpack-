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
export PYTHONHOME="/tmp/python"

if [ ! -f "$controlfolder/libs/${python_runtime}.squashfs" ]; then
  if [ ! -f "$controlfolder/harbourmaster" ]; then
    pm_message "This port requires the latest PortMaster to run, please go to https://portmaster.games/ for more info."
    sleep 5
    exit 1
  fi
  $ESUDO $controlfolder/harbourmaster --quiet --no-check runtime_check "${python_runtime}.squashfs"
fi

if [[ "$PM_CAN_MOUNT" != "N" ]]; then
  $ESUDO umount "${PYTHONHOME}"
fi
$ESUDO mkdir -p "${PYTHONHOME}"
$ESUDO mount -o loop "$controlfolder/libs/${python_runtime}.squashfs" "${PYTHONHOME}"
export PATH="${PYTHONHOME}/bin:$PATH"

if command -v python3 &>/dev/null; then
  PYTHON_CMD=python3
elif command -v python &>/dev/null; then
  PYTHON_CMD=python
else
  pm_message "Python runtime could not be started. See log.txt in the port folder."
  sleep 5
  exit 1
fi

# ---- Окружение: PySDL2 и requests лежат в pylibs самого PortMaster
export PYTHONPATH="$GAMEDIR:$controlfolder/pylibs:$controlfolder/exlibs${PYTHONPATH:+:$PYTHONPATH}"
export PYTHONDONTWRITEBYTECODE=1
export SDL_GAMECONTROLLERCONFIG="$sdl_controllerconfig"
export PM_INSTALLER_HOME="$GAMEDIR/data"
export CFW_NAME

echo "controlfolder=$controlfolder GAMEDIR=$GAMEDIR CFW_NAME=$CFW_NAME python=$($PYTHON_CMD --version 2>&1)"

$PYTHON_CMD main.py "$@"

if [[ "$PM_CAN_MOUNT" != "N" ]]; then
  $ESUDO umount "${PYTHONHOME}"
fi

pm_finish
