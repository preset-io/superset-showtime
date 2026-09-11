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

"""Regression coverage for startup identity, evidence, and transport ownership."""
import json
from pathlib import Path
from typing import Any, Dict, Tuple
from unittest.mock import Mock, patch

import pytest
from botocore.config import Config
from botocore.exceptions import ClientError

from showtime.core.aws import AWSInterface
from showtime.core.readiness import StartupBudget
from tests.unit.test_phase2_readiness import (
    EXPECTED_DEFINITION,
    SERVICE_NAME,
    TASK_1,
    TASK_2,
    FakeClock,
    FakeHttpClient,
    observer,
    running_task,
    stable_service,
)


def healthy_clients() -> Tuple[Mock, Mock]:
    """Build repeatable live-task responses without AWS calls."""
    ecs, ec2 = Mock(), Mock()
    ecs.describe_services.return_value = {"services": [stable_service()], "failures": []}
    ecs.list_tasks.side_effect = lambda **kw: {
        "taskArns": [TASK_1] if kw["desiredStatus"] == "RUNNING" else []
    }
    ecs.describe_tasks.return_value = {"tasks": [running_task()], "failures": []}
    ec2.describe_network_interfaces.return_value = {
        "NetworkInterfaces": [{"Association": {"PublicIp": "192.0.2.10"}}]
    }
    return ecs, ec2


def test_ambiguous_create_retains_diagnostic_identity() -> None:
    """Exercise the named readiness guarantee with controlled clients."""
    ecs, ec2 = healthy_clients()
    aws = AWSInterface(ecs_client=ecs, ecr_client=Mock(), ec2_client=ec2, http_client_factory=Mock)
    with patch.object(
        aws, "_create_task_definition_with_image_and_flags", return_value=EXPECTED_DEFINITION
    ), patch.object(aws, "_service_exists_any_state", return_value=False), patch.object(
        aws, "_create_ecs_service", return_value=False
    ):
        result = aws.create_environment(1234, "abc123f")
    assert result.service_created is None
    assert result.diagnostic.service_name == result.service_name
    assert result.diagnostic.expected_task_definition_arn == EXPECTED_DEFINITION
    assert ecs.describe_services.call_args.kwargs["services"] == [result.service_name]


def test_invalid_client_fails_before_any_mutation() -> None:
    """Exercise the named readiness guarantee with controlled clients."""
    ecs = Mock()
    ecs.meta.config = Config(connect_timeout=60, read_timeout=60)
    aws = AWSInterface(ecs_client=ecs, ecr_client=Mock(), ec2_client=Mock())
    with patch.object(
        aws, "_create_task_definition_with_image_and_flags", return_value=EXPECTED_DEFINITION
    ) as register, patch.object(aws, "_service_exists_any_state", return_value=False), patch.object(
        aws, "_service_exists", return_value=False
    ):
        result = aws.create_environment(1234, "abc123f", force=True)
    assert not result.success
    register.assert_not_called()
    ecs.delete_service.assert_not_called()
    ecs.create_service.assert_not_called()


@pytest.mark.parametrize("missing_operation", ["service", "task"])
def test_new_identity_visibility_recovers(missing_operation: str) -> None:
    """A transient missing identity recovers within the original budget."""
    clock = FakeClock()
    ecs, ec2 = healthy_clients()
    if missing_operation == "service":
        ecs.describe_services.side_effect = [
            ClientError({"Error": {"Code": "ServiceNotFoundException"}}, "DescribeServices"),
            ecs.describe_services.return_value,
        ]
    else:
        ecs.describe_tasks.side_effect = [
            {"tasks": [], "failures": [{"arn": TASK_1, "reason": "MISSING"}]},
            ecs.describe_tasks.return_value,
        ]
    http = FakeHttpClient([200])
    result = observer(clock, ecs, ec2, http).wait(
        SERVICE_NAME, EXPECTED_DEFINITION, StartupBudget(60, clock.monotonic, clock.wall_clock)
    )
    assert result.success
    assert 0 < clock.seconds < 60


