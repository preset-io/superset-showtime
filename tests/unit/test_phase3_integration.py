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
from pathlib import Path
from typing import Any
from unittest.mock import Mock, call, patch

import pytest
import yaml
from typer.testing import CliRunner

from showtime.cli import app
from showtime.core.aws import AWSInterface, EnvironmentResult
from showtime.core.pull_request import PullRequest
from showtime.core.runner_smoke import ProcessResult, SmokeResult
from showtime.core.show import Show
from showtime.core.task_definition import render_task_definition

IMAGE_REFERENCE = "apache/superset@sha256:" + "a" * 64


def test_build_returns_manifest_digest_and_requests_buildx_metadata() -> None:
    """A successful push yields Buildx's manifest digest, not mutable identity."""
    show = Show(1234, "abc123f", "building")

    metadata_paths: list[str] = []

    def successful_build(command: list[str], *_args: object, **_kwargs: object) -> ProcessResult:
        metadata_path = command[command.index("--metadata-file") + 1]
        metadata_paths.append(metadata_path)
        Path(metadata_path).write_text(
            '{"containerimage.config.digest":"sha256:'
            + "b" * 64
            + '","containerimage.digest":"sha256:'
            + "a" * 64
            + '"}'
        )
        return ProcessResult(0)

    with patch("showtime.core.show.run_bounded_process", side_effect=successful_build) as run:
        result = show.build_docker()

    command = run.call_args.args[0]
    assert "--metadata-file" in command
    assert command[command.index("--platform") + 1] == "linux/amd64"
    assert run.call_args.args[1] == 3600
    assert result == IMAGE_REFERENCE
    assert not Path(metadata_paths[0]).exists()


@pytest.mark.parametrize(
    "metadata",
    [
        "{}",
        '{"containerimage.config.digest":"sha256:' + "b" * 64 + '"}',
        '{"containerimage.digest":"not-a-digest"}',
    ],
)
def test_build_rejects_missing_or_malformed_manifest_metadata(metadata: str) -> None:
    """A config digest or malformed metadata cannot authorize smoke or ECS."""
    show = Show(1234, "abc123f", "building")

    def completed_build(command: list[str], *_args: object, **_kwargs: object) -> ProcessResult:
        Path(command[command.index("--metadata-file") + 1]).write_text(metadata)
        return ProcessResult(0)

    with patch("showtime.core.show.run_bounded_process", side_effect=completed_build):
        with pytest.raises(RuntimeError, match="valid manifest digest"):
            show.build_docker()


def test_sync_passes_one_build_identity_to_aws() -> None:
    """The managed build-to-ECS path must not reconstruct a mutable tag."""
    pr = PullRequest(1234, [])
    candidate = Show(1234, "abc123f", "building")
    build = Mock(return_value=IMAGE_REFERENCE)
    deploy = Mock()
    candidate.build_docker = build  # type: ignore[method-assign]
    smoke = Mock()
    candidate.run_smoke = smoke  # type: ignore[method-assign]
    candidate.deploy_aws = deploy  # type: ignore[method-assign]

    with ExitStack() as stack:
        stack.enter_context(
            patch.object(pr, "_determine_action", return_value="create_environment")
        )
        stack.enter_context(patch.object(pr, "_atomic_claim", return_value=True))
        stack.enter_context(patch.object(pr, "_create_new_show", return_value=candidate))
        stack.enter_context(patch.object(pr, "set_show_status"))
        stack.enter_context(patch.object(pr, "_update_show_labels"))
        stack.enter_context(
            patch.object(
                pr, "set_active_show", return_value=Mock(success=True, pointer_attached=True)
            )
        )
        stack.enter_context(patch.object(pr, "_cleanup_shows", return_value=Mock(success=True)))
        stack.enter_context(patch.object(pr, "_show_service_urls"))
        stack.enter_context(patch.object(pr, "_post_building_comment"))
        stack.enter_context(patch.object(pr, "_post_success_comment"))
        stack.enter_context(
            patch(
                "showtime.core.pull_request.get_github",
                return_value=Mock(get_pr_data=Mock(return_value={"body": ""})),
            )
        )
        result = pr.sync("abc123f")

    assert result.success is True
    deploy.assert_called_once_with(
        False,
        feature_flags=[],
        startup_timeout_seconds=1800,
        image_reference=IMAGE_REFERENCE,
    )
    smoke.assert_not_called()


