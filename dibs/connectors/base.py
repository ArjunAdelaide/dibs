"""Shared shapes for live availability."""

from dataclasses import dataclass, field


@dataclass
class Rate:
    name: str
    price: float
    url: str


@dataclass
class Slot:
    time: str  # 24h HH:MM, local
    min_players: int = 1
    max_players: int = 4
    strict_max: bool = False  # True when max_players is the number of free places left
    rates: list[Rate] = field(default_factory=list)

    def fits(self, party_size: int) -> bool:
        return self.min_players <= party_size and (not self.strict_max or party_size <= self.max_players)
