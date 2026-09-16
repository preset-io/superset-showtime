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

import json
import os
import stat
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import pytest

import showtime.core.runner_smoke as runner_smoke_module
from showtime.core.runner_smoke import (
    OUTPUT_LIMIT_BYTES,
    ProcessResult,
    RunnerSmoke,
    run_bounded_process,
)
from showtime.core.task_definition import render_task_definition, rendered_container

IMAGE = "apache/superset@sha256:" + "a" * 64
CONTAINER_ID = "b" * 64


class Token:
    def __init__(self, value: str) -> None:
        self.hex = value


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class Response:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


class FakeDocker:
    """Small Docker transport fake exposing only allowlisted command results."""

    def __init__(
        self,
        *,
        state: str = "running\ttrue\t0\tfalse\thealthy\tstart\t\n",
        create_result: Optional[ProcessResult] = None,
        foreign_owner: bool = False,
        cleanup_fails: bool = False,
        start_result: Optional[ProcessResult] = None,
        logs_result: Optional[ProcessResult] = None,
    ) -> None:
        self.calls: List[List[str]] = []
        self.state = state
        self.create_result = create_result or ProcessResult(0, CONTAINER_ID + "\n")
        self.foreign_owner = foreign_owner
        self.cleanup_fails = cleanup_fails
        self.start_result = start_result or ProcessResult(0, CONTAINER_ID + "\n")
        self.logs_result = logs_result or ProcessResult(0, "")
        self.owner = ""
        self.name = ""
        self.env_path = ""
        self.env_contents = ""
        self.env_mode = 0

    def __call__(
        self,
        command: Sequence[str],
        _timeout: float,
        **_kwargs: Any,
    ) -> ProcessResult:
        cmd = list(command)
        self.calls.append(cmd)
        operation = cmd[1]
        if operation == "pull":
            return ProcessResult(0)
        if operation == "create":
            self.name = cmd[cmd.index("--name") + 1]
            label = cmd[cmd.index("--label") + 1]
            self.owner = label.split("=", 1)[1]
            self.env_path = cmd[cmd.index("--env-file") + 1]
            self.env_contents = Path(self.env_path).read_text()
            self.env_mode = stat.S_IMODE(os.stat(self.env_path).st_mode)
            return self.create_result
        if operation == "inspect":
            template = cmd[cmd.index("--format") + 1]
            if ".Config.Labels" in template:
                owner = "foreign" if self.foreign_owner else self.owner
                return ProcessResult(0, f"{CONTAINER_ID}\t{owner}\t{IMAGE}\n")
            return ProcessResult(0, self.state)
        if operation == "start":
            return self.start_result
        if operation == "port":
            return ProcessResult(0, "127.0.0.1:49152\n")
        if operation == "logs":
            return self.logs_result
        if operation == "rm":
            return (
                ProcessResult(1, error="remove failed") if self.cleanup_fails else ProcessResult(0)
            )
        raise AssertionError(cmd)


def task_definition() -> Dict[str, Any]:
    rendered = render_task_definition(
        IMAGE,
        [
            {"name": "SUPERSET_LOAD_EXAMPLES", "value": "no"},
            {"name": "SUPERSET_FEATURE_TEST", "value": "True"},
        ],
    )
    container = rendered_container(rendered)
    container["entryPoint"] = ["python", "-m"]
    container["command"] = ["superset"]
    return rendered


def token_factory() -> Any:
    values = iter([Token("name-token"), Token("owner-token"), Token("artifact-token")])
    return lambda: next(values)


def test_rendered_definition_merges_flags_by_name_without_mutating_packaged_data() -> None:
    first = task_definition()
    second = render_task_definition(IMAGE)
    environment = rendered_container(first)["environment"]

    assert [entry["name"] for entry in environment] == sorted(
        entry["name"] for entry in environment
    )
    assert {entry["name"]: entry["value"] for entry in environment}[
        "SUPERSET_LOAD_EXAMPLES"
    ] == "no"
    assert {entry["name"]: entry["value"] for entry in rendered_container(second)["environment"]}[
        "SUPERSET_LOAD_EXAMPLES"
    ] == "yes"


