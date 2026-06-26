"""
Detection des gestes a partir des fronts d'entree.

Reproduit la logique des classes de base Calaos :
  - InputSwitchLongPress : front montant -> timer 500 ms.
      relachement avant 500 ms -> "single"
      maintien >= 500 ms        -> "long"
  - InputSwitchTriple : premier front montant -> timer 500 ms, compteur a 0.
      chaque front montant incremente le compteur.
      a l'expiration : 1 -> "single", 2 -> "double", >=3 -> "triple"

Le geste detecte est remonte via un callback (utilise pour publier un
evenement Home Assistant).
"""
from __future__ import annotations

import asyncio
from typing import Awaitable, Callable

GestureCallback = Callable[[str], Awaitable[None] | None]

LONG_PRESS_S = 0.5
MULTI_CLICK_S = 0.5


def _emit(cb: GestureCallback, gesture: str) -> None:
    res = cb(gesture)
    if asyncio.iscoroutine(res):
        asyncio.ensure_future(res)


class LongPressDetector:
    """single / long."""

    def __init__(self, callback: GestureCallback) -> None:
        self._cb = callback
        self._timer: asyncio.TimerHandle | None = None
        self._held = False

    def feed(self, raw: bool) -> None:
        loop = asyncio.get_running_loop()
        if raw:
            if self._timer is None:
                self._held = True
                self._timer = loop.call_later(LONG_PRESS_S, self._on_long)
        else:
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
                self._held = False
                _emit(self._cb, "single")

    def _on_long(self) -> None:
        self._timer = None
        if self._held:
            self._held = False
            _emit(self._cb, "long")


class MultiClickDetector:
    """single / double / triple."""

    def __init__(self, callback: GestureCallback) -> None:
        self._cb = callback
        self._timer: asyncio.TimerHandle | None = None
        self._count = 0

    def feed(self, raw: bool) -> None:
        loop = asyncio.get_running_loop()
        if raw:  # on ne compte que les fronts montants
            if self._timer is None:
                self._count = 0
                self._timer = loop.call_later(MULTI_CLICK_S, self._on_done)
            self._count += 1

    def _on_done(self) -> None:
        self._timer = None
        if self._count <= 0:
            return
        if self._count == 1:
            gesture = "single"
        elif self._count == 2:
            gesture = "double"
        else:
            gesture = "triple"
        self._count = 0
        _emit(self._cb, gesture)
