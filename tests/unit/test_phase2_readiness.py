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

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from unittest.mock import Mock

import pytest

from showtime.core.readiness import (
    DEFAULT_STARTUP_TIMEOUT_SECONDS,
    DiagnosticSummary,
    ECSReadinessObserver,
    StartupBudget,
    render_diagnostic_summary,
    validate_startup_timeout_seconds,
)

EXPECTED_DEFINITION = "arn:aws:ecs:us-west-2:123456789012:task-definition/showtime:42"
SERVICE_NAME = "pr-1234-abc123f-service"
TASK_1 = "arn:aws:ecs:us-west-2:123456789012:task/cluster/task-1"
TASK_2 = "arn:aws:ecs:us-west-2:123456789012:task/cluster/task-2"


class FakeClock:
    """Advance monotonic and wall time without real sleeps."""

    def __init__(self) -> None:
        self.seconds = 0.0
        self.started_at = datetime(2026, 1, 1, tzinfo=timezone.utc)

    def monotonic(self) -> float:
        """Return fake monotonic seconds."""
        return self.seconds

    def wall_clock(self) -> datetime:
        """Return fake UTC wall time."""
        from datetime import timedelta

        return self.started_at + timedelta(seconds=self.seconds)

    def sleep(self, seconds: float) -> None:
        """Advance instead of blocking."""
        self.seconds += seconds


class ResponseContext:
    """Streaming response context that records body access."""

    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
        self.body_read = False

    def __enter__(self) -> "ResponseContext":
        """Return the response."""
        return self

    def __exit__(self, *args: Any) -> None:
        """Close the fake response."""

    def read(self) -> bytes:
        """Record forbidden response-body consumption."""
        self.body_read = True
        return b"body"


class FakeHttpClient:
    """Return configured streaming status responses."""

    def __init__(self, statuses: List[int]) -> None:
        self.statuses = statuses
        self.urls: List[str] = []
        self.responses: List[ResponseContext] = []

    def stream(self, method: str, url: str, **kwargs: Any) -> ResponseContext:
        """Create a response without downloading its body."""
        assert method == "GET"
        self.urls.append(url)
        response = ResponseContext(self.statuses.pop(0))
        self.responses.append(response)
        return response

    def close(self) -> None:
        """Close the fake client."""


def stable_service(
    *,
    task_definition: str = EXPECTED_DEFINITION,
    rollout_state: Optional[str] = "COMPLETED",
) -> Dict[str, Any]:
    """Build the exact accepted stable service shape."""
    deployment: Dict[str, Any] = {
        "status": "PRIMARY",
        "taskDefinition": task_definition,
        "desiredCount": 1,
        "runningCount": 1,
        "pendingCount": 0,
    }
    if rollout_state is not None:
        deployment["rolloutState"] = rollout_state
    return {
        "serviceName": SERVICE_NAME,
        "status": "ACTIVE",
        "taskDefinition": task_definition,
        "desiredCount": 1,
        "runningCount": 1,
        "pendingCount": 0,
        "deployments": [deployment],
        "events": [],
    }


def running_task(task_arn: str = TASK_1, *, desired_status: str = "RUNNING") -> Dict[str, Any]:
    """Build a task attached to one test ENI."""
    return {
        "taskArn": task_arn,
        "taskDefinitionArn": EXPECTED_DEFINITION,
        "lastStatus": "RUNNING",
        "desiredStatus": desired_status,
        "createdAt": datetime(2026, 1, 1, 0, 1, tzinfo=timezone.utc),
        "attachments": [
            {
                "type": "ElasticNetworkInterface",
                "details": [{"name": "networkInterfaceId", "value": f"eni-{task_arn[-1]}"}],
            }
        ],
        "containers": [],
    }


