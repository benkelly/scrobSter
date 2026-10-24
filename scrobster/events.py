"""What happens inside scrobSter, for whoever wants to follow it.

The listener and the data functions emit an event when something changes: a
match, a scrobble, a room or an account edited. A subscriber gets its own queue
and reads at its own pace. MQTT is one subscriber, and a feed for a client such
as a Home Assistant integration can be another.

Emitting never waits and never fails. A subscriber that falls behind loses its
oldest events, and the `dropped` count on its queue says so, so it can read
the whole state again instead.

Kinds, with their fields:
  match        room_id (None for a browser clip), users (ids), match
  scrobble     user_id, match_id, results ({service: status})
  cleared      users (ids): their playing-now mark was removed
  listening    on
  rooms        a room was added, changed or removed, or its loop started or stopped
  member       room_id, user_id, on
  users        an account was added, changed or removed
  services     user_id: their service credentials changed
  rates        how often Shazam is asked changed
"""
import asyncio
import collections
import contextlib
import time

QUEUE_SIZE = 200


class Subscription:
    """One subscriber's events, oldest first."""

    def __init__(self, size):
        self._events = collections.deque(maxlen=size)
        self._waiters = []
        self.dropped = 0

    def _put(self, event):
        if len(self._events) == self._events.maxlen:
            self.dropped += 1
        self._events.append(event)
        for waiter in self._waiters:
            if not waiter.done():
                waiter.set_result(None)
        self._waiters.clear()

    def drain(self) -> list[dict]:
        """Every event waiting, and none left behind."""
        out = list(self._events)
        self._events.clear()
        return out

    async def wait(self, timeout=None) -> list[dict]:
        """The events waiting, or the next ones to arrive within timeout. An
        empty list means the timeout passed."""
        if not self._events:
            waiter = asyncio.get_running_loop().create_future()
            self._waiters.append(waiter)
            try:
                await asyncio.wait_for(waiter, timeout)
            except asyncio.TimeoutError:
                pass
            finally:
                if waiter in self._waiters:
                    self._waiters.remove(waiter)
        return self.drain()


_subscriptions = set()


def emit(kind: str, **fields):
    event = {"kind": kind, "ts": time.time(), **fields}
    for sub in list(_subscriptions):
        sub._put(event)


@contextlib.contextmanager
def subscribe(size=QUEUE_SIZE):
    sub = Subscription(size)
    _subscriptions.add(sub)
    try:
        yield sub
    finally:
        _subscriptions.discard(sub)
