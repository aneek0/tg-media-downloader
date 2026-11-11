from __future__ import annotations

import time


class CooldownManager:
    def __init__(self, timeout_seconds: int) -> None:
        self.timeout_seconds = timeout_seconds
        self._last_seen: dict[int, float] = {}

    def check(self, user_id: int, authorized_users: set[int]) -> int:
        if user_id in authorized_users:
            return 0
        now = time.time()
        if len(self._last_seen) >= 10_000:
            # One prune per 10k users: entries older than the cooldown are
            # meaningless, and inline strangers grow this dict forever.
            cutoff = now - self.timeout_seconds
            self._last_seen = {
                uid: seen for uid, seen in self._last_seen.items() if seen > cutoff
            }
        previous = self._last_seen.get(user_id)
        self._last_seen[user_id] = now
        if previous is None:
            return 0
        elapsed = now - previous
        if elapsed >= self.timeout_seconds:
            return 0
        return int(self.timeout_seconds - elapsed)
