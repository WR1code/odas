from __future__ import annotations

from avtwin_linux.channel_selection import normalize_channels


def test_missing_selection_defaults_to_all_channels() -> None:
    assert normalize_channels(None) == tuple(range(8))


def test_channel_selection_is_sorted_unique_and_bounded() -> None:
    assert normalize_channels([7, 2, 2, -1, 8]) == (2, 7)
    assert normalize_channels("6, 1, 3") == (1, 3, 6)


def test_explicit_empty_selection_is_preserved() -> None:
    assert normalize_channels([]) == ()
    assert normalize_channels("") == ()