def test_inconsistent_extra_live_task_never_passes_health() -> None:
    """Exercise the named readiness guarantee with controlled clients."""
    clock = FakeClock()
    ecs, ec2 = healthy_clients()
    wrong = running_task(TASK_2)
    wrong["taskDefinitionArn"] = "arn:task-definition/other:1"
    ecs.list_tasks.side_effect = lambda **kw: {
        "taskArns": [TASK_1, TASK_2] if kw["desiredStatus"] == "RUNNING" else []
    }
    ecs.describe_tasks.return_value = {"tasks": [running_task(), wrong], "failures": []}
    http = FakeHttpClient([200])
    result = observer(clock, ecs, ec2, http).wait(
        SERVICE_NAME, EXPECTED_DEFINITION, StartupBudget(7, clock.monotonic, clock.wall_clock)
    )
    assert not result.success
    assert http.urls == []


def test_failure_logs_use_packaged_group() -> None:
    """Exercise the named readiness guarantee with controlled clients."""
    clock = FakeClock()
    ecs, ec2 = healthy_clients()
    logs = Mock()
    logs.get_log_events.return_value = {"events": []}
    obs = observer(clock, ecs, ec2, FakeHttpClient([503]), logs)
    startup = StartupBudget(7, clock.monotonic, clock.wall_clock)
    assert not obs.wait(SERVICE_NAME, EXPECTED_DEFINITION, startup).success
    summary = obs.capture_failure(
        "unhealthy", startup, StartupBudget(30, clock.monotonic, clock.wall_clock)
    )
    template = json.loads(Path("showtime/data/ecs-task-definition.json").read_text())
    expected = template["containerDefinitions"][0]["logConfiguration"]["options"]["awslogs-group"]
    assert summary.log_state == "available-empty"
    assert logs.get_log_events.call_args.kwargs["logGroupName"] == expected


def test_healthy_candidate_is_not_starved_by_stopped_history() -> None:
    """Exercise the named readiness guarantee with controlled clients."""
    clock = FakeClock()
    ecs, ec2 = healthy_clients()

    def inventory(**kwargs: Any) -> Dict[str, Any]:
        """Simulate an arbitrarily long stopped-task inventory."""
        if kwargs["desiredStatus"] == "RUNNING":
            return {"taskArns": [TASK_1]}
        clock.seconds += 7
        return {"taskArns": [], "nextToken": "more-history"}

    ecs.list_tasks.side_effect = inventory
    http = FakeHttpClient([200])
    result = observer(clock, ecs, ec2, http).wait(
        SERVICE_NAME, EXPECTED_DEFINITION, StartupBudget(30, clock.monotonic, clock.wall_clock)
    )
    assert result.success
    assert result.ip == "192.0.2.10"


def test_startup_healthy_at_minute_29() -> None:
    """Exercise the named readiness guarantee with controlled clients."""
    clock = FakeClock()
    ecs, ec2 = healthy_clients()

    class SlowHealthyHttp(FakeHttpClient):
        def stream(self, method: str, url: str, **kwargs: Any) -> Any:
            """Serve a healthy header only after minute 29."""
            self.statuses.append(200 if clock.seconds >= 1740 else 503)
            return super().stream(method, url, **kwargs)

    http = SlowHealthyHttp([])
    result = observer(clock, ecs, ec2, http).wait(
        SERVICE_NAME, EXPECTED_DEFINITION, StartupBudget(1800, clock.monotonic, clock.wall_clock)
    )
    assert result.success
    assert 1740 <= clock.seconds < 1800


