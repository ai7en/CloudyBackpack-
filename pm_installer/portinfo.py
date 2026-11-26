"""Сведения о порте из port.json и проверка нужного рантайма PortMaster."""
import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

log = logging.getLogger(__name__)

CONTROL_CANDIDATES = ("/opt/system/Tools/PortMaster", "/opt/tools/PortMaster",
                      "~/.local/share/PortMaster", "/roms/ports/PortMaster",
                      "/mnt/mmc/MUOS/PortMaster", "/mnt/sdcard/MUOS/PortMaster",
                      "/userdata/roms/ports/PortMaster")


@dataclass
class PortInfo:
    title: str = ""
    desc: str = ""
    porter: str = ""
    runtime: str = ""
    reqs: List[str] = field(default_factory=list)
    genres: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    def lines(self) -> List[str]:
        out = []
        if self.title:
            out.append(f"Порт: {self.title}")
        if self.desc:
            out.append(self.desc[:200])
        if self.porter:
            out.append(f"Портировал: {self.porter}")
        if self.runtime:
            out.append(f"Рантайм: {self.runtime}")
        if self.reqs:
            out.append("Требуется: " + ", ".join(self.reqs))
        out += [f"Внимание: {w}" for w in self.warnings]
        return out


def _as_list(v) -> List[str]:
    if isinstance(v, str):
        return [v] if v else []
    return [str(x) for x in v] if isinstance(v, (list, tuple)) else []


def find_port_json(root: Path, depth: int = 2) -> Optional[Path]:
    root = Path(root)
    for d in range(depth + 1):
        pat = "/".join(["*"] * d + ["port.json"])
        for p in sorted(root.glob(pat)):
            if p.is_file():
                return p
    return None


def control_folder(env=None) -> Optional[Path]:
    env = os.environ if env is None else env
    cands = [env.get("PM_CONTROLFOLDER", "")] + list(CONTROL_CANDIDATES)
    for c in cands:
        if c and Path(os.path.expanduser(c)).is_dir():
            return Path(os.path.expanduser(c))
    return None


def runtime_present(runtime: str, ctl: Optional[Path]) -> Optional[bool]:
    """True/False, либо None, если папку PortMaster найти не удалось."""
    if ctl is None:
        return None
    libs = ctl / "libs"
    return any((libs / (runtime + ext)).exists() for ext in ("", ".squashfs"))


def read(work: Path, ctl: Optional[Path] = None) -> Optional[PortInfo]:
    p = find_port_json(work)
    if p is None:
        return None
    try:
        data = json.loads(p.read_text("utf-8", errors="replace"))
    except (OSError, ValueError) as e:
        log.warning("port.json не разобран: %s", e)
        return None
    if not isinstance(data, dict):
        return None
    attr = data.get("attr") if isinstance(data.get("attr"), dict) else {}
    rt = attr.get("runtime")
    info = PortInfo(title=str(attr.get("title") or data.get("name") or ""),
                    desc=str(attr.get("desc") or "").replace("\n", " "),
                    porter=", ".join(_as_list(attr.get("porter"))),
                    runtime=str(rt) if rt else "",
                    reqs=_as_list(attr.get("reqs")), genres=_as_list(attr.get("genres")))
    if info.runtime:
        ok = runtime_present(info.runtime, ctl if ctl is not None else control_folder())
        if ok is False:
            info.warnings.append(f"не найден рантайм «{info.runtime}» — установите его через PortMaster, "
                                 f"иначе порт не запустится")
    return info
