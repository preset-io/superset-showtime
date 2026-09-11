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

"""Bounded, disposable local startup validation for an immutable image."""

import json
import os
import re
import signal
import subprocess
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager, nullcontext
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence, Tuple

import httpx

from .readiness import _redact_text, configured_secret_values
from .task_definition import rendered_container, validate_image_reference

DEFAULT_SMOKE_TIMEOUT_SECONDS = 600
DEFAULT_BUILD_TIMEOUT_SECONDS = 3600
DEFAULT_DIAGNOSTICS_DIR = ".showtime/diagnostics"
COMMAND_TIMEOUT_SECONDS = 30
HTTP_TIMEOUT_SECONDS = 7
DIAGNOSTIC_TIMEOUT_SECONDS = 30
OUTPUT_LIMIT_BYTES = 64 * 1024
LOG_LIMIT_BYTES = 16 * 1024
LOG_LIMIT_LINES = 100
READ_CHUNK_BYTES = 4096
OWNER_LABEL = "showtime.runner-smoke-owner"


def validate_positive_seconds(value: int, name: str) -> int:
    """Validate a configurable process deadline before external work."""
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def validate_diagnostics_directory(value: str) -> Path:
    """Validate an artifact destination without creating or writing it."""
    path = Path(value).expanduser()
    for component in (path, *path.parents):
        if component.is_symlink():
            raise ValueError("smoke diagnostics directory must not use symlinks")
    if path.exists() and not path.is_dir():
        raise ValueError("smoke diagnostics path must be a directory")
    probe = path if path.exists() else path.parent
    while not probe.exists() and probe != probe.parent:
        if probe.is_symlink():
            raise ValueError("smoke diagnostics directory must not use symlinked parents")
        probe = probe.parent
    if not probe.is_dir() or not os.access(probe, os.W_OK):
        raise ValueError("smoke diagnostics directory is not writable")
    return path


@dataclass
class ProcessResult:
    """Finite result from one owned process group."""

    returncode: Optional[int]
    output: str = ""
    timed_out: bool = False
    interrupted: bool = False
    error: Optional[str] = None

    @property
    def success(self) -> bool:
        return self.returncode == 0 and not self.timed_out and not self.interrupted


class _BoundedSink:
    """Streaming-safe redaction with bounded line and retained-output memory."""

    def __init__(self, secrets: Sequence[str], prefix: Optional[str]) -> None:
        self._secrets = secrets
        self._prefix = prefix
        self._line = bytearray()
        self._tail = bytearray()
        self._discarding = False

    def feed(self, chunk: bytes) -> None:
        for part in chunk.splitlines(keepends=True):
            ended = part.endswith((b"\n", b"\r"))
            if self._discarding:
                if ended:
                    self._discarding = False
                continue
            self._line.extend(part)
            if len(self._line) > OUTPUT_LIMIT_BYTES:
                self._line.clear()
                self._discarding = not ended
                self._emit("[output line discarded: exceeded limit]\n")
            elif ended:
                self._flush_line()

    def finish(self) -> None:
        if self._line and not self._discarding:
            self._flush_line()

    def text(self) -> str:
        return self._tail.decode("utf-8", errors="replace")

    def _flush_line(self) -> None:
        value = self._line.decode("utf-8", errors="replace")
        self._line.clear()
        self._emit(_redact_text(value, self._secrets))

    def _emit(self, value: str) -> None:
        encoded = value.encode("utf-8")
        self._tail.extend(encoded)
        if len(self._tail) > OUTPUT_LIMIT_BYTES:
            del self._tail[: len(self._tail) - OUTPUT_LIMIT_BYTES]
        if self._prefix is not None:
            print(f"{self._prefix}{value}", end="" if value.endswith(("\n", "\r")) else "\n")


def _drain_output(stream: Any, sink: _BoundedSink) -> None:
    try:
        while True:
            chunk = stream.read(READ_CHUNK_BYTES)
            if not chunk:
                break
            sink.feed(chunk if isinstance(chunk, bytes) else str(chunk).encode())
    except (OSError, ValueError):
        pass
    finally:
        sink.finish()