def test_show_redacts_packaged_and_feature_flag_secret_values(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Task-template and feature-flag secrets cannot escape printed diagnostics."""
    from showtime.core.aws import EnvironmentResult
    from showtime.core.readiness import DiagnosticSummary
    from showtime.core.show import Show

    summary = DiagnosticSummary(
        SERVICE_NAME,
        EXPECTED_DEFINITION,
        12,
        "failed",
        log_state="available",
        log_lines=["super-secret-for-ephemerals feature-secret-sentinel"],
    )
    aws = Mock()
    aws.create_environment.return_value = EnvironmentResult(
        False,
        error="failed",
        diagnostic=summary,
        service_created=True,
        task_definition_arn=EXPECTED_DEFINITION,
    )
    with patch("showtime.core.show.get_interfaces", return_value=(Mock(), aws)):
        with pytest.raises(Exception, match="AWS deployment failed"):
            Show(1234, "abc123f", "deploying").deploy_aws(
                feature_flags=[{"name": "FEATURE_API_TOKEN", "value": "feature-secret-sentinel"}]
            )
    out = capsys.readouterr().out
    assert "super-secret-for-ephemerals" not in out
    assert "feature-secret-sentinel" not in out


def test_aws_startup_exception_does_not_print_raw_payload(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Provider exception payloads are replaced by normalized codes."""
    ecs, ec2 = healthy_clients()
    ecs.create_service.side_effect = ClientError(
        {"Error": {"Code": "AccessDeniedException", "Message": "opaque-payload-sentinel"}},
        "CreateService",
    )
    aws = AWSInterface(ecs_client=ecs, ecr_client=Mock(), ec2_client=ec2, http_client_factory=Mock)
    with patch.object(
        aws, "_create_task_definition_with_image_and_flags", return_value=EXPECTED_DEFINITION
    ), patch.object(aws, "_service_exists_any_state", return_value=False):
        result = aws.create_environment(1234, "abc123f")
    assert not result.success
    assert "opaque-payload-sentinel" not in capsys.readouterr().out
    assert "opaque-payload-sentinel" not in str(result)


def test_startup_http_client_is_closed_on_creation_failure() -> None:
    """Exercise the named readiness guarantee with controlled clients."""
    ecs, ec2 = healthy_clients()
    http = Mock()
    aws = AWSInterface(
        ecs_client=ecs, ecr_client=Mock(), ec2_client=ec2, http_client_factory=lambda: http
    )
    with patch.object(
        aws, "_create_task_definition_with_image_and_flags", return_value=EXPECTED_DEFINITION
    ), patch.object(aws, "_service_exists_any_state", return_value=False), patch.object(
        aws, "_create_ecs_service", return_value=False
    ):
        result = aws.create_environment(1234, "abc123f")
    assert not result.success
    http.close.assert_called_once()


@pytest.mark.parametrize(
    "code, expected_state",
    [("AccessDeniedException", "access-denied"), ("ResourceNotFoundException", "unavailable")],
)
def test_log_api_failure_is_distinct_from_empty(code: str, expected_state: str) -> None:
    """A failed log fetch cannot be represented as an empty successful page."""
    clock = FakeClock()
    ecs, ec2 = healthy_clients()
    logs = Mock()
    logs.get_log_events.side_effect = ClientError(
        {"Error": {"Code": code, "Message": "raw-sentinel"}}, "GetLogEvents"
    )
    obs = observer(clock, ecs, ec2, FakeHttpClient([503]), logs)
    startup = StartupBudget(7, clock.monotonic, clock.wall_clock)
    obs.wait(SERVICE_NAME, EXPECTED_DEFINITION, startup)
    summary = obs.capture_failure(
        "unhealthy", startup, StartupBudget(30, clock.monotonic, clock.wall_clock)
    )
    assert summary.log_state == expected_state
    assert "raw-sentinel" not in str(summary)
    logs.get_log_events.assert_called_once()


def test_stale_stopped_task_is_not_attributed_or_selected_for_logs() -> None:
    """An old exact-definition task found only in STOPPED is historical."""
    from datetime import timedelta

    clock = FakeClock()
    ecs, ec2 = healthy_clients()
    ecs.describe_services.return_value = {"services": [stable_service(rollout_state="IN_PROGRESS")]}
    ecs.list_tasks.side_effect = lambda **kw: {
        "taskArns": [TASK_1] if kw["desiredStatus"] == "STOPPED" else []
    }
    task = running_task()
    task.update(
        lastStatus="STOPPED",
        desiredStatus="STOPPED",
        createdAt=clock.started_at - timedelta(hours=1),
    )
    task["containers"] = [{"exitCode": 137, "reason": "old failure"}]
    ecs.describe_tasks.return_value = {"tasks": [task]}
    logs = Mock()
    obs = observer(clock, ecs, ec2, FakeHttpClient([]), logs)
    startup = StartupBudget(7, clock.monotonic, clock.wall_clock)
    result = obs.wait(SERVICE_NAME, EXPECTED_DEFINITION, startup)
    assert not result.success
    summary = obs.capture_failure(
        "unready", startup, StartupBudget(30, clock.monotonic, clock.wall_clock)
    )
    assert summary.tasks and not summary.tasks[0].correlated
    assert summary.log_state == "not-attempted"
    logs.get_log_events.assert_not_called()


def test_real_replacement_updates_ip_without_resetting_budget() -> None:
    """A stopped first task is correlated and a new healthy task uses its own IP."""
    clock = FakeClock()
    ecs, ec2 = healthy_clients()

    def task_inventory(**kwargs: Any) -> Dict[str, Any]:
        if kwargs["desiredStatus"] == "RUNNING":
            return {"taskArns": [TASK_1 if clock.seconds == 0 else TASK_2]}
        return {"taskArns": [TASK_1] if clock.seconds else []}

    def task_details(**kwargs: Any) -> Dict[str, Any]:
        tasks = []
        for arn in kwargs["tasks"]:
            task = running_task(arn)
            if arn == TASK_1 and clock.seconds:
                task.update(
                    lastStatus="STOPPED", desiredStatus="STOPPED", stoppedReason="OutOfMemoryError"
                )
                task["containers"] = [{"exitCode": 137}]
            tasks.append(task)
        return {"tasks": tasks}

    ecs.list_tasks.side_effect = task_inventory
    ecs.describe_tasks.side_effect = task_details
    ec2.describe_network_interfaces.side_effect = lambda **kw: {
        "NetworkInterfaces": [
            {
                "Association": {
                    "PublicIp": "192.0.2.1"
                    if kw["NetworkInterfaceIds"] == ["eni-1"]
                    else "192.0.2.2"
                }
            }
        ]
    }
    http = FakeHttpClient([503, 200])
    obs = observer(clock, ecs, ec2, http)
    budget = StartupBudget(1800, clock.monotonic, clock.wall_clock)
    result = obs.wait(SERVICE_NAME, EXPECTED_DEFINITION, budget)
    assert result.success and result.ip == "192.0.2.2"
    assert http.urls == ["http://192.0.2.1:8080/health", "http://192.0.2.2:8080/health"]
    assert budget.deadline == 1800 and clock.seconds == 15


def test_production_startup_clients_have_explicit_transport_bounds() -> None:
    """Normal construction creates dedicated no-retry clients and bounded HTTP."""
    from showtime.core.aws import AWSInterface

    configs = []

    def client_factory(service: str, **kwargs: Any) -> Mock:
        client = Mock()
        config = kwargs.get("config")
        client.meta.config = config or Config()
        if config is not None:
            configs.append(config)
        return client

    with patch("showtime.core.aws.boto3.client", side_effect=client_factory):
        AWSInterface()._get_startup_clients()
    assert len(configs) == 3
    assert all(
        config.connect_timeout == 2
        and config.read_timeout == 5
        and config.retries["total_max_attempts"] == 1
        for config in configs
    )
    with AWSInterface._create_http_client() as http:
        assert (http.timeout.connect, http.timeout.read, http.timeout.write, http.timeout.pool) == (
            2,
            3,
            1,
            1,
        )
        assert not http.follow_redirects


@pytest.mark.parametrize("created", [False, True])
@pytest.mark.parametrize("capture_fails", [False, True])
def test_diagnostics_precede_real_orchestration_compensation(
    github_fake: Any,
    created: bool,
    capture_fails: bool,
) -> None:
    """None/True allocation results capture and render before candidate teardown."""
    from contextlib import ExitStack

    from showtime.core.pull_request import PullRequest
    from showtime.core.readiness import ECSReadinessObserver
    from showtime.core.show import Show

    trace = []

    class TracedObserver(ECSReadinessObserver):
        def capture_failure(self, *args: Any, **kwargs: Any) -> Any:
            """Record capture without bypassing the actual diagnostic path."""
            trace.append("capture")
            if capture_fails:
                raise RuntimeError("capture-payload-sentinel")
            return super().capture_failure(*args, **kwargs)

    ecs, ec2 = healthy_clients()
    aws = AWSInterface(
        ecs_client=ecs,
        ecr_client=Mock(),
        ec2_client=ec2,
        http_client_factory=Mock,
        readiness_observer_factory=TracedObserver,
    )
    github = github_fake(["🎪 old123f 🚦 running", "🎪 🎯 old123f"])
    pr = PullRequest(1234, list(github.labels))
    candidate = Show(1234, "abc123f", "building")
    with ExitStack() as stack:
        stack.enter_context(patch("showtime.core.pull_request.get_github", return_value=github))
        stack.enter_context(patch("showtime.core.show.get_interfaces", return_value=(github, aws)))
        stack.enter_context(
            patch(
                "showtime.core.show.render_diagnostic_summary",
                side_effect=lambda *a: trace.append("render") or "safe diagnostic",
            )
        )
        stack.enter_context(patch.object(pr, "_atomic_claim", return_value=True))
        stack.enter_context(
            patch.object(pr, "_determine_action", return_value="create_environment")
        )
        stack.enter_context(patch.object(pr, "_create_new_show", return_value=candidate))
        stack.enter_context(patch.object(candidate, "build_docker"))
        stack.enter_context(
            patch.object(candidate, "stop", side_effect=lambda **kw: trace.append("stop") or True)
        )
        stack.enter_context(
            patch.object(
                aws,
                "_create_task_definition_with_image_and_flags",
                return_value=EXPECTED_DEFINITION,
            )
        )
        stack.enter_context(patch.object(aws, "_service_exists_any_state", return_value=False))
        stack.enter_context(patch.object(aws, "_create_ecs_service", return_value=created))
        stack.enter_context(patch.object(aws, "_deploy_task_definition", return_value=False))
        result = pr.sync("abc123f")
    assert not result.success
    assert trace == ["capture", "render", "stop"]
    assert candidate.service_created is (True if created else None)
    assert "🎪 🎯 old123f" in github.labels
    assert "capture-payload-sentinel" not in str(candidate.diagnostic)


def test_creation_and_update_consume_the_original_budget() -> None:
    """Transport time before observation reduces the remaining readiness allowance."""
    clock = FakeClock()
    ecs, ec2 = healthy_clients()
    http = Mock()
    aws = AWSInterface(
        ecs_client=ecs,
        ecr_client=Mock(),
        ec2_client=ec2,
        http_client_factory=lambda: http,
        monotonic=clock.monotonic,
        wall_clock=clock.wall_clock,
        sleep=clock.sleep,
    )
    ecs.create_service.side_effect = lambda **kw: clock.sleep(20) or {}
    ecs.update_service.side_effect = lambda **kw: clock.sleep(7) or {}
    with patch.object(
        aws, "_create_task_definition_with_image_and_flags", return_value=EXPECTED_DEFINITION
    ), patch.object(aws, "_service_exists_any_state", return_value=False):
        result = aws.create_environment(1234, "abc123f", startup_timeout_seconds=30)
    assert not result.success and result.service_created is True
    assert "request budget" in result.error
    assert clock.seconds == 27
    http.stream.assert_not_called()


def test_log_output_is_bounded_and_reports_truncation() -> None:
    """A large line remains evidence rather than becoming misleading empty logs."""
    clock = FakeClock()
    ecs, ec2 = healthy_clients()
    logs = Mock()
    logs.get_log_events.return_value = {"events": [{"message": "x" * 20000}]}
    obs = observer(clock, ecs, ec2, FakeHttpClient([503]), logs)
    startup = StartupBudget(7, clock.monotonic, clock.wall_clock)
    obs.wait(SERVICE_NAME, EXPECTED_DEFINITION, startup)
    summary = obs.capture_failure(
        "unhealthy", startup, StartupBudget(30, clock.monotonic, clock.wall_clock)
    )
    assert summary.log_state == "available"
    assert len("\n".join(summary.log_lines).encode()) <= 16 * 1024
    assert len(summary.log_lines) <= 100
    assert any("truncated" in item for item in summary.capture_errors)


@pytest.mark.parametrize("kind", ["service", "task", "eni", "permission"])
def test_visibility_expiry_and_permission_failure_remain_distinct(kind: str) -> None:
    """Transient absence has a bounded grace; denied access fails immediately."""
    clock = FakeClock()
    ecs, ec2 = healthy_clients()
    if kind == "service":
        ecs.describe_services.return_value = {"services": [], "failures": [{"reason": "MISSING"}]}
    elif kind == "task":
        ecs.describe_tasks.return_value = {
            "tasks": [],
            "failures": [{"reason": "MISSING", "arn": TASK_1}],
        }
    elif kind == "eni":
        ec2.describe_network_interfaces.side_effect = ClientError(
            {"Error": {"Code": "InvalidNetworkInterfaceID.NotFound"}}, "DescribeNetworkInterfaces"
        )
    else:
        ecs.describe_services.side_effect = ClientError(
            {"Error": {"Code": "AccessDeniedException"}}, "DescribeServices"
        )
    result = observer(clock, ecs, ec2, FakeHttpClient([])).wait(
        SERVICE_NAME, EXPECTED_DEFINITION, StartupBudget(1800, clock.monotonic, clock.wall_clock)
    )
    assert not result.success
    if kind == "permission":
        assert clock.seconds == 0
        assert "AccessDeniedException" in result.error
    else:
        assert clock.seconds == 300
        assert "visibility allowance exhausted" in result.error


@pytest.mark.parametrize("failure", ["FAILED", "DRAINING", "AccessDeniedException"])
def test_terminal_service_failure_does_not_skip_task_and_log_diagnostics(failure: str) -> None:
    """Independent evidence sources remain useful when service readiness is terminal."""
    clock = FakeClock()
    ecs, ec2 = healthy_clients()
    ecs.describe_tasks.return_value = {"tasks": [{**running_task(), "createdAt": clock.started_at}]}
    service = stable_service(rollout_state="FAILED" if failure == "FAILED" else "COMPLETED")
    if failure == "DRAINING":
        service["status"] = "DRAINING"
    ecs.describe_services.return_value = {"services": [service]}
    if failure == "AccessDeniedException":
        ecs.describe_services.side_effect = ClientError(
            {"Error": {"Code": failure}}, "DescribeServices"
        )
    logs = Mock()
    logs.get_log_events.return_value = {"events": [{"message": "startup error evidence"}]}
    obs = observer(clock, ecs, ec2, FakeHttpClient([]), logs)
    startup = StartupBudget(1800, clock.monotonic, clock.wall_clock)
    result = obs.wait(SERVICE_NAME, EXPECTED_DEFINITION, startup)
    assert not result.success and failure in result.error
    summary = obs.capture_failure(
        result.error, startup, StartupBudget(30, clock.monotonic, clock.wall_clock)
    )
    assert summary.primary_error == result.error
    assert ecs.list_tasks.call_count > 0
    assert summary.tasks
    assert summary.log_state == "available"
    assert summary.log_lines == ["startup error evidence"]
