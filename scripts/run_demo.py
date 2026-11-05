"""Launch all localhost HTTP services with readiness checks and cleanup."""

import argparse
import signal
import sys

from ai_field_assessment.config import Settings
from ai_field_assessment.demo import DemoCluster


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    settings = Settings.from_env()
    parser.add_argument("--base-port", type=int, default=settings.gateway_port)
    parser.add_argument("--host", default=settings.gateway_host)
    args = parser.parse_args()

    def stop(signum, frame):
        raise KeyboardInterrupt()

    signal.signal(signal.SIGTERM, stop)
    try:
        with DemoCluster(base_port=args.base_port, host=args.host) as cluster:
            for name in cluster.ports:
                print(f"{name}: {cluster.url(name)}", flush=True)
            print("All demo services ready. Ctrl+C stops the cluster.", flush=True)
            cluster.wait()
    except KeyboardInterrupt:
        return 0
    except (OSError, RuntimeError, TimeoutError, ValueError) as exc:
        print(f"Demo startup failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