def _signal_group(process: Any, signum: int) -> None:
    try:
        kill_group = getattr(os, "killpg", None)
        if kill_group is None:
            raise OSError("process groups are unavailable")
        kill_group(process.pid, signum)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            process.send_signal(signum)
        except (ProcessLookupError, OSError):
            pass


def _group_alive(process: Any) -> bool:
    """Inspect the owned group even when descendants have closed their output."""
    running = process.poll() is None
    try:
        kill_group = getattr(os, "killpg", None)
        if kill_group is None:
            return running
        kill_group(process.pid, 0)
        return True
    except ProcessLookupError:
        return running
    except OSError:
        return running


def _stop_group(process: Any, reader: threading.Thread, grace_seconds: float) -> None:
    """Allow the whole group a finite TERM grace before escalating to KILL."""
    _signal_group(process, signal.SIGTERM)
    deadline = time.monotonic() + max(0.0, grace_seconds)
    while _group_alive(process) and time.monotonic() < deadline:
        remaining = max(0.0, deadline - time.monotonic())
        if process.poll() is None:
            try:
                process.wait(timeout=min(0.05, remaining))
            except subprocess.TimeoutExpired:
                pass
        else:
            time.sleep(min(0.05, remaining))
    if _group_alive(process) or reader.is_alive():
        _signal_group(process, signal.SIGKILL)
    try:
        process.wait(timeout=1)
    except (subprocess.TimeoutExpired, ProcessLookupError):
        pass
    reader.join(timeout=1)


def run_bounded_process(
    command: Sequence[str],
    timeout_seconds: float,
    *,
    secret_values: Sequence[str] = (),
    stream_prefix: Optional[str] = None,
    interrupt_event: Optional[threading.Event] = None,
    popen_factory: Callable[..., Any] = subprocess.Popen,
    monotonic: Callable[[], float] = time.monotonic,
    termination_grace_seconds: float = 5,
) -> ProcessResult:
    """Run a command whose deadline includes child exit and stdout completion."""
    sink = _BoundedSink(secret_values, stream_prefix)
    event = interrupt_event if interrupt_event is not None else threading.Event()
    handlers = _scoped_interrupt_handlers(event) if interrupt_event is None else nullcontext()
    deadline = monotonic() + timeout_seconds
    with handlers:
        try:
            process = popen_factory(
                list(command),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=False,
                bufsize=0,
                start_new_session=True,
            )
        except KeyboardInterrupt:
            return ProcessResult(None, interrupted=True, error="interrupted")
        except Exception as exc:
            return ProcessResult(None, error=_redact_text(str(exc), secret_values)[:500])

        reader = threading.Thread(target=_drain_output, args=(process.stdout, sink), daemon=True)
        reader.start()
        timed_out = False
        interrupted = False
        try:
            # Completion includes EOF: descendants can retain the output descriptor.
            while process.poll() is None or reader.is_alive():
                interrupted = event.is_set()
                remaining = deadline - monotonic()
                if interrupted or remaining <= 0:
                    timed_out = not interrupted
                    _stop_group(process, reader, termination_grace_seconds)
                    break
                if process.poll() is None:
                    try:
                        process.wait(timeout=min(0.1, remaining))
                    except subprocess.TimeoutExpired:
                        continue
                else:
                    reader.join(timeout=min(0.1, remaining))
        except KeyboardInterrupt:
            interrupted = True
            _stop_group(process, reader, termination_grace_seconds)
        except Exception as exc:
            _stop_group(process, reader, termination_grace_seconds)
            return ProcessResult(
                None, sink.text(), error=_redact_text(str(exc), secret_values)[:500]
            )
        interrupted = interrupted or event.is_set()
        timed_out = timed_out or (not interrupted and monotonic() > deadline)
        return ProcessResult(process.poll(), sink.text(), timed_out, interrupted)


@dataclass
class ContainerState:
    """Allowlisted Docker state used for decisions and artifacts."""

    status: Optional[str] = None
    running: Optional[bool] = None
    exit_code: Optional[int] = None
    oom_killed: Optional[bool] = None
    health: Optional[str] = None
    started_at: Optional[str] = None
    finished_at: Optional[str] = None