def observer(
    clock: FakeClock,
    ecs: Mock,
    ec2: Mock,
    http: FakeHttpClient,
    logs: Optional[Mock] = None,
) -> ECSReadinessObserver:
    """Build an observer with injected clients and clocks."""
    return ECSReadinessObserver(
        ecs_client=ecs,
        ec2_client=ec2,
        logs_client=logs,
        cluster="superset-ci",
        http_client=http,
        monotonic=clock.monotonic,
        wall_clock=clock.wall_clock,
        sleep=clock.sleep,
    )


def configure_single_healthy_cycle(ecs: Mock, ec2: Mock) -> None:
    """Configure one exact stable observation."""
    ecs.describe_services.return_value = {"services": [stable_service()], "failures": []}
    ecs.list_tasks.side_effect = [
        {"taskArns": [TASK_1]},
        {"taskArns": []},
    ]
    ecs.describe_tasks.return_value = {"tasks": [running_task()], "failures": []}
    ec2.describe_network_interfaces.return_value = {
        "NetworkInterfaces": [{"Association": {"PublicIp": "192.0.2.10"}}]
    }


def test_startup_timeout_validation_and_default() -> None:
    """The public timeout default is positive and rejects bool/non-positive values."""
    assert DEFAULT_STARTUP_TIMEOUT_SECONDS == 1800
    assert validate_startup_timeout_seconds(29) == 29
    for value in (0, -1, True, 1.5, "30"):
        with pytest.raises(ValueError, match="positive integer"):
            validate_startup_timeout_seconds(value)  # type: ignore[arg-type]


def test_strict_streaming_health_never_uses_homepage_or_reads_body() -> None:
    """A 503 /health response is non-ready without a homepage fallback."""
    clock = FakeClock()
    ecs, ec2 = Mock(), Mock()
    configure_single_healthy_cycle(ecs, ec2)
    http = FakeHttpClient([503])

    result = observer(clock, ecs, ec2, http).wait(
        SERVICE_NAME, EXPECTED_DEFINITION, StartupBudget(7, clock.monotonic, clock.wall_clock)
    )

    assert result.success is False
    assert http.urls == ["http://192.0.2.10:8080/health"]
    assert not http.responses[0].body_read


@pytest.mark.parametrize(
    "mutate",
    [
        lambda service: service["deployments"][0].update(rolloutState="IN_PROGRESS"),
        lambda service: service["deployments"].append(dict(service["deployments"][0])),
        lambda service: service.update(pendingCount=1),
        lambda service: service["deployments"][0].update(status="ACTIVE"),
        lambda service: service.update(taskDefinition="arn:task-definition/other:1"),
    ],
)
def test_healthy_http_cannot_bypass_incomplete_deployment(mutate: Any) -> None:
    """HTTP 200 is considered only after the strict ECS stability predicate."""
    clock = FakeClock()
    ecs, ec2 = Mock(), Mock()
    service = stable_service()
    mutate(service)
    ecs.describe_services.return_value = {"services": [service], "failures": []}
    ecs.list_tasks.side_effect = [{"taskArns": [TASK_1]}, {"taskArns": []}]
    ecs.describe_tasks.return_value = {"tasks": [running_task()], "failures": []}
    http = FakeHttpClient([200])

    result = observer(clock, ecs, ec2, http).wait(
        SERVICE_NAME, EXPECTED_DEFINITION, StartupBudget(7, clock.monotonic, clock.wall_clock)
    )

    assert result.success is False
    assert http.urls == []


def test_missing_rollout_state_uses_stable_count_compatibility() -> None:
    """Older ECS responses can pass when identity and all stable counts agree."""
    clock = FakeClock()
    ecs, ec2 = Mock(), Mock()
    ecs.describe_services.return_value = {
        "services": [stable_service(rollout_state=None)],
        "failures": [],
    }
    ecs.list_tasks.side_effect = [{"taskArns": [TASK_1]}, {"taskArns": []}]
    ecs.describe_tasks.return_value = {"tasks": [running_task()], "failures": []}
    ec2.describe_network_interfaces.return_value = {
        "NetworkInterfaces": [{"Association": {"PublicIp": "192.0.2.10"}}]
    }

    result = observer(clock, ecs, ec2, FakeHttpClient([200])).wait(
        SERVICE_NAME, EXPECTED_DEFINITION, StartupBudget(30, clock.monotonic, clock.wall_clock)
    )

    assert result.success is True
    assert result.ip == "192.0.2.10"


