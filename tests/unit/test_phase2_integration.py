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

from contextlib import ExitStack
from typing import Any
from unittest.mock import Mock, patch

import pytest
from botocore.config import Config
from typer.testing import CliRunner

from showtime.cli import app
from showtime.core.aws import AWSInterface, EnvironmentResult
from showtime.core.pull_request import PullRequest, SyncResult
from showtime.core.readiness import DiagnosticSummary
from showtime.core.show import Show


def test_injected_sdk_startup_clients_require_bounded_config() -> None:
    """Injected botocore clients with default long timeouts fail before mutation."""
    client = Mock()
    client.meta.config = Config(connect_timeout=60, read_timeout=60)
    aws = AWSInterface(ecs_client=client, ecr_client=Mock(), ec2_client=Mock())

    with pytest.raises(ValueError, match="transport limits"):
        aws._get_startup_clients()


def test_show_preserves_and_renders_diagnostic_before_raising(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Show exposes the typed diagnostic before orchestration compensates."""
    summary = DiagnosticSummary(
        service_name="pr-1234-abc123f-service",
        expected_task_definition_arn="arn:task/candidate:1",
        elapsed_seconds=15,
        primary_error="strict health returned 503",
    )
    aws = Mock()
    aws.create_environment.return_value = EnvironmentResult(
        False,
        error="strict health returned 503",
        task_definition_arn="arn:task/candidate:1",
        service_created=True,
        diagnostic=summary,
    )
    show = Show(1234, "abc123f", "deploying")

    with patch("showtime.core.show.get_interfaces", return_value=(Mock(), aws)):
        with pytest.raises(Exception, match="strict health"):
            show.deploy_aws()

    assert show.diagnostic is summary
    assert "strict health returned 503" in capsys.readouterr().out


def test_render_failure_does_not_mask_deployment_or_allocation_truth() -> None:
    """Diagnostic rendering is guarded independently from lifecycle state."""
    summary = DiagnosticSummary(
        service_name="pr-1234-abc123f-service",
        expected_task_definition_arn="arn:task/candidate:1",
        elapsed_seconds=15,
        primary_error="primary",
    )
    aws = Mock()
    aws.create_environment.return_value = EnvironmentResult(
        False,
        error="primary",
        task_definition_arn="arn:task/candidate:1",
        service_created=None,
        diagnostic=summary,
    )
    show = Show(1234, "abc123f", "deploying")

    with ExitStack() as stack:
        stack.enter_context(patch("showtime.core.show.get_interfaces", return_value=(Mock(), aws)))
        stack.enter_context(
            patch(
                "showtime.core.show.render_diagnostic_summary", side_effect=RuntimeError("render")
            )
        )
        with pytest.raises(Exception, match="AWS deployment failed: primary"):
            show.deploy_aws()

    assert show.service_created is None
    assert show.diagnostic is summary


def test_timeout_plumbs_from_pull_request_to_show() -> None:
    """PullRequest forwards the accepted timeout through candidate deployment."""
    pr = PullRequest(1234, [])
    candidate = Show(1234, "abc123f", "building")

    with ExitStack() as stack:
        stack.enter_context(
            patch.object(pr, "_determine_action", return_value="create_environment")
        )
        stack.enter_context(patch.object(pr, "_atomic_claim", return_value=True))
        stack.enter_context(patch.object(pr, "_create_new_show", return_value=candidate))
        stack.enter_context(
            patch.object(
                candidate,
                "build_docker",
                return_value="apache/superset@sha256:" + "a" * 64,
            )
        )
        deploy = stack.enter_context(patch.object(candidate, "deploy_aws"))
        stack.enter_context(patch.object(pr, "set_show_status"))
        stack.enter_context(patch.object(pr, "_update_show_labels"))
        stack.enter_context(
            patch.object(
                pr, "set_active_show", return_value=Mock(success=True, pointer_attached=True)
            )
        )
        stack.enter_context(patch.object(pr, "_cleanup_shows", return_value=Mock(success=True)))
        stack.enter_context(patch.object(pr, "_show_service_urls"))
        stack.enter_context(
            patch(
                "showtime.core.pull_request.get_github",
                return_value=Mock(get_pr_data=Mock(return_value={"body": ""})),
            )
        )
        result = pr.sync("abc123f", startup_timeout_seconds=321)

    assert result.success is True
    deploy.assert_called_once_with(
        False,
        feature_flags=[],
        startup_timeout_seconds=321,
        image_reference="apache/superset@sha256:" + "a" * 64,
    )


@pytest.mark.parametrize("command", ["start", "sync"])
def test_cli_rejects_invalid_timeout_before_pull_request_lookup(command: str) -> None:
    """Invalid configuration fails before GitHub, labels, Docker, or AWS work."""
    with patch.object(PullRequest, "from_id") as from_id:
        args = [command, "1234", "--startup-timeout-seconds", "0"]
        result = CliRunner().invoke(app, args)

    assert result.exit_code != 0
    from_id.assert_not_called()


@pytest.mark.parametrize("command", ["start", "sync"])
@pytest.mark.parametrize("value, expected", [("321", 321), (None, 1800)])
def test_cli_timeout_defaults_and_environment_override(
    command: str, value: Any, expected: int
) -> None:
    """Both commands expose the default and SHOWTIME environment override."""
    pr = Mock()
    pr.current_show = None
    pr.start_environment.return_value = SyncResult(True, "create_environment")
    pr.sync.return_value = SyncResult(True, "create_environment")
    github = Mock()
    github.get_latest_commit_sha.return_value = "abc123f"
    github.get_pr_data.return_value = {"state": "open"}

    with ExitStack() as stack:
        stack.enter_context(patch.object(PullRequest, "from_id", return_value=pr))
        stack.enter_context(patch("showtime.core.pull_request.get_github", return_value=github))
        stack.enter_context(
            patch("showtime.core.git_validation.should_skip_validation", return_value=True)
        )
        result = CliRunner().invoke(
            app,
            [command, "1234"] + (["--sha", "abc123f"] if command == "sync" else []),
            env={"SHOWTIME_STARTUP_TIMEOUT_SECONDS": value},
        )

    assert result.exit_code == 0, result.output
    target = pr.start_environment if command == "start" else pr.sync
    assert target.call_args.kwargs["startup_timeout_seconds"] == expected