@dataclass
class SmokeResult:
    """Primary smoke outcome with secondary evidence kept separate."""

    success: bool
    primary_outcome: str
    error: Optional[str] = None
    cleanup_success: Optional[bool] = None
    log_state: str = "not_attempted"
    state: ContainerState = field(default_factory=ContainerState)
    mapped_port: Optional[int] = None
    artifact_paths: List[str] = field(default_factory=list)
    secondary_errors: List[str] = field(default_factory=list)


ProcessRunner = Callable[..., ProcessResult]


@dataclass(frozen=True)
class _HealthStatus:
    status_code: int


def _streaming_http_get(url: str, *, timeout: float, follow_redirects: bool) -> Any:
    """Read only the health response status, never an unbounded response body."""
    transport = httpx.Timeout(
        connect=min(2, timeout),
        read=min(3, timeout),
        write=min(1, timeout),
        pool=min(1, timeout),
    )
    with httpx.Client(timeout=transport, follow_redirects=follow_redirects) as client:
        with client.stream("GET", url) as response:
            return _HealthStatus(response.status_code)


class RunnerSmoke:
    """Create, probe, diagnose, and remove one owned disposable container."""

    def __init__(
        self,
        process_runner: ProcessRunner = run_bounded_process,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        http_get: Callable[..., Any] = _streaming_http_get,
        token_factory: Callable[[], Any] = uuid.uuid4,
    ) -> None:
        self._run_process = process_runner
        self._monotonic = monotonic
        self._sleep = sleep
        self._http_get = http_get
        self._token_factory = token_factory

    def run(
        self,
        image_reference: str,
        task_definition: Dict[str, Any],
        *,
        pr_number: int,
        sha: str,
        timeout_seconds: int = DEFAULT_SMOKE_TIMEOUT_SECONDS,
        diagnostics_dir: str = DEFAULT_DIAGNOSTICS_DIR,
    ) -> SmokeResult:
        """Run the rendered task locally and require owned cleanup before success."""
        validate_image_reference(image_reference)
        validate_positive_seconds(timeout_seconds, "smoke timeout")
        artifact_dir = validate_diagnostics_directory(diagnostics_dir)
        container = rendered_container(task_definition)
        secrets = configured_secret_values(container.get("environment", []))
        config = self._configuration(task_definition, container, secrets)
        deadline = self._monotonic() + timeout_seconds
        interrupted = threading.Event()
        name = f"showtime-smoke-{self._token_factory().hex}"
        token = self._token_factory().hex
        container_id: Optional[str] = None
        result = SmokeResult(False, "not_started")

        env_path: Optional[str] = None
        create_attempted = False
        recovery_attempted = False
        create = ProcessResult(None)

        def own_env_file(path: str) -> None:
            """Track the allocated secret file before any write can fail."""
            nonlocal env_path
            env_path = path

        with _scoped_interrupt_handlers(interrupted):
            try:
                pull = self._command(
                    ["docker", "pull", "--platform", "linux/amd64", image_reference],
                    deadline,
                    secrets,
                    interrupted,
                )
                if not pull.success:
                    result = self._process_failure("pull", pull)
                else:
                    env_path = self._write_env_file(config["environment"], on_created=own_env_file)
                    create_attempted = True
                    create = self._command(
                        self._create_command(image_reference, name, token, env_path, config),
                        deadline,
                        secrets,
                        interrupted,
                    )
                    try:
                        _unlink_secret_file(env_path)
                        env_path = None
                    except OSError as exc:
                        env_cleanup_error = (
                            f"temporary environment cleanup failed: {type(exc).__name__}"
                        )
                        if not create.success:
                            result = self._process_failure("create", create)
                            result.secondary_errors.append(env_cleanup_error)
                        else:
                            result = SmokeResult(False, "docker_command_failure", env_cleanup_error)
                    recovery_attempted = True
                    container_id, ownership_error = self._recover_container(
                        create,
                        name,
                        token,
                        image_reference,
                        self._monotonic() + COMMAND_TIMEOUT_SECONDS,
                        secrets,
                        threading.Event(),
                    )
                    if container_id is None:
                        if result.primary_outcome == "not_started":
                            result = self._process_failure("create", create)
                        result.cleanup_success = False
                        if ownership_error:
                            result.secondary_errors.append(ownership_error)
                    elif result.primary_outcome == "not_started":
                        if create.interrupted or interrupted.is_set():
                            result = SmokeResult(
                                False, "interrupted", "runner smoke was interrupted"
                            )
                        else:
                            result = self._start_and_probe(
                                container_id,
                                config["port"],
                                deadline,
                                secrets,
                                interrupted,
                            )
            except (Exception, KeyboardInterrupt) as exc:
                if isinstance(exc, KeyboardInterrupt):
                    interrupted.set()
                    result = SmokeResult(False, "interrupted", "runner smoke was interrupted")
                else:
                    result = SmokeResult(
                        False, "docker_command_failure", _redact_text(str(exc), secrets)[:500]
                    )
            finally:
                # Temporary-file and evidence failures cannot abandon the owned resource.
                if env_path is not None:
                    try:
                        _unlink_secret_file(env_path)
                    except OSError as exc:
                        result.secondary_errors.append(
                            f"environment cleanup failed: {type(exc).__name__}"
                        )
                        if result.success:
                            result.success = False
                            result.error = "temporary environment cleanup failed"
                if create_attempted and not recovery_attempted:
                    try:
                        container_id, ownership_error = self._recover_container(
                            create,
                            name,
                            token,
                            image_reference,
                            self._monotonic() + COMMAND_TIMEOUT_SECONDS,
                            secrets,
                            threading.Event(),
                        )
                        if ownership_error:
                            result.secondary_errors.append(ownership_error)
                    except Exception as exc:
                        result.secondary_errors.append(
                            f"ownership recovery failed: {type(exc).__name__}"
                        )
                    if container_id is None:
                        result.cleanup_success = False
                if container_id is not None:
                    post_event = threading.Event()
                    collected = False
                    if not result.success:
                        collected = True
                        self._best_effort_evidence(
                            result, container_id, deadline, secrets, post_event
                        )
                    try:
                        cleanup_success, cleanup_error = self._cleanup(
                            container_id,
                            token,
                            image_reference,
                            secrets,
                            post_event,
                        )
                    except Exception as exc:
                        cleanup_success = False
                        cleanup_error = f"owned cleanup failed: {type(exc).__name__}"
                    result.cleanup_success = cleanup_success
                    if cleanup_error:
                        result.secondary_errors.append(cleanup_error)
                    if result.success and not cleanup_success:
                        result.success = False
                        result.error = "runner smoke was healthy but owned cleanup failed"
                    if not result.success and not collected:
                        self._best_effort_evidence(
                            result, container_id, deadline, secrets, post_event
                        )
            if interrupted.is_set() and result.success:
                result.success = False
                result.primary_outcome = "interrupted"
                result.error = "runner smoke was interrupted"
            if not result.success:
                self._write_artifacts(result, artifact_dir, pr_number, sha, image_reference)
        return result

    def _best_effort_evidence(
        self,
        result: SmokeResult,
        container_id: str,
        deadline: float,
        secrets: Sequence[str],
        interrupted: threading.Event,
    ) -> None:
        """Keep optional diagnostic failures secondary to the startup result."""
        try:
            self._collect_evidence(result, container_id, deadline, secrets, interrupted)
        except Exception as exc:
            result.log_state = "unavailable"
            result.secondary_errors.append(f"diagnostic capture failed: {type(exc).__name__}")

    def _configuration(
        self,
        task_definition: Dict[str, Any],
        container: Dict[str, Any],
        secrets: Sequence[str],
    ) -> Dict[str, Any]:
        cpu = int(task_definition.get("cpu", 0))
        memory = int(task_definition.get("memory", 0))
        if cpu <= 0 or memory <= 0:
            raise ValueError("task CPU and memory must be positive")
        environment = list(container.get("environment", []))
        for entry in environment:
            name, value = str(entry.get("name", "")), str(entry.get("value", ""))
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
                raise ValueError("invalid environment variable name")
            if "\n" in value or "\r" in value or "\x00" in value:
                raise ValueError(f"environment value for {name} contains a forbidden character")
        entrypoint = [str(value) for value in container.get("entryPoint", [])]
        command = [str(value) for value in container.get("command", [])]
        if any(secret and secret in value for value in entrypoint + command for secret in secrets):
            raise ValueError("packaged command contains a configured secret")
        mappings = container.get("portMappings", [])
        if not mappings or not isinstance(mappings[0].get("containerPort"), int):
            raise ValueError("packaged container port is missing")
        return {
            "cpu": cpu / 1024,
            "memory": memory,
            "environment": environment,
            "entrypoint": entrypoint,
            "command": command,
            "port": mappings[0]["containerPort"],
        }

    def _create_command(
        self,
        image: str,
        name: str,
        token: str,
        env_path: str,
        config: Dict[str, Any],
    ) -> List[str]:
        command = [
            "docker",
            "create",
            "--platform",
            "linux/amd64",
            "--name",
            name,
            "--label",
            f"{OWNER_LABEL}={token}",
            "--env-file",
            env_path,
            "--cpus",
            str(config["cpu"]),
            "--memory",
            f"{config['memory']}m",
            "--memory-swap",
            f"{config['memory']}m",
            "--publish",
            f"127.0.0.1::{config['port']}",
        ]
        entrypoint = config["entrypoint"]
        if entrypoint:
            command.extend(["--entrypoint", entrypoint[0]])
        command.append(image)
        command.extend(entrypoint[1:] + config["command"])
        return command

    def _command(
        self,
        command: Sequence[str],
        deadline: float,
        secrets: Sequence[str],
        interrupted: threading.Event,
    ) -> ProcessResult:
        remaining = deadline - self._monotonic()
        if interrupted.is_set():
            return ProcessResult(None, interrupted=True, error="interrupted")
        if remaining <= 0:
            return ProcessResult(None, timed_out=True, error="deadline exhausted")
        try:
            return self._run_process(
                command,
                min(COMMAND_TIMEOUT_SECONDS, remaining),
                secret_values=secrets,
                interrupt_event=interrupted,
            )
        except Exception as exc:
            return ProcessResult(None, error=_redact_text(str(exc), secrets)[:500])

    def _recover_container(
        self,
        create: ProcessResult,
        name: str,
        token: str,
        image: str,
        deadline: float,
        secrets: Sequence[str],
        interrupted: threading.Event,
    ) -> Tuple[Optional[str], Optional[str]]:
        candidate = create.output.strip().splitlines()[-1] if create.output.strip() else name
        identity = self._inspect_identity(candidate, deadline, secrets, interrupted)
        if identity is None and candidate != name:
            identity = self._inspect_identity(name, deadline, secrets, interrupted)
        if identity is None:
            return None, "container ownership could not be verified; nothing was removed"
        container_id, observed_token, observed_image = identity
        if observed_token != token or observed_image != image:
            return None, "container ownership mismatch; nothing was removed"
        return container_id, None

    def _inspect_identity(
        self,
        target: str,
        deadline: float,
        secrets: Sequence[str],
        interrupted: threading.Event,
    ) -> Optional[Tuple[str, str, str]]:
        template = (
            f'{{{{.Id}}}}\t{{{{index .Config.Labels "{OWNER_LABEL}"}}}}\t{{{{.Config.Image}}}}'
        )
        inspected = self._command(
            ["docker", "inspect", "--format", template, target],
            deadline,
            secrets,
            interrupted,
        )
        if not inspected.success:
            return None
        parts = inspected.output.rstrip("\r\n").split("\t")
        if len(parts) != 3 or not re.fullmatch(r"[0-9a-f]{12,64}", parts[0]):
            return None
        return parts[0], parts[1], parts[2]

    def _start_and_probe(
        self,
        container_id: str,
        container_port: int,
        deadline: float,
        secrets: Sequence[str],
        interrupted: threading.Event,
    ) -> SmokeResult:
        started = self._command(["docker", "start", container_id], deadline, secrets, interrupted)
        if not started.success:
            return self._process_failure("start", started)
        port = self._mapped_port(container_id, container_port, deadline, secrets, interrupted)
        if port is None:
            return SmokeResult(False, "docker_command_failure", "mapped port unavailable")

        result = SmokeResult(False, "timeout", mapped_port=port)
        while self._monotonic() < deadline and not interrupted.is_set():
            state = self._inspect_state(container_id, deadline, secrets, interrupted)
            if state is None:
                return SmokeResult(
                    False, "docker_command_failure", "container state unavailable", mapped_port=port
                )
            result.state = state
            if state.oom_killed:
                result.primary_outcome = "oom"
                result.error = "runner smoke container was OOM-killed"
                return result
            if state.running is False:
                result.primary_outcome = "nonzero_exit" if state.exit_code else "exited"
                result.error = f"runner smoke container exited with code {state.exit_code}"
                return result
            try:
                remaining = deadline - self._monotonic()
                if remaining < HTTP_TIMEOUT_SECONDS:
                    break
                response = self._http_get(
                    f"http://127.0.0.1:{port}/health",
                    timeout=min(HTTP_TIMEOUT_SECONDS, max(0.001, remaining)),
                    follow_redirects=False,
                )
                if interrupted.is_set() or self._monotonic() > deadline:
                    break
                if response.status_code == 200:
                    return SmokeResult(
                        True,
                        "healthy",
                        state=state,
                        mapped_port=port,
                    )
            except Exception:
                pass
            self._sleep(min(2, max(0, deadline - self._monotonic())))
        if interrupted.is_set():
            return SmokeResult(
                False, "interrupted", "runner smoke was interrupted", mapped_port=port
            )
        result.error = "runner smoke health deadline exhausted"
        return result

    def _mapped_port(
        self,
        container_id: str,
        port: int,
        deadline: float,
        secrets: Sequence[str],
        interrupted: threading.Event,
    ) -> Optional[int]:
        found = self._command(
            ["docker", "port", container_id, f"{port}/tcp"],
            deadline,
            secrets,
            interrupted,
        )
        if not found.success:
            return None
        match = re.fullmatch(r"127\.0\.0\.1:(\d+)", found.output.strip())
        return int(match.group(1)) if match else None

    def _inspect_state(
        self,
        container_id: str,
        deadline: float,
        secrets: Sequence[str],
        interrupted: threading.Event,
    ) -> Optional[ContainerState]:
        template = (
            "{{.State.Status}}\t{{.State.Running}}\t{{.State.ExitCode}}\t"
            "{{.State.OOMKilled}}\t{{if .State.Health}}{{.State.Health.Status}}{{end}}\t"
            "{{.State.StartedAt}}\t{{.State.FinishedAt}}"
        )
        inspected = self._command(
            ["docker", "inspect", "--format", template, container_id],
            deadline,
            secrets,
            interrupted,
        )
        if not inspected.success:
            return None
        parts = inspected.output.rstrip("\r\n").split("\t")
        if len(parts) != 7:
            return None
        try:
            return ContainerState(
                status=parts[0] or None,
                running=parts[1].lower() == "true",
                exit_code=int(parts[2]),
                oom_killed=parts[3].lower() == "true",
                health=parts[4] or None,
                started_at=parts[5] or None,
                finished_at=parts[6] or None,
            )
        except ValueError:
            return None

    def _collect_evidence(
        self,
        result: SmokeResult,
        container_id: str,
        deadline: float,
        secrets: Sequence[str],
        interrupted: threading.Event,
    ) -> None:
        evidence_deadline = self._monotonic() + DIAGNOSTIC_TIMEOUT_SECONDS
        state = self._inspect_state(container_id, evidence_deadline, secrets, interrupted)
        if state is not None:
            result.state = state
        logs = self._command(
            ["docker", "logs", "--tail", str(LOG_LIMIT_LINES), container_id],
            evidence_deadline,
            secrets,
            interrupted,
        )
        if logs.success:
            lines = logs.output.splitlines()[-LOG_LIMIT_LINES:]
            bounded = "\n".join(lines).encode("utf-8")[-LOG_LIMIT_BYTES:]
            result._log_output = bounded.decode("utf-8", errors="ignore")  # type: ignore[attr-defined]
            result.log_state = "captured_nonempty" if bounded else "captured_empty"
        else:
            result.log_state = "unavailable"
            if logs.error:
                result.secondary_errors.append(f"logs: {logs.error}")

    def _cleanup(
        self,
        container_id: str,
        token: str,
        image: str,
        secrets: Sequence[str],
        interrupted: threading.Event,
    ) -> Tuple[bool, Optional[str]]:
        deadline = self._monotonic() + COMMAND_TIMEOUT_SECONDS
        identity = self._inspect_identity(container_id, deadline, secrets, interrupted)
        if identity is None or identity[1:] != (token, image):
            return False, "cleanup ownership verification failed; nothing was removed"
        removed = self._command(
            ["docker", "rm", "--force", container_id], deadline, secrets, interrupted
        )
        if not removed.success:
            return False, f"owned cleanup failed: {removed.error or removed.returncode}"
        return True, None

    def _process_failure(self, operation: str, process: ProcessResult) -> SmokeResult:
        if process.interrupted:
            outcome = "interrupted"
        elif process.timed_out:
            outcome = "timeout"
        else:
            outcome = "docker_command_failure"
        detail = process.error or f"exit code {process.returncode}"
        # The process transport redacts before retaining output or clipping its tail.
        if process.output.strip():
            tail = process.output.strip().encode("utf-8")[-2048:].decode("utf-8", errors="ignore")
            detail = f"{detail}: {tail}"
        return SmokeResult(False, outcome, f"docker {operation} failed: {detail}")

    def _write_env_file(
        self,
        environment: Sequence[Dict[str, str]],
        *,
        on_created: Callable[[str], None],
    ) -> str:
        """Publish file ownership before writing and preserve any primary write error."""
        descriptor, path = tempfile.mkstemp(prefix="showtime-smoke-env-")
        on_created(path)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w") as stream:
                for entry in environment:
                    stream.write(f"{entry['name']}={entry['value']}\n")
        except Exception:
            try:
                os.close(descriptor)
            except OSError:
                pass
            try:
                _unlink_secret_file(path)
            except OSError:
                # The caller owns the path and retries in its outer finalizer.
                pass
            raise
        return path

    def _write_artifacts(
        self,
        result: SmokeResult,
        directory: Path,
        pr_number: int,
        sha: str,
        image: str,
    ) -> None:
        try:
            if directory.is_symlink():
                raise ValueError("smoke diagnostics directory became a symlink")
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            attempt = self._token_factory().hex
            base = directory / f"runner-smoke-pr-{pr_number}-{sha[:7]}-{attempt}"
            payload = {
                "primary_outcome": result.primary_outcome,
                "error": result.error,
                "image_reference": image,
                "mapped_port": result.mapped_port,
                "state": asdict(result.state),
                "log_state": result.log_state,
                "cleanup_success": result.cleanup_success,
                "secondary_errors": result.secondary_errors,
            }
            json_path = str(base.with_suffix(".json"))
            log_path = str(base.with_suffix(".log"))
            _exclusive_write(json_path, json.dumps(payload, indent=2, sort_keys=True) + "\n")
            _exclusive_write(log_path, getattr(result, "_log_output", ""))
            result.artifact_paths.extend([json_path, log_path])
        except Exception as exc:
            result.secondary_errors.append(f"artifact write failed: {type(exc).__name__}")


@contextmanager
def _scoped_interrupt_handlers(event: threading.Event) -> Iterator[None]:
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    previous: Dict[signal.Signals, Any] = {}

    def handle(_signum: int, _frame: Any) -> None:
        event.set()

    try:
        for current_signal in (signal.SIGINT, signal.SIGTERM):
            previous[current_signal] = signal.getsignal(current_signal)
            signal.signal(current_signal, handle)
        yield
    finally:
        for current_signal, handler in previous.items():
            signal.signal(current_signal, handler)


def _unlink_secret_file(path: str) -> None:
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass


def _exclusive_write(path: str, value: str) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        stream.write(value)
