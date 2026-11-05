"""Concurrent Gemini lookups (approval dates, PTE)."""

import asyncio

from .constants import LOOKUP_CONCURRENCY


async def run_lookups(jobs: set, lookup) -> dict:
    """{key: lookup(*key)} for every key in *jobs*, each run in a thread, at most LOOKUP_CONCURRENCY at once."""
    semaphore = asyncio.Semaphore(LOOKUP_CONCURRENCY)

    async def one(key):
        async with semaphore:
            return key, await asyncio.to_thread(lookup, *key)

    return dict(await asyncio.gather(*[one(key) for key in sorted(jobs)]))
