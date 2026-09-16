# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.

"""Exercise deadline and cleanup guarantees across real Showtime boundaries."""

import os
import signal
import sys
import threading
import time
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from showtime.core import runner_smoke as module
from showtime.core.runner_smoke import RunnerSmoke, run_bounded_process
from tests.unit.test_phase3_smoke import IMAGE, Clock, FakeDocker, Response, task_definition


def run_smoke(tmp_path: Path, docker: FakeDocker, **kwargs: Any) -> Any:
    """Run against an isolated fake Docker transport and artifact directory."""
    return RunnerSmoke(process_runner=docker, **kwargs).run(
        IMAGE,
        task_definition(),
        pr_number=1234,
        sha="abc123f",
        timeout_seconds=10,
        diagnostics_dir=str(tmp_path),
    )


def test_late_health_200_cannot_authorize_deployment(tmp_path: Path) -> None:
    """A healthy header returned beyond startup deadline remains a timeout."""
    clock = Clock()

    def late(*args: Any, **kwargs: Any) -> Response:
        clock.sleep(11)
        return Response(200)

    result = run_smoke(
        tmp_path, FakeDocker(), monotonic=clock.monotonic, sleep=clock.sleep, http_get=late
    )
    assert not result.success
    assert result.primary_outcome == "timeout"
    assert result.cleanup_success


def test_interruption_during_health_cannot_authorize_deployment(tmp_path: Path) -> None:
    """The scoped SIGTERM event is checked after healthy response headers."""

    def interrupted(*args: Any, **kwargs: Any) -> Response:
        os.kill(os.getpid(), signal.SIGTERM)
        return Response(200)

    result = run_smoke(tmp_path, FakeDocker(), http_get=interrupted)
    assert not result.success
    assert result.primary_outcome == "interrupted"
    assert result.cleanup_success


def test_diagnostic_exception_does_not_skip_cleanup(tmp_path: Path) -> None:
    """Evidence collection cannot displace the failed container outcome."""
    docker = FakeDocker(state="exited\tfalse\t9\tfalse\t\tstart\tfinish\n")
    with patch.object(RunnerSmoke, "_collect_evidence", side_effect=OSError("evidence error")):
        result = run_smoke(tmp_path, docker)
    assert result.primary_outcome == "nonzero_exit"
    assert result.cleanup_success
    assert any(call[1] == "rm" for call in docker.calls)
    assert result.artifact_paths


def test_env_unlink_failure_still_recovers_and_cleans_owned_container(tmp_path: Path) -> None:
    """A local temporary-file error cannot abandon an allocated container."""
    docker = FakeDocker()
    real_unlink = module._unlink_secret_file
    calls = 0

    def fail_once(path: str) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("temporary unlink failure")
        real_unlink(path)

    try:
        with patch.object(module, "_unlink_secret_file", side_effect=fail_once):
            result = run_smoke(tmp_path, docker, http_get=lambda *a, **k: Response(200))
        assert not result.success
        assert result.cleanup_success
        assert any(call[1] == "rm" for call in docker.calls)
        assert not Path(docker.env_path).exists()
    finally:
        if docker.env_path:
            real_unlink(docker.env_path)


def test_failed_cleanup_after_health_collects_failure_evidence(tmp_path: Path) -> None:
    """A resource left behind gets logs/state even after successful startup."""
    result = run_smoke(
        tmp_path, FakeDocker(cleanup_fails=True), http_get=lambda *a, **k: Response(200)
    )
    assert not result.success
    assert result.log_state == "captured_empty"


def test_smoke_pull_and_create_explicitly_select_ecs_platform(tmp_path: Path) -> None:
    """A manifest index uses linux/amd64 on any supported runner host."""
    docker = FakeDocker()
    assert run_smoke(tmp_path, docker, http_get=lambda *a, **k: Response(200)).success
    for command in docker.calls:
        if command[1] in ("pull", "create"):
            assert command[command.index("--platform") + 1] == "linux/amd64"


