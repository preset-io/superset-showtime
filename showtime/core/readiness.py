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

"""Bounded, task-aware ECS startup readiness and redacted diagnostics."""

import json
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Set

import httpx


def _utc_now() -> datetime:
    """Return an aware wall-clock timestamp for event correlation."""
    return datetime.now(timezone.utc)


DEFAULT_STARTUP_TIMEOUT_SECONDS = 1800
DIAGNOSTIC_TIMEOUT_SECONDS = 30
SDK_REQUEST_ENVELOPE_SECONDS = 7
HTTP_REQUEST_ENVELOPE_SECONDS = 7
VISIBILITY_GRACE_SECONDS = 300
LOG_EVENT_LIMIT = 100
LOG_LINE_LIMIT = 100
LOG_BYTE_LIMIT = 16 * 1024


def validate_startup_timeout_seconds(value: int) -> int:
    """Return a positive integer startup timeout or raise before mutation."""
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError("startup timeout must be a positive integer")
    return value


class BudgetExceeded(RuntimeError):
    """Raised when the original deadline cannot fit another operation."""


class ObservationError(RuntimeError):
    """Represent a normalized AWS observation error without raw payloads."""

    def __init__(self, operation: str, code: str) -> None:
        super().__init__(f"{operation} failed ({code})")
        self.operation = operation
        self.code = code


class TerminalReadinessError(RuntimeError):
    """Represent a readiness state that cannot recover for this attempt."""


@dataclass
class StartupBudget:
    """One immutable monotonic startup deadline with wall-clock correlation."""

    timeout_seconds: int
    monotonic: Callable[[], float] = time.monotonic
    wall_clock: Callable[[], datetime] = _utc_now

    def __post_init__(self) -> None:
        """Capture the original clocks exactly once."""
        validate_startup_timeout_seconds(self.timeout_seconds)
        self.started_monotonic = self.monotonic()
        self.deadline = self.started_monotonic + self.timeout_seconds
        self.started_wall_clock = self.wall_clock()
        self._ended_wall_clock: Optional[datetime] = None

    @property
    def correlation_end(self) -> datetime:
        """Bound event attribution by elapsed startup time and its deadline."""
        return self._ended_wall_clock or self.started_wall_clock + timedelta(
            seconds=min(self.elapsed, self.timeout_seconds)
        )

    def finish(self) -> None:
        """Freeze the startup window before separate diagnostic collection."""
        if self._ended_wall_clock is None:
            self._ended_wall_clock = self.correlation_end

    @property
    def elapsed(self) -> float:
        """Return actual monotonic elapsed seconds."""
        return max(0.0, self.monotonic() - self.started_monotonic)

    @property
    def remaining(self) -> float:
        """Return non-negative seconds remaining on the original deadline."""
        return max(0.0, self.deadline - self.monotonic())

    def require(self, envelope_seconds: int, operation: str) -> None:
        """Refuse to start a request whose transport envelope cannot fit."""
        if self.remaining < envelope_seconds:
            raise BudgetExceeded(
                f"insufficient remaining request budget for {operation}: "
                f"{self.remaining:.1f}s available, {envelope_seconds}s required"
            )

    def reject_late_result(self, operation: str) -> None:
        """Reject a result returned after the immutable deadline."""
        if self.monotonic() > self.deadline:
            raise BudgetExceeded(f"startup deadline exceeded during {operation}")


@dataclass
class DeploymentDiagnostic:
    """Allowlisted ECS deployment state."""

    status: Optional[str] = None
    task_definition_arn: Optional[str] = None
    rollout_state: Optional[str] = None
    desired_count: Optional[int] = None
    running_count: Optional[int] = None
    pending_count: Optional[int] = None


@dataclass
class TaskDiagnostic:
    """Allowlisted ECS task and container failure state."""

    task_arn: str
    task_definition_arn: Optional[str] = None
    last_status: Optional[str] = None
    desired_status: Optional[str] = None
    stop_code: Optional[str] = None
    stopped_reason: Optional[str] = None
    exit_code: Optional[int] = None
    container_reason: Optional[str] = None
    image_digest: Optional[str] = None
    correlated: bool = False


