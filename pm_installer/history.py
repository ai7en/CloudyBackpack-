"""История установок (data/installed.json): из какого облака и файла поставлен порт, чтобы видеть обновления."""
import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional

log = logging.getLogger("pm_installer.history")


def _norm(name: str) -> str:
    n = (name or "").strip().lower()
    return n[:-3] if n.endswith(".sh") else n


def display_name(rf) -> str:
    """Имя файла в облаке так, как его видит пользователь (с подпапкой, если есть)."""
    return rf.meta.get("display") or rf.name or ""


class History:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.RLock()

    # ---- хранение
    def _load(self) -> List[dict]:
        try:
            data = json.loads(self.path.read_text("utf-8"))
            items = data.get("entries", []) if isinstance(data, dict) else []
            return [e for e in items if isinstance(e, dict) and e.get("url")]
        except FileNotFoundError:
            return []
        except (OSError, ValueError) as e:
            log.warning("История установок повреждена (%s) — начинаю заново", e)
            return []

    def _save(self, entries: List[dict]) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(self.path.name + ".tmp")
            tmp.write_text(json.dumps({"entries": entries}, ensure_ascii=False, indent=1), "utf-8")
            os.replace(tmp, self.path)
        except OSError as e:
            log.warning("Не удалось сохранить историю: %s", e)      # история — удобство, не причина падать

    # ---- запись
    def record(self, source: Optional[dict], rf, names: List[str]) -> None:
        """Запоминает установленный файл облака и имена поставленных скриптов/папок."""
        if not source or not names:
            return
        entry = {"url": source.get("url", ""), "cloud": source.get("name") or "", "file": display_name(rf),
                 "size": rf.size, "md5": rf.meta.get("md5"), "modified": rf.meta.get("modified"),
                 "date": time.strftime("%Y-%m-%d %H:%M"), "ports": sorted(set(names))}
        with self._lock:
            entries = [e for e in self._load() if (e["url"], e.get("file", "")) != (entry["url"], entry["file"])]
            mine = {_norm(n) for n in names}
            for e in entries:      # порт теперь принадлежит новой записи
                e["ports"] = [p for p in e.get("ports", []) if _norm(p) not in mine]
            entries = [e for e in entries if e.get("ports")]
            entries.append(entry)
            self._save(entries)

    def forget_port(self, names: List[str]) -> None:
        gone = {_norm(n) for n in names if n}
        with self._lock:
            entries = self._load()
            for e in entries:
                e["ports"] = [p for p in e.get("ports", []) if _norm(p) not in gone]
            self._save([e for e in entries if e["ports"]])

    # ---- чтение
    def status(self, url: str, rf) -> str:
        """new — не устанавливали, same — актуален, changed — в облаке файл изменился."""
        key = (url, display_name(rf))
        with self._lock:
            old = next((e for e in self._load() if (e["url"], e.get("file", "")) == key), None)
        if old is None:
            return "new"
        if old.get("md5") and rf.meta.get("md5"):
            return "same" if old["md5"] == rf.meta["md5"] else "changed"
        if old.get("size") and rf.size and old["size"] != rf.size:
            return "changed"
        if old.get("modified") and rf.meta.get("modified") and old["modified"] != rf.meta["modified"]:
            return "changed"
        return "same"

    def source_of(self, names: List[str]) -> Optional[Dict]:
        want = {_norm(n) for n in names if n}
        with self._lock:
            for e in reversed(self._load()):
                if want & {_norm(p) for p in e.get("ports", [])}:
                    return e
        return None