def test_enabled_smoke_runs_between_build_and_aws_and_readiness_remains_mandatory() -> None:
    """Healthy local smoke is a pre-gate, while deploy_aws still owns ECS readiness."""
    events: list[str] = []
    pr = PullRequest(1234, [])
    candidate = Show(1234, "abc123f", "building")
    candidate.build_docker = Mock(  # type: ignore[method-assign]
        side_effect=lambda *_args, **_kwargs: events.append("build") or IMAGE_REFERENCE
    )
    candidate.run_smoke = Mock(  # type: ignore[method-assign]
        side_effect=lambda *_args, **_kwargs: events.append("smoke")
        or SmokeResult(True, "healthy", cleanup_success=True)
    )
    candidate.deploy_aws = Mock(  # type: ignore[method-assign]
        side_effect=lambda *_args, **_kwargs: events.append("ecs-readiness")
    )

    with _sync_context(pr, candidate):
        result = pr.sync("abc123f", smoke_test=True, build_timeout_seconds=9)

    assert result.success is True
    assert events == ["build", "smoke", "ecs-readiness"]
    candidate.build_docker.assert_called_once_with(False, build_timeout_seconds=9)
    candidate.run_smoke.assert_called_once_with(
        IMAGE_REFERENCE,
        feature_flags=[],
        timeout_seconds=600,
        diagnostics_dir=".showtime/diagnostics",
    )


def test_real_show_interfaces_share_identical_rendered_digest_and_configuration() -> None:
    """Real Show adapters give smoke and ECS the same deterministic render inputs."""
    pr = PullRequest(1234, [])
    candidate = Show(1234, "abc123f", "building")
    candidate.build_docker = Mock(return_value=IMAGE_REFERENCE)  # type: ignore[method-assign]
    smoke_runner = Mock()
    smoke_runner.run.return_value = SmokeResult(True, "healthy", cleanup_success=True)
    aws = Mock()
    aws.create_environment.return_value = EnvironmentResult(True, ip="1.2.3.4")

    with _sync_context(pr, candidate) as stack:
        stack.enter_context(patch("showtime.core.show.RunnerSmoke", return_value=smoke_runner))
        stack.enter_context(patch("showtime.core.show.get_interfaces", return_value=(Mock(), aws)))
        result = pr.sync("abc123f", smoke_test=True)

    assert result.success is True
    smoke_image, smoke_definition = smoke_runner.run.call_args.args
    assert smoke_image == IMAGE_REFERENCE
    assert smoke_definition["containerDefinitions"][0]["image"] == IMAGE_REFERENCE
    assert aws.create_environment.call_args.kwargs["image_reference"] == IMAGE_REFERENCE
    ecs_definition = render_task_definition(
        aws.create_environment.call_args.kwargs["image_reference"],
        aws.create_environment.call_args.kwargs["feature_flags"],
    )
    assert smoke_definition == ecs_definition


def test_smoke_failure_prevents_aws_and_preserves_old_pointer() -> None:
    """A failed pre-gate cannot register ECS state or promote the candidate."""
    pr = PullRequest(1234, ["🎪 old123a 🚦 running", "🎪 🎯 old123a"])
    candidate = Show(1234, "abc123f", "building")
    candidate.build_docker = Mock(return_value=IMAGE_REFERENCE)  # type: ignore[method-assign]
    candidate.run_smoke = Mock(  # type: ignore[method-assign]
        return_value=SmokeResult(
            False,
            "timeout",
            error="health deadline",
            cleanup_success=True,
            artifact_paths=["evidence.json"],
        )
    )
    candidate.deploy_aws = Mock()  # type: ignore[method-assign]

    with _sync_context(pr, candidate) as stack:
        record = stack.enter_context(
            patch.object(pr, "_record_failed_candidate", wraps=pr._record_failed_candidate)
        )
        result = pr.sync("abc123f", smoke_test=True, dry_run_github=True)

    assert result.success is False
    assert "evidence.json" in (result.error or "")
    candidate.deploy_aws.assert_not_called()
    record.assert_called_once()
    assert pr.current_show is not None
    assert pr.current_show.sha == "old123a"


