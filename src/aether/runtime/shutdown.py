"""Cross-platform shutdown signal handling (Windows has no loop.add_signal_handler)."""

import asyncio
import logging
import signal
from collections.abc import Callable
from types import FrameType, ModuleType
from typing import Any

logger = logging.getLogger("aether.runtime.shutdown")


def _signal_names() -> tuple[str, ...]:
    return ("SIGINT", "SIGTERM", "SIGBREAK")


class ShutdownSignalHandler:
    """Invokes `callback` on the event loop thread when a termination signal arrives."""

    def __init__(
        self,
        callback: Callable[[], None],
        *,
        signal_module: ModuleType = signal,
    ) -> None:
        self._callback = callback
        self._signal = signal_module
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_signals: list[Any] = []
        self._previous: dict[Any, Any] = {}

    def install(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        for name in _signal_names():
            signum = getattr(self._signal, name, None)
            if signum is None:
                continue
            try:
                loop.add_signal_handler(signum, self._callback)
                self._loop_signals.append(signum)
            except NotImplementedError:
                self._install_fallback(signum)
            except (RuntimeError, ValueError):
                logger.debug("cannot register %s on this loop", name)

    def uninstall(self) -> None:
        loop = self._loop
        for signum in self._loop_signals:
            if loop is not None:
                loop.remove_signal_handler(signum)
        self._loop_signals.clear()
        for signum, previous in self._previous.items():
            try:
                self._signal.signal(signum, previous)
            except (ValueError, TypeError):
                pass
        self._previous.clear()

    def _install_fallback(self, signum: Any) -> None:
        loop = self._loop
        assert loop is not None

        def handler(_signum: int, _frame: FrameType | None) -> None:
            # Signal handlers run in the main thread outside the loop; hop back thread-safely.
            loop.call_soon_threadsafe(self._callback)

        try:
            self._previous[signum] = self._signal.signal(signum, handler)
        except ValueError:
            logger.warning("signal handlers can only be installed from the main thread")
