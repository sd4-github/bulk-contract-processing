"""Mocked extraction service.

Simulates an external document-extraction/OCR API. Given a document and a set
of variable names it returns plausible random values. Later this module can be
swapped for a real provider (see DESIGN.md) without changing callers, because
the worker only depends on the `extract` function signature.
"""

import asyncio
import random
from typing import Iterable

from ..config import settings

# Value generators keyed by (partial) variable-name heuristics. Unknown
# variables fall back to a generic generator.
_MONEY_PATTERNS = ("rent", "deposit", "charges", "fee", "amount", "price")
_DATE_PATTERNS = ("date", "day", "period", "validity")
_NAME_PATTERNS = ("name", "party", "person", "company")
_LAW_PATTERNS = ("law", "jurisdiction", "state")


def _random_date(rng: random.Random) -> str:
    year = rng.randint(2020, 2035)
    month = rng.randint(1, 12)
    day = rng.randint(1, 28)
    return f"{year:04d}-{month:02d}-{day:02d}"


def _random_money(rng: random.Random) -> str:
    return f"Rs. {rng.randint(500, 500000):,}"


def _random_name(rng: random.Random) -> str:
    first = rng.choice(
        ["Aarav", "Meera", "Rohan", "Sneha", "Kabir", "Ananya"]
    )
    last = rng.choice(
        ["Sharma", "Iyer", "Patel", "Reddy", "Nair", "Gupta"]
    )
    return f"{first} {last}"


def _random_law(rng: random.Random) -> str:
    return rng.choice(
        [
            "Laws of India",
            "Law of England and Wales",
            "Laws of Singapore",
            "Law of the State of New York",
        ]
    )


def _generic(rng: random.Random) -> str:
    return f"value-{rng.randint(100000, 999999)}"


def _generator_for(variable: str):
    lowered = variable.lower()
    if any(p in lowered for p in _MONEY_PATTERNS):
        return _random_money
    if any(p in lowered for p in _DATE_PATTERNS):
        return _random_date
    if any(p in lowered for p in _LAW_PATTERNS):
        return _random_law
    if any(p in lowered for p in _NAME_PATTERNS):
        return _random_name
    return _generic


class MockExtractionError(Exception):
    """Raised when the mocked API fails (simulates a provider timeout/500)."""


async def extract(
    document_path: str,
    variables: Iterable[str],
    *,
    seed: int | None = None,
) -> dict[str, str]:
    """Return {variable: value} for each requested variable.

    Simulates network latency via an async sleep, then returns plausible random
    values. A seeded RNG keeps results deterministic for a given document.
    """
    await asyncio.sleep(settings.mock_extraction_delay_seconds)

    if settings.mock_failure_rate > 0 and random.random() < settings.mock_failure_rate:
        raise MockExtractionError(
            "Simulated extraction provider failure (timeout / 5xx)"
        )

    rng = random.Random(seed if seed is not None else document_path)
    return {
        variable: _generator_for(variable)(rng) for variable in variables
    }