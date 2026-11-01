from typing import NamedTuple


class Mood(NamedTuple):
    key: str
    emoji: str
    label: str


MOODS: list[Mood] = [
    Mood("good", "\U0001F642", "Good"),
    Mood("okay", "\U0001F610", "Okay"),
    Mood("rough", "\U0001F615", "Rough"),
    Mood("low", "\U0001F61E", "Low"),
    Mood("sad", "\U0001F622", "Sad"),
]

_KEYS = {m.key for m in MOODS}


def is_valid(key: str) -> bool:
    return key in _KEYS
