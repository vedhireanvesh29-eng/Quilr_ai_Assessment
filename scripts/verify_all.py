"""Live stdio + HTTP verification, with isolated ports and database."""

import argparse
import asyncio
import json
import tempfile
from pathlib import Path

from ai_field_assessment.demo import DemoCluster
from ai_field_assessment.verification import verify_http, verify_mcp


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base-port", type=int, default=0, help="0 allocates isolated ephemeral ports"
    )
    args = parser.parse_args()
    results = asyncio.run(verify_mcp())
    with tempfile.TemporaryDirectory(prefix="quilr-verify-") as directory:
        with DemoCluster(
            base_port=args.base_port, database_path=str(Path(directory) / "usage.db")
        ) as cluster:
            results.update(verify_http(cluster))
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