def test_dry_run_restrictions_are_checked_before_claim() -> None:
    """Skipped Docker cannot feed smoke or live AWS immutable identity."""
    pr = PullRequest(1234, [])
    with patch.object(pr, "_determine_action") as determine:
        with pytest.raises(ValueError, match="requires --dry-run-aws"):
            pr.sync("abc123f", dry_run_docker=True)
        with pytest.raises(ValueError, match="cannot be enabled"):
            pr.sync(
                "abc123f",
                dry_run_docker=True,
                dry_run_aws=True,
                smoke_test=True,
            )
    determine.assert_not_called()


def test_aws_uses_exact_immutable_reference_and_rejects_conflicting_override() -> None:
    """Managed ECS rendering is digest-pinned; legacy override remains exclusive."""
    ecs = Mock()
    ecs.register_task_definition.return_value = {
        "taskDefinition": {"taskDefinitionArn": "arn:task/candidate:1"}
    }
    aws = AWSInterface(
        ecs_client=ecs,
        ecr_client=Mock(),
        ec2_client=Mock(),
        logs_client=Mock(),
    )

    arn = aws._create_task_definition_with_image_and_flags(
        IMAGE_REFERENCE,
        [{"name": "SUPERSET_FEATURE_Z", "value": "True"}],
    )
    conflict = aws.create_environment(
        1234,
        "abc123f",
        image_tag_override="latest",
        image_reference=IMAGE_REFERENCE,
    )

    assert arn == "arn:task/candidate:1"
    rendered = ecs.register_task_definition.call_args.kwargs
    assert rendered["containerDefinitions"][0]["image"] == IMAGE_REFERENCE
    assert conflict.success is False
    assert "exclusive" in (conflict.error or "")
    assert ecs.mock_calls == [call.register_task_definition(**rendered)]


@pytest.mark.parametrize("command", ["start", "sync"])
def test_cli_rejects_invalid_smoke_options_before_pr_lookup(command: str, tmp_path: Path) -> None:
    """CLI option validation precedes GitHub, Docker, and AWS work."""
    symlink = tmp_path / "diagnostics-link"
    symlink.symlink_to(tmp_path / "target")
    with patch.object(PullRequest, "from_id") as from_id:
        invalid_timeout = CliRunner().invoke(app, [command, "1234", "--smoke-timeout-seconds", "0"])
        invalid_path = CliRunner().invoke(
            app,
            [command, "1234", "--smoke-diagnostics-dir", str(symlink)],
        )

    assert invalid_timeout.exit_code != 0
    assert invalid_path.exit_code != 0
    from_id.assert_not_called()


@pytest.mark.parametrize("command", ["start", "sync"])
def test_cli_smoke_defaults_and_environment_overrides(command: str, tmp_path: Path) -> None:
    """Both deployment commands expose the default-off smoke configuration."""
    pr = Mock(current_show=None)
    pr.start_environment.return_value = Mock(success=True, show=None)
    pr.sync.return_value = Mock(success=True, action_taken="create_environment")
    github = Mock()
    github.get_latest_commit_sha.return_value = "abc123f"
    github.get_pr_data.return_value = {"state": "open"}
    diagnostics = tmp_path / "diagnostics"

    with ExitStack() as stack:
        stack.enter_context(patch.object(PullRequest, "from_id", return_value=pr))
        stack.enter_context(patch("showtime.core.pull_request.get_github", return_value=github))
        stack.enter_context(
            patch("showtime.core.git_validation.should_skip_validation", return_value=True)
        )
        result = CliRunner().invoke(
            app,
            [command, "1234"] + (["--sha", "abc123f"] if command == "sync" else []),
            env={
                "SHOWTIME_SMOKE_TEST": "true",
                "SHOWTIME_SMOKE_TIMEOUT_SECONDS": "7",
                "SHOWTIME_BUILD_TIMEOUT_SECONDS": "9",
                "SHOWTIME_SMOKE_DIAGNOSTICS_DIR": str(diagnostics),
            },
        )

    assert result.exit_code == 0, result.output
    target = pr.start_environment if command == "start" else pr.sync
    assert target.call_args.kwargs["smoke_test"] is True
    assert target.call_args.kwargs["smoke_timeout_seconds"] == 7
    assert target.call_args.kwargs["build_timeout_seconds"] == 9
    assert target.call_args.kwargs["smoke_diagnostics_dir"] == str(diagnostics)


