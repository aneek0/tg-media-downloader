from services.cooldown import CooldownManager


def test_authorized_users_bypass_cooldown():
    cooldown = CooldownManager(timeout_seconds=60)

    assert cooldown.check(1, {1}) == 0
    assert cooldown.check(1, {1}) == 0


def test_unauthorized_users_are_limited():
    cooldown = CooldownManager(timeout_seconds=60)

    assert cooldown.check(10, set()) == 0
    remaining = cooldown.check(10, set())

    assert 0 < remaining <= 60


def test_stale_entries_are_pruned_at_the_cap():
    """Regression: inline strangers grew _last_seen forever; entries older
    than the cooldown must be dropped once the dict reaches 10k users."""
    cooldown = CooldownManager(timeout_seconds=60)
    cooldown._last_seen = dict.fromkeys(range(10_000), 0.0)

    assert cooldown.check(500_000, set()) == 0
    assert 500_000 in cooldown._last_seen
    assert 999 not in cooldown._last_seen