@pytest.mark.skipif(not hasattr(os, "killpg"), reason="owned process groups require POSIX")
def test_signal_interrupts_descendant_stdout_wait_promptly() -> None:
    """SIGTERM during inherited-pipe draining stops the owned child group."""
    script = (
        "import subprocess,sys; "
        "subprocess.Popen([sys.executable,'-c','import time; time.sleep(3)']); "
        "print('direct child done')"
    )
    timer = threading.Timer(0.25, lambda: os.kill(os.getpid(), signal.SIGTERM))
    # Outer handler protects the test runner while exercising missing inner handling.
    interrupted = threading.Event()
    started = time.monotonic()
    with module._scoped_interrupt_handlers(interrupted):
        timer.start()
        try:
            result = run_bounded_process(
                [sys.executable, "-c", script],
                2,
                interrupt_event=interrupted,
                termination_grace_seconds=0.1,
            )
        finally:
            timer.cancel()
    assert result.interrupted
    assert time.monotonic() - started < 1.5


@pytest.mark.parametrize("local_healthy, ecs_healthy", [(True, True), (False, True), (True, False)])
def test_real_build_smoke_ecs_chain_uses_one_digest_and_keeps_both_gates(
    tmp_path: Path, local_healthy: bool, ecs_healthy: bool
) -> None:
    """Only external processes/SDK/HTTP are faked across real orchestration."""
    import json
    from unittest.mock import Mock

    from showtime.core.aws import AWSInterface
    from showtime.core.pull_request import PullRequest
    from showtime.core.runner_smoke import ProcessResult
    from showtime.core.show import Show
    from tests.unit.test_phase2_parent_regressions import healthy_clients
    from tests.unit.test_phase2_readiness import EXPECTED_DEFINITION, FakeClock, FakeHttpClient
    from tests.unit.test_phase3_integration import _sync_context

    clock = FakeClock()
    docker = FakeDocker()
    smoke = RunnerSmoke(
        process_runner=docker,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
        http_get=lambda *a, **k: Response(200 if local_healthy else 503),
    )
    ecs, ec2 = healthy_clients()
    ecs.register_task_definition.return_value = {
        "taskDefinition": {"taskDefinitionArn": EXPECTED_DEFINITION}
    }
    remote_health = FakeHttpClient([200] if ecs_healthy else [503] * 20)
    aws = AWSInterface(
        ecs_client=ecs,
        ecr_client=Mock(),
        ec2_client=ec2,
        http_client_factory=lambda: remote_health,
        monotonic=clock.monotonic,
        wall_clock=clock.wall_clock,
        sleep=clock.sleep,
    )
    pr = PullRequest(1234, [])
    candidate = Show(1234, "abc123f", "building")

    def build(command: Any, *args: Any, **kwargs: Any) -> ProcessResult:
        Path(command[command.index("--metadata-file") + 1]).write_text(
            json.dumps(
                {
                    "containerimage.digest": IMAGE.split("@", 1)[1],
                    "containerimage.config.digest": "sha256:" + "c" * 64,
                }
            )
        )
        return ProcessResult(0)

    with _sync_context(pr, candidate) as stack:
        stack.enter_context(patch("showtime.core.show.run_bounded_process", side_effect=build))
        stack.enter_context(patch("showtime.core.show.RunnerSmoke", return_value=smoke))
        stack.enter_context(patch("showtime.core.show.get_interfaces", return_value=(Mock(), aws)))
        stack.enter_context(patch.object(aws, "_service_exists_any_state", return_value=False))
        stack.enter_context(patch.object(aws, "delete_environment", return_value=True))
        outcome = pr.sync(
            "abc123f",
            smoke_test=True,
            smoke_timeout_seconds=10,
            startup_timeout_seconds=20,
            smoke_diagnostics_dir=str(tmp_path),
        )
    assert outcome.success is (local_healthy and ecs_healthy)
    create = next(call for call in docker.calls if call[1] == "create")
    assert IMAGE in create
    assert any(call[1] == "rm" for call in docker.calls)
    if local_healthy:
        registered = ecs.register_task_definition.call_args.kwargs
        assert registered["containerDefinitions"][0]["image"] == IMAGE
        assert remote_health.urls
    else:
        ecs.register_task_definition.assert_not_called()
        ecs.create_service.assert_not_called()
        assert not remote_health.urls