def test_healthy_smoke_uses_rendered_configuration_and_cleans_before_success(
    tmp_path: Path,
) -> None:
    docker = FakeDocker()
    runner = RunnerSmoke(
        process_runner=docker,
        http_get=lambda *_args, **_kwargs: Response(200),
        token_factory=token_factory(),
    )

    result = runner.run(
        IMAGE,
        task_definition(),
        pr_number=1234,
        sha="abc123f",
        diagnostics_dir=str(tmp_path / "diagnostics"),
    )

    assert result.success is True
    assert result.cleanup_success is True
    create = next(call for call in docker.calls if call[1] == "create")
    assert create[create.index("--cpus") + 1] == "0.5"
    assert create[create.index("--memory") + 1] == "2048m"
    assert create[create.index("--memory-swap") + 1] == "2048m"
    assert create[create.index("--publish") + 1] == "127.0.0.1::8080"
    assert create[-3:] == [IMAGE, "-m", "superset"]
    assert create[create.index("--entrypoint") + 1] == "python"
    assert "SUPERSET_LOAD_EXAMPLES=no" in docker.env_contents
    assert "SUPERSET_FEATURE_TEST=True" in docker.env_contents
    assert docker.env_mode == 0o600
    assert not Path(docker.env_path).exists()
    assert docker.calls[-1] == ["docker", "rm", "--force", CONTAINER_ID]


def test_environment_values_never_appear_in_docker_argv(tmp_path: Path) -> None:
    docker = FakeDocker()
    definition = task_definition()
    rendered_container(definition)["environment"].append(
        {"name": "DATABASE_PASSWORD", "value": "argv-secret"}
    )
    runner = RunnerSmoke(
        process_runner=docker,
        http_get=lambda *_args, **_kwargs: Response(200),
        token_factory=token_factory(),
    )

    assert runner.run(
        IMAGE,
        definition,
        pr_number=1,
        sha="abc123f",
        diagnostics_dir=str(tmp_path / "diagnostics"),
    ).success
    assert "argv-secret" not in " ".join(part for call in docker.calls for part in call)


@pytest.mark.parametrize(
    "state,outcome",
    [
        ("exited\tfalse\t9\tfalse\t\tstart\tfinish\n", "nonzero_exit"),
        ("exited\tfalse\t137\ttrue\t\tstart\tfinish\n", "oom"),
    ],
)
def test_terminal_container_state_is_classified_and_artifacted(
    tmp_path: Path, state: str, outcome: str
) -> None:
    docker = FakeDocker(state=state, logs_result=ProcessResult(0, "startup failed\n"))
    runner = RunnerSmoke(
        process_runner=docker,
        http_get=lambda *_args, **_kwargs: Response(503),
        token_factory=token_factory(),
    )

    result = runner.run(
        IMAGE,
        task_definition(),
        pr_number=1234,
        sha="abc123f",
        diagnostics_dir=str(tmp_path / "diagnostics"),
    )

    assert result.success is False
    assert result.primary_outcome == outcome
    assert result.cleanup_success is True
    assert result.log_state == "captured_nonempty"
    assert len(result.artifact_paths) == 2
    assert all(stat.S_IMODE(os.stat(path).st_mode) == 0o600 for path in result.artifact_paths)
    payload = json.loads(Path(result.artifact_paths[0]).read_text())
    assert set(payload) == {
        "cleanup_success",
        "error",
        "image_reference",
        "log_state",
        "mapped_port",
        "primary_outcome",
        "secondary_errors",
        "state",
    }
    assert "environment" not in Path(result.artifact_paths[0]).read_text().lower()