def test_check_only_does_not_create_diagnostics_directory(tmp_path: Path) -> None:
    """Read-only analysis validates its path but does not write diagnostic files."""
    diagnostics = tmp_path / "diagnostics"
    pr = Mock()
    pr.analyze.return_value = Mock(to_gha_stdout=Mock(return_value="sync_needed=false"))
    github = Mock()
    github.get_pr_data.return_value = {"state": "open"}

    with ExitStack() as stack:
        stack.enter_context(patch.object(PullRequest, "from_id", return_value=pr))
        stack.enter_context(patch("showtime.core.pull_request.get_github", return_value=github))
        result = CliRunner().invoke(
            app,
            [
                "sync",
                "1234",
                "--sha",
                "abc123f",
                "--check-only",
                "--smoke-diagnostics-dir",
                str(diagnostics),
            ],
        )

    assert result.exit_code == 0, result.output
    assert not diagnostics.exists()


class _SyncContext:
    def __init__(self, pr: PullRequest, candidate: Show) -> None:
        self.stack = ExitStack()
        self.pr = pr
        self.candidate = candidate

    def __enter__(self) -> ExitStack:
        stack = self.stack
        stack.enter_context(
            patch.object(self.pr, "_determine_action", return_value="create_environment")
        )
        stack.enter_context(patch.object(self.pr, "_atomic_claim", return_value=True))
        stack.enter_context(patch.object(self.pr, "_create_new_show", return_value=self.candidate))
        stack.enter_context(patch.object(self.pr, "set_show_status"))
        stack.enter_context(patch.object(self.pr, "_update_show_labels"))
        stack.enter_context(
            patch.object(
                self.pr, "set_active_show", return_value=Mock(success=True, pointer_attached=True)
            )
        )
        stack.enter_context(
            patch.object(self.pr, "_cleanup_shows", return_value=Mock(success=True))
        )
        stack.enter_context(patch.object(self.pr, "_show_service_urls"))
        stack.enter_context(patch.object(self.pr, "_post_building_comment"))
        stack.enter_context(patch.object(self.pr, "_post_success_comment"))
        stack.enter_context(
            patch(
                "showtime.core.pull_request.get_github",
                return_value=Mock(get_pr_data=Mock(return_value={"body": ""})),
            )
        )
        return stack

    def __exit__(self, *args: Any) -> None:
        self.stack.__exit__(*args)


def _sync_context(pr: PullRequest, candidate: Show) -> _SyncContext:
    return _SyncContext(pr, candidate)


def test_reference_workflow_uploads_only_hidden_smoke_artifacts() -> None:
    """The example opts hidden diagnostic files into a narrowly scoped upload."""
    workflow = Path("workflows-reference/showtime-trigger.yml").read_text()
    parsed = yaml.safe_load(workflow)
    upload = next(
        step
        for step in parsed["jobs"]["sync"]["steps"]
        if step.get("uses") == "actions/upload-artifact@v4"
    )

    assert upload["if"] == "always()"
    assert upload["with"]["include-hidden-files"] is True
    assert upload["with"]["if-no-files-found"] == "ignore"
    assert upload["with"]["path"] == ".showtime/diagnostics/runner-smoke-*"
    assert "\n          path: .showtime/\n" not in workflow
