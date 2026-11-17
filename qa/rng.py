"""Seeded random source for the property harness.

A thin wrapper around :class:`random.Random` that records the seed so a failed
case can always be replayed with ``--seed``.
"""

from __future__ import annotations

import random as _random


class Random:
    def __init__(self, seed: int):
        self.seed = seed
        self._rng = _random.Random(seed)

    def random(self) -> float:
        return self._rng.random()

    def choice(self, seq):
        return self._rng.choice(seq)

    def randrange(self, *args):
        return self._rng.randrange(*args)

    def shuffle(self, seq) -> None:
        self._rng.shuffle(seq)

    def sample(self, seq, k):
        return self._rng.sample(seq, k)
