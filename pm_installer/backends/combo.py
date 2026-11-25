"""Выход по комбинации Select+Start (удерживаются одновременно) — общий для обоих бэкендов."""
from typing import List, Set


class ExitCombo:
    COMBO = frozenset(("select", "start"))

    def __init__(self):
        self.held: Set[str] = set()

    def press(self, action: str, mapped: str) -> List[str]:
        """Нажатие кнопки: либо её действие, либо «quit», если вторая кнопка комбинации уже удерживается."""
        if action in self.COMBO:
            self.held.add(action)
            if self.held >= self.COMBO:
                self.held.clear()
                return ["quit"]
        return [mapped]

    def release(self, action: str) -> None:
        self.held.discard(action)
