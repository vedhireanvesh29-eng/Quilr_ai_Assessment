"""Synchronous checks for restricted hosts where asyncio loopback sockets are blocked."""

import concurrent.futures
import json
import tempfile
import unittest
from pathlib import Path

from ai_field_assessment.task4_model_router.rate_limiter import RateLimitExceeded, RateLimiter


def main() -> None:
    suite = unittest.TestLoader().discover("tests", pattern="test_core.py")
    synchronous = unittest.TestSuite()
    for group in suite:
        for cases in group:
            for case in cases:
                if not isinstance(case, unittest.IsolatedAsyncioTestCase):
                    synchronous.addTest(case)
    result = unittest.TextTestRunner(verbosity=2).run(synchronous)
    assert result.wasSuccessful()
    with tempfile.TemporaryDirectory(dir=".") as directory:
        database = str(Path(directory) / "usage.db")
        limiter = RateLimiter(database, limit=50000)
        limiter._initialize()

        def reserve(_: int) -> bool:
            try:
                RateLimiter(database, limit=50000)._reserve("tenant", 20000)
                return True
            except RateLimitExceeded:
                return False

        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
            outcomes = list(executor.map(reserve, range(3)))
        assert sum(outcomes) == 2, outcomes
        print(json.dumps({"sqlite_independent_connection_reservations": outcomes,
                          "accepted": sum(outcomes), "rejected": 3 - sum(outcomes)}))


if __name__ == "__main__":
    main()