def test_failed_rollout_is_terminal_without_sleep() -> None:
    """An expected-definition FAILED rollout fails immediately."""
    clock = FakeClock()
    ecs, ec2 = Mock(), Mock()
    ecs.describe_services.return_value = {
        "services": [stable_service(rollout_state="FAILED")],
        "failures": [],
    }

    result = observer(clock, ecs, ec2, FakeHttpClient([])).wait(
        SERVICE_NAME, EXPECTED_DEFINITION, StartupBudget(1800, clock.monotonic, clock.wall_clock)
    )

    assert result.success is False
    assert "FAILED" in (result.error or "")
    assert clock.seconds == 0


def test_task_inventory_paginates_both_states_and_batches_describes() -> None:
    """RUNNING and STOPPED inventories page fully and DescribeTasks stays at 100 IDs."""
    clock = FakeClock()
    ecs, ec2 = Mock(), Mock()
    ecs.describe_services.return_value = {"services": [stable_service()], "failures": []}
    running = [f"arn:task/running-{index}" for index in range(101)]
    stopped = [f"arn:task/stopped-{index}" for index in range(2)]
    ecs.list_tasks.side_effect = [
        {"taskArns": running[:100], "nextToken": "next"},
        {"taskArns": running[100:]},
        {"taskArns": stopped[:1], "nextToken": "stopped-next"},
        {"taskArns": stopped[1:]},
    ]
    ecs.describe_tasks.side_effect = [
        {"tasks": [running_task(arn) for arn in running[:100]], "failures": []},
        {"tasks": [running_task(running[100])], "failures": []},
        {"tasks": [], "failures": []},
    ]

    readiness = observer(clock, ecs, ec2, FakeHttpClient([]))

    def finish_cycle(seconds: float) -> None:
        """End the test after one complete inventory cycle."""
        clock.seconds = 31

    readiness.sleep = finish_cycle
    result = readiness.wait(
        SERVICE_NAME, EXPECTED_DEFINITION, StartupBudget(30, clock.monotonic, clock.wall_clock)
    )

    assert result.success is False
    statuses = [call.kwargs["desiredStatus"] for call in ecs.list_tasks.call_args_list]
    assert statuses == ["RUNNING", "RUNNING", "STOPPED", "STOPPED"]
    assert all(len(call.kwargs["tasks"]) <= 100 for call in ecs.describe_tasks.call_args_list)


def test_desired_stopped_task_cannot_be_selected() -> None:
    """A task already marked desired STOPPED is not probed."""
    clock = FakeClock()
    ecs, ec2 = Mock(), Mock()
    configure_single_healthy_cycle(ecs, ec2)
    ecs.describe_tasks.return_value = {
        "tasks": [running_task(desired_status="STOPPED")],
        "failures": [],
    }
    http = FakeHttpClient([200])

    result = observer(clock, ecs, ec2, http).wait(
        SERVICE_NAME, EXPECTED_DEFINITION, StartupBudget(7, clock.monotonic, clock.wall_clock)
    )

    assert result.success is False
    assert http.urls == []


def test_no_sdk_call_starts_without_full_transport_envelope() -> None:
    """The observer refuses a call when less than seven seconds remain."""
    clock = FakeClock()
    ecs = Mock()

    result = observer(clock, ecs, Mock(), FakeHttpClient([])).wait(
        SERVICE_NAME, EXPECTED_DEFINITION, StartupBudget(6, clock.monotonic, clock.wall_clock)
    )

    assert result.success is False
    assert "request budget" in (result.error or "")
    ecs.describe_services.assert_not_called()


