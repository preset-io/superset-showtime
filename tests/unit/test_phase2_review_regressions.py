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

"""Lock independent-review findings to observable startup behavior."""
from typing import Any
from unittest.mock import Mock, patch

import pytest
from botocore.config import Config
from botocore.exceptions import ClientError

from showtime.core.aws import AWSInterface
from showtime.core.readiness import DiagnosticSummary, StartupBudget, render_diagnostic_summary
from tests.unit.test_phase2_parent_regressions import healthy_clients
from tests.unit.test_phase2_readiness import (
    EXPECTED_DEFINITION,
    SERVICE_NAME,
    TASK_1,
    TASK_2,
    FakeClock,
    FakeHttpClient,
    observer,
    running_task,
)


def test_failed_probe_does_not_allow_history_to_starve_recovery() -> None:
    """A first503 followed by200 succeeds despite slow endless stopped history."""
    clock = FakeClock()
    ecs, ec2 = healthy_clients()

    def inventory(**kwargs: Any) -> Any:
        """Spend the full SDK envelope on each historical page."""
        if kwargs["desiredStatus"] == "RUNNING":
            return {"taskArns": [TASK_1]}
        clock.sleep(7)
        return {"taskArns": [], "nextToken": "more"}

    ecs.list_tasks.side_effect = inventory
    http = FakeHttpClient([503, 200])
    result = observer(clock, ecs, ec2, http).wait(
        SERVICE_NAME, EXPECTED_DEFINITION, StartupBudget(60, clock.monotonic, clock.wall_clock)
    )
    assert result.success
    assert len(http.urls) == 2
    assert clock.seconds < 60


def test_startup_factory_time_consumes_budget_before_creation() -> None:
    """Late dedicated-client setup cannot receive a fresh readiness deadline."""
    clock = FakeClock()
    ecs, ec2 = healthy_clients()
    ecs.meta.config = Config()
    ec2.meta.config = Config()
    clients = {"ecs": ecs, "ec2": ec2, "ecr": Mock(), "logs": Mock()}

    def slow_factory(service: str, config: Config) -> Any:
        """Simulate a credential or client-setup delay exceeding the deadline."""
        clock.sleep(25)
        return clients[service]

    with patch(
        "showtime.core.aws.boto3.client", side_effect=lambda service, **kw: clients[service]
    ):
        aws = AWSInterface(
            startup_client_factory=slow_factory,
            http_client_factory=lambda: FakeHttpClient([200]),
            monotonic=clock.monotonic,
            wall_clock=clock.wall_clock,
            sleep=clock.sleep,
        )
    with patch.object(
        aws, "_create_task_definition_with_image_and_flags", return_value=EXPECTED_DEFINITION
    ), patch.object(aws, "_service_exists_any_state", return_value=False):
        result = aws.create_environment(1234, "abc123f", startup_timeout_seconds=20)
    assert not result.success
    assert result.service_created is False
    ecs.create_service.assert_not_called()
    assert clock.seconds == 25


def test_late_successful_creation_is_confirmed_but_not_ready() -> None:
    """A late successful AWS response proves allocation even though readiness fails."""
    clock = FakeClock()
    ecs, ec2 = healthy_clients()
    ecs.create_service.side_effect = lambda **kw: clock.sleep(31) or {}
    aws = AWSInterface(
        ecs_client=ecs,
        ecr_client=Mock(),
        ec2_client=ec2,
        http_client_factory=Mock,
        monotonic=clock.monotonic,
        wall_clock=clock.wall_clock,
        sleep=clock.sleep,
    )
    with patch.object(
        aws, "_create_task_definition_with_image_and_flags", return_value=EXPECTED_DEFINITION
    ), patch.object(aws, "_service_exists_any_state", return_value=False):
        result = aws.create_environment(1234, "abc123f", startup_timeout_seconds=30)
    assert not result.success
    assert result.service_created is True
    ecs.update_service.assert_not_called()


@pytest.mark.parametrize(
    "payload, secrets",
    [
        ('{"token": "runtime-secret"}', ["runtime-secret"]),
        ('password="firstword secondword"', ["firstword", "secondword"]),
        ("password='firstword secondword'", ["firstword", "secondword"]),
        (
            '{"API_TOKEN": "firstword \\"middleword\\" lastword"}',
            ["firstword", "middleword", "lastword"],
        ),
    ],
)
def test_structured_secret_assignments_are_fully_redacted(payload: str, secrets: Any) -> None:
    """JSON keys and quoted values receive the same redaction boundary."""
    summary = DiagnosticSummary(SERVICE_NAME, EXPECTED_DEFINITION, 1, "failed", log_lines=[payload])
    rendered = render_diagnostic_summary(summary)
    for secret in secrets:
        assert secret not in rendered


