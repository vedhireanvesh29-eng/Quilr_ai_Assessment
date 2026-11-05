"""Real subprocess and TCP checks, in addition to ASGI unit tests."""

import socket

import pytest

from ai_field_assessment.demo import DemoCluster
from ai_field_assessment.verification import verify_http, verify_mcp


async def test_common_live_stdio_http_and_process_cleanup(tmp_path):
    assert (await verify_mcp())["mock_refund"] == "passed"
    cluster = DemoCluster(base_port=0, database_path=str(tmp_path / "usage.db"))
    with cluster:
        assert all(value == "passed" for value in verify_http(cluster).values())
    assert all(process.poll() is not None for process in cluster.processes.values())
    for port in cluster.ports.values():
        with socket.socket() as probe:
            probe.settimeout(1)
            assert probe.connect_ex(("127.0.0.1", port)) != 0


def test_common_occupied_port_fails_before_spawning():
    with socket.socket() as occupied:
        occupied.bind(("127.0.0.1", 0))
        port = occupied.getsockname()[1]
        if port > 65531:
            pytest.skip("Ephemeral port outside usable consecutive-port range")
        cluster = DemoCluster(base_port=port)
        with pytest.raises(OSError):
            with cluster:
                pytest.fail("Occupied port was accepted")
        assert cluster.processes == {}


def test_common_child_failure_cleans_every_started_process(monkeypatch):
    class FailedProcess:
        returncode = 7

        def poll(self):
            return 7

        def wait(self, timeout=None):
            return 7

    monkeypatch.setattr(
        "ai_field_assessment.demo.subprocess.Popen", lambda *args, **kwargs: FailedProcess()
    )
    cluster = DemoCluster(base_port=0)
    with pytest.raises(RuntimeError, match="exited with status 7"):
        with cluster:
            pytest.fail("Failed service was accepted")
    assert all(log.closed for log in cluster.logs)