def test_health_deadline_and_cleanup_failure_preserve_primary_outcome(tmp_path: Path) -> None:
    clock = Clock()
    docker = FakeDocker(cleanup_fails=True, logs_result=ProcessResult(1, error="logs denied"))
    runner = RunnerSmoke(
        process_runner=docker,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
        http_get=lambda *_args, **_kwargs: Response(503),
        token_factory=token_factory(),
    )

    result = runner.run(
        IMAGE,
        task_definition(),
        pr_number=1234,
        sha="abc123f",
        timeout_seconds=3,
        diagnostics_dir=str(tmp_path / "diagnostics"),
    )

    assert result.primary_outcome == "timeout"
    assert result.cleanup_success is False
    assert result.log_state == "unavailable"
    assert any("owned cleanup failed" in error for error in result.secondary_errors)


def test_cleanup_failure_after_healthy_probe_blocks_success(tmp_path: Path) -> None:
    docker = FakeDocker(cleanup_fails=True)
    runner = RunnerSmoke(
        process_runner=docker,
        http_get=lambda *_args, **_kwargs: Response(200),
        token_factory=token_factory(),
    )

    result = runner.run(
        IMAGE,
        task_definition(),
        pr_number=1,
        sha="abc123f",
        diagnostics_dir=str(tmp_path / "diagnostics"),
    )

    assert result.success is False
    assert result.primary_outcome == "healthy"
    assert result.cleanup_success is False
    assert "cleanup failed" in (result.error or "")
    assert len(result.artifact_paths) == 2


def test_empty_logs_are_distinct_from_unavailable_logs(tmp_path: Path) -> None:
    docker = FakeDocker(
        state="exited\tfalse\t1\tfalse\t\tstart\tfinish\n",
        logs_result=ProcessResult(0, ""),
    )
    runner = RunnerSmoke(process_runner=docker, token_factory=token_factory())

    result = runner.run(
        IMAGE,
        task_definition(),
        pr_number=1,
        sha="abc123f",
        diagnostics_dir=str(tmp_path / "diagnostics"),
    )

    assert result.log_state == "captured_empty"


def test_artifact_exception_does_not_replace_primary_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    docker = FakeDocker(state="exited\tfalse\t1\tfalse\t\tstart\tfinish\n")
    runner = RunnerSmoke(process_runner=docker, token_factory=token_factory())
    monkeypatch.setattr(
        runner_smoke_module,
        "_exclusive_write",
        lambda *_args: (_ for _ in ()).throw(OSError("artifact-secret-payload")),
    )

    result = runner.run(
        IMAGE,
        task_definition(),
        pr_number=1,
        sha="abc123f",
        diagnostics_dir=str(tmp_path / "diagnostics"),
    )

    assert result.primary_outcome == "nonzero_exit"
    assert result.error == "runner smoke container exited with code 1"
    assert "artifact-secret-payload" not in " ".join(result.secondary_errors)
    assert any("artifact write failed" in error for error in result.secondary_errors)


@pytest.mark.parametrize("bad_value", ["line1\nline2", "nul\x00value"])
def test_invalid_environment_is_rejected_before_docker(tmp_path: Path, bad_value: str) -> None:
    docker = FakeDocker()
    definition = task_definition()
    rendered_container(definition)["environment"].append(
        {"name": "UNSAFE_VALUE", "value": bad_value}
    )
    runner = RunnerSmoke(process_runner=docker, token_factory=token_factory())

    with pytest.raises(ValueError, match="forbidden character"):
        runner.run(
            IMAGE,
            definition,
            pr_number=1,
            sha="abc123f",
            diagnostics_dir=str(tmp_path / "diagnostics"),
        )

    assert docker.calls == []


def test_lost_create_response_recovers_only_verified_owned_container(tmp_path: Path) -> None:
    docker = FakeDocker(create_result=ProcessResult(None, timed_out=True))
    runner = RunnerSmoke(
        process_runner=docker,
        http_get=lambda *_args, **_kwargs: Response(200),
        token_factory=token_factory(),
    )

    result = runner.run(
        IMAGE,
        task_definition(),
        pr_number=1,
        sha="abc123f",
        diagnostics_dir=str(tmp_path / "diagnostics"),
    )

    assert result.success is True
    assert any(call[1] == "rm" for call in docker.calls)