@dataclass
class ServiceEventDiagnostic:
    """Allowlisted ECS service event in the observation window."""

    created_at: Optional[str]
    message: str


@dataclass
class DiagnosticSummary:
    """Typed diagnostic handoff containing no raw SDK response objects."""

    service_name: str
    expected_task_definition_arn: Optional[str]
    elapsed_seconds: float
    primary_error: str
    service_status: Optional[str] = None
    service_task_definition_arn: Optional[str] = None
    deployments: List[DeploymentDiagnostic] = field(default_factory=list)
    tasks: List[TaskDiagnostic] = field(default_factory=list)
    replacement_history: List[str] = field(default_factory=list)
    service_events: List[ServiceEventDiagnostic] = field(default_factory=list)
    api_errors: List[str] = field(default_factory=list)
    log_state: str = "not-attempted"
    log_task_arn: Optional[str] = None
    log_lines: List[str] = field(default_factory=list)
    capture_errors: List[str] = field(default_factory=list)


@dataclass
class ReadinessResult:
    """Typed result of waiting for one exact ECS candidate."""

    success: bool
    ip: Optional[str] = None
    error: Optional[str] = None
    diagnostic: Optional[DiagnosticSummary] = None


def _error_code(exc: Exception) -> str:
    """Extract a normalized provider error code without returning its payload."""
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        error = response.get("Error", {})
        if isinstance(error, dict) and error.get("Code"):
            return re.sub(r"[^A-Za-z0-9_.-]", "", str(error["Code"]))[:80] or "UnknownError"
    return type(exc).__name__


def _safe_event_message(message: Any) -> str:
    """Keep a bounded event message while redacting assignment-like secrets."""
    return _redact_text(str(message), [])[:500]


def _redact_text(value: str, secret_values: Sequence[str]) -> str:
    """Redact known values and common secret-bearing assignments."""
    redacted = value
    for secret in secret_values:
        if secret:
            redacted = redacted.replace(secret, "[REDACTED]")
    pattern = re.compile(
        r"(?i)([\"']?(?:[A-Za-z_][\w.-]*[_.-])?"
        r"(?:password|passwd|secret|token|api[_-]?key|access[_-]?key|authorization|credentials?)"
        r"(?:[_.-][\w.-]+)?[\"']?\s*[:=]\s*)"
        r"(\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'|(?:Bearer|Basic)\s+[^\s,;}\]]+|[^\s,;}\]]+)"
    )
    return pattern.sub(lambda match: f'{match.group(1)}"[REDACTED]"', redacted)


def render_diagnostic_summary(
    summary: DiagnosticSummary, secret_values: Optional[Sequence[str]] = None
) -> str:
    """Render only allowlisted diagnostic fields through one redaction boundary."""
    secrets = list(secret_values or [])
    lines = [
        "Showtime startup diagnostics:",
        f"service: {summary.service_name}",
        f"expected task definition: {summary.expected_task_definition_arn or 'unknown'}",
        f"elapsed: {summary.elapsed_seconds:.1f}s",
        f"failure: {summary.primary_error}",
        f"service state: {summary.service_status or 'unknown'}",
        f"observed task definition: {summary.service_task_definition_arn or 'unknown'}",
    ]
    for deployment in summary.deployments:
        lines.append(
            "deployment: "
            f"status={deployment.status or 'unknown'} "
            f"rollout={deployment.rollout_state or 'unreported'} "
            f"desired={deployment.desired_count} running={deployment.running_count} "
            f"pending={deployment.pending_count} definition={deployment.task_definition_arn}"
        )
    for task in summary.tasks:
        lines.append(
            "task: "
            f"arn={task.task_arn} last={task.last_status} desired={task.desired_status} "
            f"stop={task.stop_code} reason={task.stopped_reason} exit={task.exit_code} "
            f"container_reason={task.container_reason} digest={task.image_digest} "
            f"correlated={task.correlated}"
        )
    lines.extend(f"replacement: {item}" for item in summary.replacement_history)
    lines.extend(
        f"service event: {event.created_at or 'unknown'} {event.message}"
        for event in summary.service_events
    )
    lines.extend(f"api error: {item}" for item in summary.api_errors)
    lines.append(f"logs: {summary.log_state}")
    if summary.log_task_arn:
        lines.append(f"log task: {summary.log_task_arn}")
    lines.extend(f"log: {item}" for item in summary.log_lines)
    lines.extend(f"diagnostic capture: {item}" for item in summary.capture_errors)
    return "\n".join(_redact_text(line, secrets) for line in lines)


