"""Start the mock providers and three independent HTTP gateways."""

import subprocess
import sys

SERVICES = [
    ("task2_mcp_gateway.mock_mcp_server", 8001),
    ("task3_stream_guardrail.mock_llm_provider", 8002),
    ("task2_mcp_gateway.app", 8000),
    ("task3_stream_guardrail.app", 8003),
    ("task4_model_router.app", 8004),
]


def main() -> None:
    processes = []
    try:
        for module, port in SERVICES:
            processes.append(subprocess.Popen([
                sys.executable, "-m", "uvicorn", f"ai_field_assessment.{module}:app",
                "--host", "127.0.0.1", "--port", str(port),
            ]))
        print("Demo services starting on localhost:8000-8004. Ctrl+C stops all services.")
        processes[-1].wait()
    except KeyboardInterrupt:
        pass
    finally:
        for process in processes:
            process.terminate()
        for process in processes:
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


if __name__ == "__main__":
    main()