def test_described_task_must_belong_to_requested_inventory() -> None:
    """A same-definition foreign task cannot establish health for the service."""
    clock = FakeClock()
    ecs, ec2 = healthy_clients()
    ecs.describe_tasks.return_value = {"tasks": [running_task(TASK_2)]}
    http = FakeHttpClient([200])
    result = observer(clock, ecs, ec2, http).wait(
        SERVICE_NAME, EXPECTED_DEFINITION, StartupBudget(30, clock.monotonic, clock.wall_clock)
    )
    assert not result.success
    assert http.urls == []


@pytest.mark.parametrize("operation", ["CreateService", "UpdateService"])
def test_mutation_error_code_survives_in_typed_diagnostic(operation: str) -> None:
    """Normalized mutation evidence is retained without raw exception payloads."""
    ecs, ec2 = healthy_clients()
    method = ecs.create_service if operation == "CreateService" else ecs.update_service
    method.side_effect = ClientError(
        {"Error": {"Code": "AccessDeniedException", "Message": "raw-sentinel"}}, operation
    )
    aws = AWSInterface(ecs_client=ecs, ecr_client=Mock(), ec2_client=ec2, http_client_factory=Mock)
    with patch.object(
        aws, "_create_task_definition_with_image_and_flags", return_value=EXPECTED_DEFINITION
    ), patch.object(aws, "_service_exists_any_state", return_value=False):
        result = aws.create_environment(1234, "abc123f")
    assert not result.success
    assert f"{operation}:AccessDeniedException" in result.diagnostic.api_errors
    assert "raw-sentinel" not in str(result.diagnostic)


def test_late_http_200_cannot_establish_readiness() -> None:
    """Streaming success arriving after the original deadline remains a failure."""
    clock = FakeClock()
    ecs, ec2 = healthy_clients()

    class LateHttp(FakeHttpClient):
        def stream(self, method: str, url: str, **kwargs: Any) -> Any:
            """Return healthy headers only after time has expired."""
            response = super().stream(method, url, **kwargs)
            clock.sleep(31)
            return response

    result = observer(clock, ecs, ec2, LateHttp([200])).wait(
        SERVICE_NAME, EXPECTED_DEFINITION, StartupBudget(30, clock.monotonic, clock.wall_clock)
    )
    assert not result.success
    assert "deadline" in result.error


def test_legacy_aws_session_token_is_redacted(monkeypatch: pytest.MonkeyPatch) -> None:
    """The legacy Botocore environment token is independently protected."""
    from showtime.core.readiness import configured_secret_values

    monkeypatch.setenv("AWS_SECURITY_TOKEN", "legacy-unique-credential")
    summary = DiagnosticSummary(
        SERVICE_NAME, EXPECTED_DEFINITION, 1, "failed", log_lines=["value legacy-unique-credential"]
    )
    assert "legacy-unique-credential" not in render_diagnostic_summary(
        summary, configured_secret_values()
    )


@pytest.mark.parametrize(
    "payload",
    [
        "Authorization: Bearer runtime-sentinel",
        '"Authorization": "Basic runtime-sentinel"',
        'credential="runtime-sentinel"',
    ],
)
def test_authorization_assignments_are_redacted(payload: str) -> None:
    """Runtime credentials absent from local configuration stay private."""
    summary = DiagnosticSummary(SERVICE_NAME, EXPECTED_DEFINITION, 1, "failed", log_lines=[payload])
    assert "runtime-sentinel" not in render_diagnostic_summary(summary)


@pytest.mark.parametrize("stop_after", [10, 30])
def test_diagnostic_window_does_not_extend_startup_correlation(stop_after: int) -> None:
    """Tasks and events first appearing after failure are not startup evidence."""
    from datetime import timedelta

    from tests.unit.test_phase2_readiness import stable_service

    clock = FakeClock()
    ecs, ec2 = healthy_clients()
    obs = observer(clock, ecs, ec2, FakeHttpClient([]), logs=Mock())
    obs.set_candidate(SERVICE_NAME, EXPECTED_DEFINITION)
    startup = StartupBudget(30, clock.monotonic, clock.wall_clock)
    clock.seconds = stop_after
    late = running_task(TASK_2)
    late["createdAt"] = clock.started_at + timedelta(seconds=stop_after + 5)
    late["lastStatus"] = "STOPPED"
    late["desiredStatus"] = "STOPPED"
    ecs.list_tasks.side_effect = [{"taskArns": [TASK_2]}, {"taskArns": [TASK_2]}]
    ecs.describe_tasks.return_value = {"tasks": [late]}
    service = stable_service()
    service["events"] = [{"createdAt": late["createdAt"], "message": "post-startup-event"}]

    def describe(**kwargs: Any) -> Any:
        """Advance into diagnostic-only time before evidence is returned."""
        clock.sleep(5)
        return {"services": [service]}

    ecs.describe_services.side_effect = describe
    summary = obs.capture_failure(
        "failed", startup, StartupBudget(30, clock.monotonic, clock.wall_clock)
    )
    assert summary.tasks and not summary.tasks[0].correlated
    assert not summary.service_events
    obs.logs.get_log_events.assert_not_called()