class ECSReadinessObserver:
    """Observe strict ECS and HTTP health within one caller-owned budget."""

    def __init__(
        self,
        *,
        ecs_client: Any,
        ec2_client: Any,
        logs_client: Optional[Any],
        cluster: str,
        http_client: Optional[Any] = None,
        monotonic: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], datetime] = _utc_now,
        sleep: Callable[[float], None] = time.sleep,
        secret_values: Optional[Sequence[str]] = None,
    ) -> None:
        self.ecs = ecs_client
        self.ec2 = ec2_client
        self.logs = logs_client
        self.cluster = cluster
        self.monotonic = monotonic
        self.wall_clock = wall_clock
        self.sleep = sleep
        self.secret_values = list(secret_values or configured_secret_values())
        self._reported: Set[str] = set()
        self._visibility_pending = False
        self.http = http_client or httpx.Client(
            timeout=httpx.Timeout(connect=2, read=3, write=1, pool=1),
            follow_redirects=False,
        )
        self._service: Optional[Dict[str, Any]] = None
        self._tasks: Dict[str, Dict[str, Any]] = {}
        self._live_observed: Set[str] = set()
        self._selected_task_arn: Optional[str] = None
        self._replacement_history: List[str] = []
        self._api_errors: List[str] = []
        self._historical_partial = False
        self._expected_seen = False
        self._identity_visibility_started: Dict[str, float] = {}
        self._service_name = ""
        self._expected_definition = ""
        self._diagnostic_startup_budget: Optional[StartupBudget] = None

    def set_candidate(self, service_name: str, expected_definition: str) -> None:
        """Bind diagnostic identity before the first candidate mutation."""
        self._service_name = service_name
        self._expected_definition = expected_definition

    def record_api_error(self, operation: str, code: str) -> None:
        """Retain normalized mutation errors in the typed diagnostic handoff."""
        self._api_errors.append(f"{operation}:{code}")

    def _report(self, message: str) -> None:
        """Report state transitions once through the secret redaction boundary."""
        safe = _redact_text(message, self.secret_values)
        if safe not in self._reported:
            self._reported.add(safe)
            print(f"Showtime startup: {safe}")

    def _call(
        self,
        budget: StartupBudget,
        operation: str,
        function: Callable[..., Any],
        **kwargs: Any,
    ) -> Any:
        """Run one SDK request only when its finite envelope fits."""
        budget.require(SDK_REQUEST_ENVELOPE_SECONDS, operation)
        try:
            response = function(**kwargs)
        except Exception as exc:
            code = _error_code(exc)
            self._api_errors.append(f"{operation}:{code}")
            raise ObservationError(operation, code) from exc
        budget.reject_late_result(operation)
        return response

    def _visibility_wait_allowed(self, key: str, budget: StartupBudget) -> bool:
        """Return whether an explicit eventual-consistency grace remains."""
        started = self._identity_visibility_started.setdefault(key, budget.elapsed)
        allowance = min(VISIBILITY_GRACE_SECONDS, float(budget.timeout_seconds))
        allowed = budget.elapsed - started < allowance and budget.remaining > 0
        if allowed:
            self._visibility_pending = True
            self._report(f"waiting for visibility: {key}")
        return allowed

    def _describe_service(self, budget: StartupBudget) -> Optional[Dict[str, Any]]:
        """Read and validate the candidate service identity."""
        try:
            response = self._call(
                budget,
                "DescribeServices",
                self.ecs.describe_services,
                cluster=self.cluster,
                services=[self._service_name],
            )
        except ObservationError as exc:
            if exc.code in ("ServiceNotFoundException", "ServiceNotFound", "MISSING"):
                if not self._expected_seen and self._visibility_wait_allowed("service", budget):
                    self._report("waiting for candidate service visibility")
                    return None
                raise TerminalReadinessError(
                    "candidate service visibility allowance exhausted"
                ) from exc
            raise
        services = response.get("services", [])
        failures = response.get("failures", [])
        if failures and not all(item.get("reason") == "MISSING" for item in failures):
            code = str(failures[0].get("reason", "UNKNOWN"))
            raise ObservationError("DescribeServices", code)
        if len(services) != 1:
            if not self._expected_seen and self._visibility_wait_allowed("service", budget):
                self._report("waiting for candidate service visibility")
                return None
            raise TerminalReadinessError("candidate service visibility allowance exhausted")

        service: Dict[str, Any] = dict(services[0])
        observed_definition = service.get("taskDefinition")
        if observed_definition != self._expected_definition:
            if not self._expected_seen and self._visibility_wait_allowed("service", budget):
                return None
            raise TerminalReadinessError(
                "candidate attempt was superseded by another task definition"
            )
        self._expected_seen = True
        self._service = service
        self._report(
            f"service {self._service_name}: status={service.get('status')} "
            f"running={service.get('runningCount')} pending={service.get('pendingCount')}"
        )
        for event in service.get("events", []):
            created = event.get("createdAt")
            if isinstance(created, datetime) and created >= budget.started_wall_clock:
                self._report(f"service event: {event.get('message', '')}")
        status = service.get("status")
        if status in ("DRAINING", "INACTIVE"):
            raise TerminalReadinessError(f"candidate service entered terminal state {status}")
        deployments = service.get("deployments", [])
        for deployment in deployments:
            if (
                deployment.get("taskDefinition") == self._expected_definition
                and deployment.get("rolloutState") == "FAILED"
            ):
                raise TerminalReadinessError("candidate deployment rollout FAILED")
        return service

    def _list_task_arns(
        self, desired_status: str, budget: StartupBudget, *, historical: bool
    ) -> List[str]:
        """Page one desired-status task inventory without requesting PENDING."""
        arns: List[str] = []
        next_token: Optional[str] = None
        while True:
            kwargs: Dict[str, Any] = {
                "cluster": self.cluster,
                "serviceName": self._service_name,
                "desiredStatus": desired_status,
            }
            if next_token:
                kwargs["nextToken"] = next_token
            try:
                response = self._call(budget, "ListTasks", self.ecs.list_tasks, **kwargs)
            except ObservationError:
                if not historical:
                    raise
                self._historical_partial = True
                return arns
            arns.extend(str(arn) for arn in response.get("taskArns", []))
            next_token = response.get("nextToken")
            if not next_token:
                return arns

    def _describe_task_arns(
        self, arns: Sequence[str], budget: StartupBudget, *, historical: bool
    ) -> List[Dict[str, Any]]:
        """Describe task identities in API-sized batches."""
        tasks: List[Dict[str, Any]] = []
        for start in range(0, len(arns), 100):
            try:
                response = self._call(
                    budget,
                    "DescribeTasks",
                    self.ecs.describe_tasks,
                    cluster=self.cluster,
                    tasks=list(arns[start : start + 100]),
                )
            except ObservationError:
                if not historical:
                    raise
                self._historical_partial = True
                continue
            returned = response.get("tasks", [])
            requested = set(arns[start : start + 100])
            if any(task.get("taskArn") not in requested for task in returned):
                self.record_api_error("DescribeTasks", "IdentityMismatch")
                if historical:
                    self._historical_partial = True
                    continue
                raise ObservationError("DescribeTasks", "IdentityMismatch")
            failures = response.get("failures", [])
            if failures:
                codes = [str(item.get("reason", "UNKNOWN")) for item in failures]
                self._api_errors.extend(f"DescribeTasks:{code}" for code in codes)
                if not historical:
                    missing = [item for item in failures if item.get("reason") == "MISSING"]
                    if len(missing) == len(failures) and all(
                        self._visibility_wait_allowed(f"task:{item.get('arn', 'unknown')}", budget)
                        for item in missing
                    ):
                        self._report("waiting for candidate task visibility")
                        return []
                    if len(missing) == len(failures):
                        raise TerminalReadinessError(
                            "candidate task visibility allowance exhausted"
                        )
                    raise ObservationError("DescribeTasks", codes[0])
                self._historical_partial = True
            tasks.extend(response.get("tasks", []))
        return tasks

    def _remember_tasks(self, tasks: Sequence[Dict[str, Any]], budget: StartupBudget) -> None:
        """Retain identity-bound snapshots and report current task changes."""
        for task in tasks:
            task_arn = task.get("taskArn")
            if not task_arn:
                continue
            self._tasks[str(task_arn)] = task
            if not self._task_diagnostic(
                task, self._diagnostic_startup_budget or budget
            ).correlated:
                continue
            container: Dict[str, Any] = next(iter(task.get("containers", [])), {})
            self._report(
                f"task {task_arn}: last={task.get('lastStatus')} "
                f"desired={task.get('desiredStatus')} exit={container.get('exitCode')} "
                f"stop={task.get('stopCode')} reason={task.get('stoppedReason')}"
            )

    def _observe_tasks(self, budget: StartupBudget) -> List[Dict[str, Any]]:
        """Read live identity before spending time on optional stopped history."""
        arns = self._list_task_arns("RUNNING", budget, historical=False)
        running = self._describe_task_arns(arns, budget, historical=False)
        if self._diagnostic_startup_budget is None:
            self._live_observed.update(
                str(task["taskArn"]) for task in running if task.get("taskArn")
            )
        self._remember_tasks(running, budget)
        return running

    def _observe_stopped(self, budget: StartupBudget, *, during_startup: bool = False) -> None:
        """Bound optional history while preserving time for the next live probe."""
        history_budget = budget
        if during_startup:
            # Reserve the polling interval and one full request envelope.
            allowance = min(15, int(budget.remaining) - 22)
            if allowance < SDK_REQUEST_ENVELOPE_SECONDS:
                self._historical_partial = True
                return
            history_budget = StartupBudget(allowance, budget.monotonic, budget.wall_clock)
            history_budget.deadline = min(history_budget.deadline, budget.deadline)
        try:
            arns = self._list_task_arns("STOPPED", history_budget, historical=True)
            stopped = self._describe_task_arns(arns, history_budget, historical=True)
            self._remember_tasks(stopped, budget)
        except BudgetExceeded:
            self._historical_partial = True

    def _stable(self, service: Dict[str, Any]) -> bool:
        """Apply the accepted concrete ServicesStable-compatible predicate."""
        if (
            service.get("status") != "ACTIVE"
            or service.get("taskDefinition") != self._expected_definition
        ):
            return False
        desired = service.get("desiredCount")
        if not isinstance(desired, int) or desired <= 0:
            return False
        if service.get("runningCount") != desired or service.get("pendingCount") != 0:
            return False
        deployments = service.get("deployments")
        if not isinstance(deployments, list) or len(deployments) != 1:
            return False
        primary = deployments[0]
        if primary.get("status") != "PRIMARY":
            return False
        if primary.get("taskDefinition") != self._expected_definition:
            return False
        if primary.get("desiredCount") != desired or primary.get("runningCount") != desired:
            return False
        if primary.get("pendingCount") != 0:
            return False
        return primary.get("rolloutState") in (None, "COMPLETED")

    def _select_task(
        self, running: Sequence[Dict[str, Any]], service: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        """Select the sole exact-definition live RUNNING task."""
        candidates = [
            task
            for task in running
            if task.get("taskDefinitionArn") == self._expected_definition
            and task.get("lastStatus") == "RUNNING"
            and task.get("desiredStatus") == "RUNNING"
        ]
        if len(running) != 1 or len(candidates) != 1 or service.get("runningCount") != 1:
            return None
        task_arn = str(candidates[0].get("taskArn"))
        if task_arn != self._selected_task_arn:
            prior = self._selected_task_arn or "none"
            self._replacement_history.append(f"{prior} -> {task_arn}")
            self._report(f"selected task changed: {prior} -> {task_arn}")
            self._selected_task_arn = task_arn
        return candidates[0]

    @staticmethod
    def _eni_id(task: Dict[str, Any]) -> Optional[str]:
        """Extract an attached network-interface identity."""
        for attachment in task.get("attachments", []):
            for detail in attachment.get("details", []):
                if detail.get("name") == "networkInterfaceId":
                    return str(detail.get("value"))
        return None

    def _task_ip(self, task: Dict[str, Any], budget: StartupBudget) -> Optional[str]:
        """Resolve the selected task's current public IP."""
        task_arn = str(task.get("taskArn"))
        eni_id = self._eni_id(task)
        if not eni_id:
            if self._visibility_wait_allowed(f"task:{task_arn}", budget):
                return None
            raise TerminalReadinessError("task network attachment visibility allowance exhausted")
        try:
            response = self._call(
                budget,
                "DescribeNetworkInterfaces",
                self.ec2.describe_network_interfaces,
                NetworkInterfaceIds=[eni_id],
            )
        except ObservationError as exc:
            if exc.code in ("InvalidNetworkInterfaceID.NotFound", "MISSING"):
                if self._visibility_wait_allowed(f"eni:{eni_id}", budget):
                    return None
                raise TerminalReadinessError(
                    "network interface visibility allowance exhausted"
                ) from exc
            raise
        interfaces = response.get("NetworkInterfaces", [])
        if not interfaces:
            if self._visibility_wait_allowed(f"eni:{eni_id}", budget):
                return None
            raise TerminalReadinessError("network interface visibility allowance exhausted")
        public_ip = interfaces[0].get("Association", {}).get("PublicIp")
        if public_ip:
            self._report(f"task {task_arn} endpoint: {public_ip}:8080")
        return str(public_ip) if public_ip else None

    def _probe(self, ip: str, budget: StartupBudget) -> bool:
        """Probe strict /health using response headers only."""
        budget.require(HTTP_REQUEST_ENVELOPE_SECONDS, "HTTP health probe")
        try:
            with self.http.stream(
                "GET", f"http://{ip}:8080/health", follow_redirects=False
            ) as response:
                status_code = response.status_code
        except Exception as exc:
            self._api_errors.append(f"HTTP:{type(exc).__name__}")
            return False
        budget.reject_late_result("HTTP health probe")
        return status_code == 200

    def _sleep(self, budget: StartupBudget) -> None:
        """Sleep for no more than the remaining original budget."""
        if budget.remaining <= 0:
            raise BudgetExceeded("startup deadline exhausted")
        delay = 5.0 if self._visibility_pending else 15.0
        self._visibility_pending = False
        self.sleep(min(delay, budget.remaining))

    def wait(
        self, service_name: str, expected_task_definition_arn: str, budget: StartupBudget
    ) -> ReadinessResult:
        """Wait for exact ECS stability and strict HTTP health."""
        self.set_candidate(service_name, expected_task_definition_arn)
        error = "startup readiness failed"
        try:
            while True:
                service = self._describe_service(budget)
                if service is None:
                    self._sleep(budget)
                    continue
                running = self._observe_tasks(budget)
                if not self._stable(service):
                    self._observe_stopped(budget, during_startup=True)
                    self._sleep(budget)
                    continue
                task = self._select_task(running, service)
                if task is None:
                    self._observe_stopped(budget, during_startup=True)
                    self._sleep(budget)
                    continue
                ip = self._task_ip(task, budget)
                if ip is None or not self._probe(ip, budget):
                    self._observe_stopped(budget, during_startup=True)
                    self._sleep(budget)
                    continue
                return ReadinessResult(True, ip=ip)
        except (BudgetExceeded, ObservationError, TerminalReadinessError) as exc:
            error = str(exc)
        return ReadinessResult(False, error=error, diagnostic=self._summary(error, budget))

    def _task_diagnostic(self, task: Dict[str, Any], budget: StartupBudget) -> TaskDiagnostic:
        """Copy only allowlisted fields from a task response."""
        task_arn = str(task.get("taskArn", "unknown"))
        created_at = task.get("createdAt")
        within_window = (
            isinstance(created_at, datetime)
            and budget.started_wall_clock <= created_at <= budget.correlation_end
        )
        correlated = task.get("taskDefinitionArn") == self._expected_definition and (
            task_arn in self._live_observed or within_window
        )
        containers = task.get("containers", [])
        container = containers[0] if containers else {}
        return TaskDiagnostic(
            task_arn=task_arn,
            task_definition_arn=task.get("taskDefinitionArn"),
            last_status=task.get("lastStatus"),
            desired_status=task.get("desiredStatus"),
            stop_code=task.get("stopCode"),
            stopped_reason=_redact_text(str(task.get("stoppedReason", "")), self.secret_values),
            exit_code=container.get("exitCode"),
            container_reason=_redact_text(str(container.get("reason", "")), self.secret_values),
            image_digest=container.get("imageDigest"),
            correlated=correlated,
        )

    def _summary(self, primary_error: str, budget: StartupBudget) -> DiagnosticSummary:
        """Build a typed summary from snapshots collected during this run."""
        budget.finish()
        service = self._service or {}
        deployments = [
            DeploymentDiagnostic(
                status=item.get("status"),
                task_definition_arn=item.get("taskDefinition"),
                rollout_state=item.get("rolloutState"),
                desired_count=item.get("desiredCount"),
                running_count=item.get("runningCount"),
                pending_count=item.get("pendingCount"),
            )
            for item in service.get("deployments", [])
        ]
        events = []
        for item in service.get("events", []):
            created_at = item.get("createdAt")
            if isinstance(created_at, datetime) and not (
                budget.started_wall_clock <= created_at <= budget.correlation_end
            ):
                continue
            events.append(
                ServiceEventDiagnostic(
                    created_at=created_at.isoformat() if isinstance(created_at, datetime) else None,
                    message=_redact_text(
                        _safe_event_message(item.get("message", "")), self.secret_values
                    ),
                )
            )
        capture_errors = []
        if self._historical_partial:
            capture_errors.append("historical task evidence is partial")
        return DiagnosticSummary(
            service_name=self._service_name,
            expected_task_definition_arn=self._expected_definition,
            elapsed_seconds=budget.elapsed,
            primary_error=_redact_text(primary_error, self.secret_values),
            service_status=service.get("status"),
            service_task_definition_arn=service.get("taskDefinition"),
            deployments=deployments,
            tasks=[self._task_diagnostic(task, budget) for task in self._tasks.values()],
            replacement_history=list(self._replacement_history),
            service_events=events,
            api_errors=list(self._api_errors),
            capture_errors=capture_errors,
        )

    def capture_failure(
        self,
        primary_error: str,
        startup_budget: StartupBudget,
        diagnostic_budget: StartupBudget,
    ) -> DiagnosticSummary:
        """Capture bounded post-failure state and at most one correlated log page."""
        startup_budget.finish()
        self._diagnostic_startup_budget = startup_budget
        for capture in (self._describe_service, self._observe_tasks, self._observe_stopped):
            try:
                capture(diagnostic_budget)
            except Exception as exc:
                # One unavailable source must not suppress the remaining evidence.
                self._api_errors.append(f"diagnostic:{capture.__name__}:{type(exc).__name__}")
        summary = self._summary(primary_error, startup_budget)
        self._capture_logs(summary, diagnostic_budget)
        return summary

    def _capture_logs(self, summary: DiagnosticSummary, budget: StartupBudget) -> None:
        """Fetch one bounded log page for the best correlated task."""
        correlated = [task for task in summary.tasks if task.correlated]
        if self.logs is None or not correlated:
            summary.log_state = "not-attempted"
            return
        current = next(
            (
                task
                for task in correlated
                if task.task_arn == self._selected_task_arn and task.last_status == "RUNNING"
            ),
            None,
        )

        def recorded_time(task: TaskDiagnostic) -> float:
            """Order failures by observed stop or creation timestamp."""
            raw = self._tasks.get(task.task_arn, {})
            timestamp = raw.get("stoppedAt") or raw.get("createdAt")
            return timestamp.timestamp() if isinstance(timestamp, datetime) else 0.0

        selected = current or max(correlated, key=recorded_time)
        task_id = selected.task_arn.rsplit("/", 1)[-1]
        summary.log_task_arn = selected.task_arn
        try:
            response = self._call(
                budget,
                "GetLogEvents",
                self.logs.get_log_events,
                logGroupName=packaged_container()["logConfiguration"]["options"]["awslogs-group"],
                logStreamName=(
                    f"{packaged_container()['logConfiguration']['options']['awslogs-stream-prefix']}/"
                    f"{packaged_container()['name']}/{task_id}"
                ),
                limit=LOG_EVENT_LIMIT,
                startFromHead=False,
            )
        except ObservationError as exc:
            summary.log_state = (
                "access-denied"
                if exc.code in ("AccessDenied", "AccessDeniedException")
                else "unavailable"
            )
            summary.capture_errors.append(f"GetLogEvents:{exc.code}")
            return
        except BudgetExceeded:
            summary.log_state = "not-attempted"
            summary.capture_errors.append("insufficient diagnostic request budget for logs")
            return
        messages = [str(item.get("message", "")) for item in response.get("events", [])]
        lines: List[str] = []
        byte_count = 0
        truncated = False
        for message in messages:
            for line in message.splitlines() or [""]:
                encoded = _redact_text(line, self.secret_values).encode("utf-8")
                remaining = LOG_BYTE_LIMIT - byte_count - 1
                if len(lines) >= LOG_LINE_LIMIT or remaining < 0:
                    truncated = True
                    break
                clipped = encoded[:remaining].decode("utf-8", errors="ignore")
                lines.append(clipped)
                byte_count += len(clipped.encode("utf-8")) + 1
                if len(encoded) > remaining:
                    truncated = True
                    break
            if truncated:
                break
        summary.log_lines = lines
        summary.log_state = "available" if lines else "available-empty"
        if truncated:
            summary.capture_errors.append("log output truncated to configured limits")


def packaged_container() -> Dict[str, Any]:
    """Read the packaged container configuration without displaying it."""
    path = Path(__file__).parent.parent / "data" / "ecs-task-definition.json"
    with path.open() as stream:
        return dict(json.load(stream)["containerDefinitions"][0])


def configured_secret_values(
    feature_flags: Optional[Sequence[Dict[str, str]]] = None,
) -> List[str]:
    """Collect sensitive configured values for diagnostic redaction."""
    names = (
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "AWS_SECURITY_TOKEN",
        "GITHUB_TOKEN",
        "SUPERSET_SECRET_KEY",
    )
    values = [value for name in names if (value := os.getenv(name))]
    entries = list(packaged_container().get("environment", [])) + list(feature_flags or [])
    for entry in entries:
        if re.search(r"SECRET|PASSWORD|PASSWD|TOKEN|KEY", entry.get("name", ""), re.I):
            value = entry.get("value")
            if value:
                values.append(str(value))
    return values
