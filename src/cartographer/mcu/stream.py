from __future__ import annotations

from typing import TYPE_CHECKING, Callable, Protocol, TypeVar, final

import greenlet
from typing_extensions import override

from cartographer.stream import Condition, Session, Stream

if TYPE_CHECKING:
    from reactor import Reactor


@final
class CartographerCondition(Condition):
    """Reactor-based equivalent of [threading.Condition](https://docs.python.org/3/library/threading.html#condition-objects)."""

    def __init__(self, reactor: Reactor):
        self.reactor = reactor
        self.waiting: list[greenlet.greenlet] = []

    @override
    def notify_all(self):
        for wait in self.waiting:
            self.reactor.update_timer(wait.timer, self.reactor.NOW)

    @override
    def wait_for(self, predicate: Callable[[], bool], timeout: float | None = None) -> bool:
        if predicate():
            return True
        wait = greenlet.getcurrent()
        deadline = self.reactor.NEVER if timeout is None else self.reactor.monotonic() + timeout
        self.waiting.append(wait)
        try:
            while not predicate():
                eventtime = self.reactor.pause(deadline)
                if timeout is not None and eventtime >= deadline and not predicate():
                    return False
            return True
        finally:
            self.waiting.remove(wait)


T = TypeVar("T")


class CartographerStreamMcu(Protocol):
    def start_streaming(self) -> None:
        """Used to ask the MCU to start sending data."""
        ...

    def stop_streaming(self) -> None:
        """Stop the MCU from sending data.
        Will be called when the last session ends.
        """
        ...


@final
class CartographerStream(Stream[T]):
    def __init__(
        self,
        mcu: CartographerStreamMcu,
        reactor: Reactor,
    ):
        super().__init__()
        self.reactor = reactor
        self.mcu = mcu

    @override
    def condition(self) -> Condition:
        return CartographerCondition(self.reactor)

    @override
    def start_session(self, start_condition: Callable[[T], bool] | None = None) -> Session[T]:
        if len(self.sessions) == 0:
            self.mcu.start_streaming()
        return super().start_session(start_condition)

    @override
    def end_session(self, session: Session[T]) -> None:
        super().end_session(session)
        if len(self.sessions) == 0:
            self.mcu.stop_streaming()