def test_term_grace_waits_for_child_even_after_stdout_closes(tmp_path: Path) -> None:
    """Closing output during graceful shutdown must not cause immediate SIGKILL."""
    marker = tmp_path / "graceful-exit"
    script = """import os, signal, sys, time
from pathlib import Path
def stop(signum, frame):
    os.close(1)
    os.close(2)
    time.sleep(0.15)
    Path(sys.argv[1]).write_text('graceful')
    sys.exit(0)
signal.signal(signal.SIGTERM, stop)
print('ready', flush=True)
time.sleep(10)
"""
    result = run_bounded_process(
        [sys.executable, "-c", script, str(marker)], 0.3, termination_grace_seconds=0.5
    )
    assert result.timed_out
    assert result.returncode == 0
    assert marker.read_text() == "graceful"


def test_docker_failure_retains_useful_redacted_stderr_in_artifact(tmp_path: Path) -> None:
    """Finite real subprocess stderr survives the fake Docker failure boundary."""
    import json

    class FailingPull(FakeDocker):
        def __call__(self, command: Any, timeout: float, **kwargs: Any) -> Any:
            if command[1] == "pull":
                return run_bounded_process(
                    [
                        sys.executable,
                        "-c",
                        "import sys; print('registry unavailable password=private-sentinel', file=sys.stderr); sys.exit(1)",
                    ],
                    timeout,
                    **kwargs,
                )
            return super().__call__(command, timeout, **kwargs)

    result = run_smoke(tmp_path, FailingPull())
    assert not result.success
    assert "registry unavailable" in result.error
    assert "private-sentinel" not in result.error
    payload = json.loads(Path(result.artifact_paths[0]).read_text())
    assert "registry unavailable" in payload["error"]
    assert "private-sentinel" not in str(payload)


def test_partial_env_write_failure_keeps_path_owned_and_primary_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A partial secret write and transient unlink failure still remove the file."""
    real_fdopen = module.os.fdopen
    real_unlink = module._unlink_secret_file
    paths = []
    attempts = 0

    class FailingStream:
        def __init__(self, stream: Any) -> None:
            self.stream = stream

        def __enter__(self) -> Any:
            return self

        def __exit__(self, *args: Any) -> Any:
            return self.stream.__exit__(*args)

        def write(self, value: str) -> Any:
            count = self.stream.write(value)
            if value.startswith("SUPERSET_SECRET_KEY="):
                self.stream.flush()
                raise OSError("primary environment write failure")
            return count

    def fdopen(*args: Any, **kwargs: Any) -> Any:
        return FailingStream(real_fdopen(*args, **kwargs))

    def unlink(path: str) -> None:
        nonlocal attempts
        paths.append(path)
        attempts += 1
        if attempts == 1:
            raise OSError("secondary unlink failure")
        real_unlink(path)

    monkeypatch.setattr(module.os, "fdopen", fdopen)
    monkeypatch.setattr(module, "_unlink_secret_file", unlink)
    try:
        result = run_smoke(tmp_path, FakeDocker())
        assert "primary environment write failure" in result.error
        assert paths and not Path(paths[0]).exists()
        assert attempts >= 2
    finally:
        for path in paths:
            real_unlink(path)


def test_create_timeout_remains_primary_when_env_unlink_also_fails(tmp_path: Path) -> None:
    """Secondary file cleanup cannot erase ambiguous-create timeout evidence."""
    from showtime.core.runner_smoke import ProcessResult

    docker = FakeDocker(create_result=ProcessResult(None, "redacted Docker detail", timed_out=True))
    real_unlink = module._unlink_secret_file
    attempts = 0

    def unlink(path: str) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("secondary unlink failure")
        real_unlink(path)

    try:
        with patch.object(module, "_unlink_secret_file", side_effect=unlink):
            result = run_smoke(tmp_path, docker)
        assert result.primary_outcome == "timeout"
        assert "redacted Docker detail" in result.error
        assert result.cleanup_success
        assert any("environment cleanup" in error for error in result.secondary_errors)
        assert not Path(docker.env_path).exists()
    finally:
        if docker.env_path:
            real_unlink(docker.env_path)