def test_late_sdk_response_is_rejected() -> None:
    """A response arriving after the deadline cannot establish readiness."""
    clock = FakeClock()
    ecs = Mock()

    def late_response(**kwargs: Any) -> Dict[str, Any]:
        clock.seconds = 31
        return {"services": [stable_service()], "failures": []}

    ecs.describe_services.side_effect = late_response
    result = observer(clock, ecs, Mock(), FakeHttpClient([])).wait(
        SERVICE_NAME, EXPECTED_DEFINITION, StartupBudget(30, clock.monotonic, clock.wall_clock)
    )

    assert result.success is False
    assert "deadline" in (result.error or "")


def test_replacement_does_not_reset_original_deadline() -> None:
    """Task churn at minute 25 remains bounded by the original minute-30 deadline."""
    clock = FakeClock()
    ecs, ec2 = Mock(), Mock()
    ecs.describe_services.return_value = {"services": [stable_service()], "failures": []}
    ecs.list_tasks.side_effect = [
        {"taskArns": [TASK_1]},
        {"taskArns": []},
        {"taskArns": [TASK_2]},
        {"taskArns": [TASK_1]},
    ]
    ecs.describe_tasks.side_effect = [
        {"tasks": [running_task(TASK_1)], "failures": []},
        {"tasks": [running_task(TASK_2)], "failures": []},
        {
            "tasks": [
                {
                    **running_task(TASK_1),
                    "lastStatus": "STOPPED",
                    "desiredStatus": "STOPPED",
                    "stopCode": "EssentialContainerExited",
                    "stoppedReason": "OutOfMemoryError",
                    "containers": [{"exitCode": 137, "reason": "OutOfMemoryError"}],
                }
            ],
            "failures": [],
        },
    ]
    ec2.describe_network_interfaces.side_effect = [
        {"NetworkInterfaces": [{"Association": {"PublicIp": "192.0.2.1"}}]},
        {"NetworkInterfaces": [{"Association": {"PublicIp": "192.0.2.2"}}]},
    ]
    http = FakeHttpClient([503, 503])

    def advance(seconds: float) -> None:
        clock.seconds = 1500 if clock.seconds == 0 else 1801

    clock.sleep = advance  # type: ignore[method-assign]
    result = observer(clock, ecs, ec2, http).wait(
        SERVICE_NAME, EXPECTED_DEFINITION, StartupBudget(1800, clock.monotonic, clock.wall_clock)
    )

    assert result.success is False
    assert result.diagnostic is not None
    assert any(TASK_2 in item for item in result.diagnostic.replacement_history)
    assert any(task.exit_code == 137 for task in result.diagnostic.tasks)
    assert http.urls == ["http://192.0.2.1:8080/health", "http://192.0.2.2:8080/health"]
    assert result.diagnostic.elapsed_seconds >= 1800


def test_redaction_boundary_excludes_secrets_and_raw_payloads() -> None:
    """Rendered diagnostics contain only allowlisted fields and redact assignments."""
    summary = DiagnosticSummary(
        service_name=SERVICE_NAME,
        expected_task_definition_arn=EXPECTED_DEFINITION,
        elapsed_seconds=12.5,
        primary_error="startup failed token=top-secret",
        log_state="available",
        log_lines=["PASSWORD=hunter2", "safe line", "AWS_SECRET_ACCESS_KEY=sentinel"],
        capture_errors=["GetLogEvents: AccessDenied"],
    )

    rendered = render_diagnostic_summary(
        summary, secret_values=["top-secret", "hunter2", "sentinel"]
    )

    assert "safe line" in rendered
    assert "[REDACTED]" in rendered
    assert "top-secret" not in rendered
    assert "hunter2" not in rendered
    assert "sentinel" not in rendered
    assert "environment" not in rendered.lower()