def test_foreign_name_collision_is_never_removed(tmp_path: Path) -> None:
    docker = FakeDocker(create_result=ProcessResult(1), foreign_owner=True)
    runner = RunnerSmoke(
        process_runner=docker,
        token_factory=token_factory(),
    )

    result = runner.run(
        IMAGE,
        task_definition(),
        pr_number=1,
        sha="abc123f",
        diagnostics_dir=str(tmp_path / "diagnostics"),
    )

    assert result.success is False
    assert result.cleanup_success is False
    assert any("ownership mismatch" in error for error in result.secondary_errors)
    assert not any(call[1] == "rm" for call in docker.calls)


def test_interrupted_start_still_runs_bounded_owned_cleanup(tmp_path: Path) -> None:
    docker = FakeDocker(start_result=ProcessResult(None, interrupted=True))
    runner = RunnerSmoke(
        process_runner=docker,
        token_factory=token_factory(),
    )

    result = runner.run(
        IMAGE,
        task_definition(),
        pr_number=1,
        sha="abc123f",
        diagnostics_dir=str(tmp_path / "diagnostics"),
    )

    assert result.primary_outcome == "interrupted"
    assert result.cleanup_success is True
    assert any(call[1] == "rm" for call in docker.calls)


class ChunkStream:
    def __init__(self, chunks: Sequence[bytes]) -> None:
        self.chunks = iter(chunks)

    def read(self, _size: int) -> bytes:
        return next(self.chunks, b"")


class FinishedProcess:
    pid = 99999999

    def __init__(self, chunks: Sequence[bytes]) -> None:
        self.stdout = ChunkStream(chunks)

    def poll(self) -> int:
        return 0

    def wait(self, _timeout: Optional[float] = None) -> int:
        return 0


def test_process_output_redacts_secrets_split_across_chunks_and_is_bounded() -> None:
    secret = "cross-chunk-secret"
    chunks = [b"TOKEN=cross-", b"chunk-secret\n", b"x" * (OUTPUT_LIMIT_BYTES + 1)]
    result = run_bounded_process(
        ["unused"],
        1,
        secret_values=[secret],
        popen_factory=lambda *_args, **_kwargs: FinishedProcess(chunks),
    )

    assert secret not in result.output
    assert "[REDACTED]" in result.output
    assert len(result.output.encode()) <= OUTPUT_LIMIT_BYTES


def test_process_start_exception_is_redacted() -> None:
    secret = "exception-secret"

    def fail_to_start(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError(f"TOKEN={secret}")

    result = run_bounded_process(
        ["unused"],
        1,
        secret_values=[secret],
        popen_factory=fail_to_start,
    )

    assert secret not in (result.error or "")
    assert "[REDACTED]" in (result.error or "")


def test_real_silent_process_timeout_is_finite() -> None:
    started = time.monotonic()
    result = run_bounded_process(
        [sys.executable, "-c", "import time; time.sleep(10)"],
        0.2,
        termination_grace_seconds=0.1,
    )

    assert result.timed_out is True
    assert time.monotonic() - started < 2


@pytest.mark.skipif(not hasattr(os, "killpg"), reason="process groups require POSIX")
def test_direct_child_exit_with_descendant_held_stdout_is_finite() -> None:
    script = (
        "import subprocess,sys; "
        "subprocess.Popen([sys.executable,'-c','import time; time.sleep(10)']); "
        "print('direct child exited')"
    )
    started = time.monotonic()
    result = run_bounded_process(
        [sys.executable, "-c", script],
        0.3,
        termination_grace_seconds=0.1,
    )

    assert result.timed_out is True
    assert "direct child exited" in result.output
    assert time.monotonic() - started < 2
