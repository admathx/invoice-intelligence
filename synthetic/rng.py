import random


def rng_for(*parts: object) -> random.Random:
    """A random.Random deterministically seeded from `parts`, independent of call order."""
    return random.Random("::".join(str(p) for p in parts))
