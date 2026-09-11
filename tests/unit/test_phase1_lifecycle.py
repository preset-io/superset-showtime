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
from typing import Any, Optional, Union
from unittest.mock import Mock, patch

import pytest

from showtime.core.aws import AWSError, AWSInterface, EnvironmentResult
from showtime.core.pull_request import PullRequest
from showtime.core.show import Show


def _aws_with_clients(ecs: Optional[Mock] = None) -> AWSInterface:
    """Build an AWS interface using only injected in-memory clients."""
    ecs_client = ecs or Mock()
    ecs_client.exceptions.ServiceNotFoundException = type("ServiceNotFound", (Exception,), {})
    ecr = Mock()
    ecr.exceptions.ImageNotFoundException = type("ImageNotFound", (Exception,), {})
    return AWSInterface(ecs_client=ecs_client, ecr_client=ecr, ec2_client=Mock())


def test_resource_reconciliation_preserves_promoted_pointer(github_fake: Any) -> None:
    """Resource-label reconciliation must not own the active pointer."""
    github = github_fake(["🎪 abc123f 🚦 deploying", "🎪 🎯 abc123f"])
    show = Show(1234, "abc123f", "running", created_at="2026-01-01T00-00")
    pr = PullRequest(1234, list(github.labels))

    with patch("showtime.core.pull_request.get_github", return_value=github):
        pr._update_show_labels(show)

    assert "🎪 🎯 abc123f" in github.labels
    assert ("remove", "🎪 🎯 abc123f") not in github.writes


def test_pointer_replacement_is_added_before_old_pointer_removal(github_fake: Any) -> None:
    """A failed candidate-pointer add must leave the healthy pointer usable."""
    old_pointer = "🎪 🎯 old123f"
    new_pointer = "🎪 🎯 new456a"
    github = github_fake([old_pointer, "🎪 old123f 🚦 running"])
    github.fail_add.add(new_pointer)
    pr = PullRequest(1234, list(github.labels))

    with patch("showtime.core.pull_request.get_github", return_value=github):
        with pytest.raises(RuntimeError, match="failed to add"):
            pr.set_active_show(Show(1234, "new456a", "running"))

    assert old_pointer in github.labels
    assert github.writes == [("add", new_pointer)]


def test_status_replacement_is_added_before_old_status_removal(github_fake: Any) -> None:
    """A failed replacement add must retain the prior discoverability sentinel."""
    old_status = "🎪 abc123f 🚦 deploying"
    new_status = "🎪 abc123f 🚦 running"
    github = github_fake([old_status])
    github.fail_add.add(new_status)
    pr = PullRequest(1234, list(github.labels))

    with patch("showtime.core.pull_request.get_github", return_value=github):
        with pytest.raises(RuntimeError, match="failed to add"):
            pr.set_show_status(Show(1234, "abc123f", "deploying"), "running")

    assert old_status in github.labels
    assert github.writes == [("add", new_status)]


def test_show_round_trips_cleanup_and_task_definition_ownership() -> None:
    """Cleanup ownership must survive reconstruction from GitHub labels."""
    arn = "arn:aws:ecs:us-west-2:123456789012:task-definition/showtime:42"
    show = Show(
        1234,
        "abc123f",
        "failed",
        created_at="2026-01-01T00-00",
        task_definition_arn=arn,
        cleanup_pending=True,
    )

    rebuilt = Show.from_circus_labels(1234, show.to_circus_labels(), "abc123f")

    assert rebuilt is not None
    assert rebuilt.cleanup_pending is True
    assert rebuilt.task_definition_fingerprint == show.task_definition_fingerprint
    assert len(rebuilt.task_definition_fingerprint or "") == 32


def test_deploy_copies_uncertain_resource_state_before_raising() -> None:
    """A failed deployment must expose whether its service may remain allocated."""
    arn = "arn:task-definition/candidate:1"
    result = EnvironmentResult(
        False,
        service_name="pr-1234-abc123f-service",
        error="health failed",
        task_definition_arn=arn,
        service_created=True,
    )
    github = Mock()
    aws = Mock()
    aws.create_environment.return_value = result
    show = Show(1234, "abc123f", "deploying")

    with patch("showtime.core.show.get_interfaces", return_value=(github, aws)):
        with pytest.raises(Exception, match="health failed"):
            show.deploy_aws()

    assert show.task_definition_arn == arn
    assert show.service_created is True


@pytest.mark.parametrize("stop_outcome", [False, RuntimeError("delete failed")])
def test_aggregate_teardown_preserves_failed_tracking(
    github_fake: Any, stop_outcome: Union[bool, Exception]
) -> None:
    """False and exceptional deletions must fail while retaining resource labels."""
    labels = [
        "🎪 aaa111a 🚦 running",
        "🎪 bbb222b 🚦 failed",
        "🎪 🎯 aaa111a",
    ]
    github = github_fake(labels)
    pr = PullRequest(1234, labels)
    first, second = sorted(pr.shows, key=lambda item: item.sha)
    first.stop = Mock(return_value=True)  # type: ignore[method-assign]
    second.stop = Mock(side_effect=stop_outcome if isinstance(stop_outcome, Exception) else None)
    if not isinstance(stop_outcome, Exception):
        second.stop.return_value = stop_outcome

    with patch("showtime.core.pull_request.get_github", return_value=github):
        result = pr.stop_environment(dry_run_github=False, dry_run_aws=False)

    assert result.success is False
    assert result.cleanup_result is not None
    assert result.cleanup_result.deleted_shas == ["aaa111a"]
    assert result.cleanup_result.pending_shas == ["bbb222b"]
    assert "🎪 bbb222b 🚦 failed" in github.labels
    assert "🎪 bbb222b 🧹 cleanup-pending" in github.labels
    assert not any(label.startswith("🎪 aaa111a ") for label in github.labels)


def test_teardown_considers_every_tracked_show_without_pointer(github_fake: Any) -> None:
    """Pointer absence must not short-circuit tracked-resource cleanup."""
    labels = ["🎪 aaa111a 🚦 failed", "🎪 bbb222b 🚦 running"]
    github = github_fake(labels)
    pr = PullRequest(1234, labels)
    for show in pr.shows:
        show.stop = Mock(return_value=True)  # type: ignore[method-assign]

    with patch("showtime.core.pull_request.get_github", return_value=github):
        result = pr.stop_environment()

    assert result.success is True
    assert result.cleanup_result is not None
    assert sorted(result.cleanup_result.attempted_shas) == ["aaa111a", "bbb222b"]
    assert not any(label.startswith("🎪") for label in github.labels)


def test_current_show_is_deterministic_with_legacy_multiple_pointers() -> None:
    """Recovery from multiple pointers prefers running, newest, then SHA."""
    labels = [
        "🎪 aaa111a 🚦 running",
        "🎪 aaa111a 📅 2026-01-01T00-00",
        "🎪 🎯 aaa111a",
        "🎪 bbb222b 🚦 running",
        "🎪 bbb222b 📅 2026-02-01T00-00",
        "🎪 🎯 bbb222b",
    ]

    assert PullRequest(1234, labels).current_show.sha == "bbb222b"  # type: ignore[union-attr]


def test_guarded_delete_refuses_changed_task_definition() -> None:
    """Candidate compensation must not delete a service with changed ownership."""
    ecs = Mock()
    ecs.describe_services.return_value = {
        "services": [{"status": "ACTIVE", "taskDefinition": "arn:task/current:2"}],
        "failures": [],
    }
    aws = _aws_with_clients(ecs)

    result = aws.delete_environment(
        "pr-1234-abc123f",
        1234,
        expected_task_definition_arn="arn:task/candidate:1",
        delete_image=False,
    )

    assert result is False
    ecs.delete_service.assert_not_called()


def test_guarded_delete_does_not_treat_uncertain_describe_as_absence() -> None:
    """A non-missing failure must not be treated as confirmed absence."""
    ecs = Mock()
    ecs.describe_services.return_value = {
        "services": [],
        "failures": [{"reason": "ACCESS_DENIED"}],
    }
    aws = _aws_with_clients(ecs)

    assert (
        aws.delete_environment(
            "pr-1234-abc123f", 1234, expected_task_definition_arn="arn:task/candidate:1"
        )
        is False
    )
    ecs.delete_service.assert_not_called()


def test_guarded_delete_accepts_definitive_missing_service() -> None:
    """A MISSING response confirms successful absence without a delete write."""
    ecs = Mock()
    ecs.describe_services.return_value = {
        "services": [],
        "failures": [{"reason": "MISSING"}],
    }
    aws = _aws_with_clients(ecs)

    assert aws.delete_environment("pr-1234-abc123f", 1234, expected_task_definition_arn="arn:x")
    ecs.delete_service.assert_not_called()


def test_delete_requires_confirmed_service_absence() -> None:
    """Accepted deletion is not success until the bounded waiter confirms absence."""
    ecs = Mock()
    aws = _aws_with_clients(ecs)

    with patch.object(aws, "_wait_for_service_deletion", return_value=False):
        assert aws.delete_environment("pr-1234-abc123f", 1234) is False


def test_service_inventory_paginates_and_raises_on_uncertainty() -> None:
    """Inventory must consume every page and never convert AWS errors to empty."""
    ecs = Mock()
    ecs.list_services.side_effect = [
        {"serviceArns": ["arn/service/pr-1-aaa111a-service"], "nextToken": "page-2"},
        {"serviceArns": ["arn/service/pr-2-bbb222b-service"]},
        RuntimeError("denied"),
    ]
    aws = _aws_with_clients(ecs)

    assert aws.find_showtime_services() == [
        "pr-1-aaa111a-service",
        "pr-2-bbb222b-service",
    ]
    with pytest.raises(AWSError, match="denied"):
        aws.find_showtime_services()


def test_partial_label_detachment_keeps_status_for_restart(github_fake: Any) -> None:
    """Status is removed last so an interrupted teardown remains discoverable."""
    status = "🎪 abc123f 🚦 failed"
    detail = "🎪 abc123f 📅 2026-01-01T00-00"
    github = github_fake([status, detail])
    github.fail_remove.add(detail)
    pr = PullRequest(1234, list(github.labels))
    show = pr.shows[0]
    show.stop = Mock(return_value=True)  # type: ignore[method-assign]

    with patch("showtime.core.pull_request.get_github", return_value=github):
        result = pr.stop_environment()

    assert result.success is False
    assert status in github.labels
    assert PullRequest(1234, list(github.labels)).shows[0].sha == "abc123f"

    github.fail_remove.clear()
    retry = PullRequest(1234, list(github.labels))
    retry.shows[0].stop = Mock(return_value=True)  # type: ignore[method-assign]
    with patch("showtime.core.pull_request.get_github", return_value=github):
        retry_result = retry.stop_environment()

    assert retry_result.success is True
    assert not any(label.startswith("🎪 abc123f ") for label in github.labels)


def test_per_pr_teardown_never_deletes_shared_label_definitions(github_fake: Any) -> None:
    """PR detachment must not prune repository-wide shared definitions."""
    github = github_fake(["🎪 abc123f 🚦 failed"])
    github.delete_repository_label = Mock()
    pr = PullRequest(1234, list(github.labels))
    pr.shows[0].stop = Mock(return_value=True)  # type: ignore[method-assign]

    with patch("showtime.core.pull_request.get_github", return_value=github):
        assert pr.stop_environment().success

    github.delete_repository_label.assert_not_called()


def test_dry_run_authorization_and_sync_make_no_github_writes(
    github_fake: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Check-only and GitHub dry-run paths must be read-only."""
    github = github_fake(["🎪 ⚡ showtime-trigger-start"])
    github.get_collaborator_permission = Mock(return_value="read")
    pr = PullRequest(1234, list(github.labels))
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_ACTOR", "unauthorized")
    response = Mock(status_code=200)
    response.json.return_value = {"permission": "read"}
    client = Mock()
    client.__enter__ = Mock(return_value=client)
    client.__exit__ = Mock(return_value=False)
    client.get.return_value = response

    with ExitStack() as stack:
        stack.enter_context(patch("showtime.core.pull_request.get_github", return_value=github))
        stack.enter_context(
            patch(
                "showtime.core.pull_request.GitHubInterface.get_actor_debug_info",
                return_value={"actor": "unauthorized", "is_github_actions": True},
            )
        )
        stack.enter_context(patch("httpx.Client", return_value=client))
        pr.analyze("abc123f", dry_run_github=True)
        pr.sync("abc123f", dry_run_github=True, dry_run_aws=True, dry_run_docker=True)

    assert github.writes == []
    assert github.comments == []


def test_comment_failure_does_not_change_healthy_sync_outcome(github_fake: Any) -> None:
    """A presentation failure after promotion must not mark a healthy show failed."""
    github = github_fake(["🎪 ⚡ showtime-trigger-start"])
    github.fail_comments = True
    pr = PullRequest(1234, list(github.labels))
    candidate = Show(1234, "abc123f", "building", created_at="2026-01-01T00-00")
    candidate.build_docker = Mock()  # type: ignore[method-assign]
    candidate.deploy_aws = Mock()  # type: ignore[method-assign]

    with ExitStack() as stack:
        stack.enter_context(patch("showtime.core.pull_request.get_github", return_value=github))
        stack.enter_context(patch.object(pr, "_atomic_claim", return_value=True))
        stack.enter_context(
            patch.object(pr, "_determine_action", return_value="create_environment")
        )
        stack.enter_context(patch.object(pr, "_create_new_show", return_value=candidate))
        stack.enter_context(patch.object(pr, "_show_service_urls"))
        result = pr.sync("abc123f", dry_run_aws=True, dry_run_docker=True)

    assert result.success is True
    assert candidate.status == "running"
    assert "🎪 🎯 abc123f" in github.labels


def test_different_sha_candidate_failure_preserves_old_environment(
    github_fake: Any,
) -> None:
    """Pre-promotion candidate failure must not touch the old pointer or service."""
    old_pointer = "🎪 🎯 old123f"
    labels = [old_pointer, "🎪 old123f 🚦 running"]
    github = github_fake(labels)
    pr = PullRequest(1234, labels)
    old_show = pr.shows[0]
    old_show.stop = Mock()  # type: ignore[method-assign]
    candidate = Show(1234, "new456a", "building", service_created=False)
    candidate.build_docker = Mock()  # type: ignore[method-assign]
    candidate.deploy_aws = Mock(side_effect=RuntimeError("candidate failed"))  # type: ignore[method-assign]

    with ExitStack() as stack:
        stack.enter_context(patch("showtime.core.pull_request.get_github", return_value=github))
        stack.enter_context(patch.object(pr, "_atomic_claim", return_value=True))
        stack.enter_context(
            patch.object(pr, "_determine_action", return_value="create_environment")
        )
        stack.enter_context(patch.object(pr, "_create_new_show", return_value=candidate))
        result = pr.sync("new456a")

    assert result.success is False
    assert old_pointer in github.labels
    old_show.stop.assert_not_called()
    assert "🎪 new456a 🚦 failed" in github.labels


def test_failed_candidate_records_pending_when_compensation_is_refused(
    github_fake: Any,
) -> None:
    """A possibly allocated candidate remains tracked when guarded cleanup fails."""
    labels = ["🎪 old123f 🚦 running", "🎪 🎯 old123f"]
    github = github_fake(labels)
    pr = PullRequest(1234, labels)
    candidate = Show(
        1234,
        "new456a",
        "building",
        task_definition_arn="arn:task/candidate:1",
        service_created=True,
    )
    candidate.build_docker = Mock()  # type: ignore[method-assign]
    candidate.deploy_aws = Mock(side_effect=RuntimeError("readiness failed"))  # type: ignore[method-assign]
    candidate.stop = Mock(return_value=False)  # type: ignore[method-assign]

    with ExitStack() as stack:
        stack.enter_context(patch("showtime.core.pull_request.get_github", return_value=github))
        stack.enter_context(patch.object(pr, "_atomic_claim", return_value=True))
        stack.enter_context(
            patch.object(pr, "_determine_action", return_value="create_environment")
        )
        stack.enter_context(patch.object(pr, "_create_new_show", return_value=candidate))
        result = pr.sync("new456a")

    assert result.cleanup_result is not None
    assert result.cleanup_result.pending_shas == ["new456a"]
    candidate.stop.assert_called_once_with(
        dry_run_github=False,
        dry_run_aws=False,
        require_ownership=True,
        delete_image=False,
    )
    assert "🎪 new456a 🚦 failed" in github.labels
    assert "🎪 new456a 🧹 cleanup-pending" in github.labels
    assert any(label.startswith("🎪 new456a 🧬 ") for label in github.labels)


def test_failed_candidate_distinguishes_successful_compensation(
    github_fake: Any,
) -> None:
    """Confirmed compensation reports deletion without cleanup-pending allocation."""
    github = github_fake([])
    pr = PullRequest(1234, [])
    candidate = Show(
        1234,
        "abc123f",
        "building",
        task_definition_arn="arn:task/candidate:1",
        service_created=True,
    )
    candidate.build_docker = Mock()  # type: ignore[method-assign]
    candidate.deploy_aws = Mock(side_effect=RuntimeError("readiness failed"))  # type: ignore[method-assign]
    candidate.stop = Mock(return_value=True)  # type: ignore[method-assign]

    with ExitStack() as stack:
        stack.enter_context(patch("showtime.core.pull_request.get_github", return_value=github))
        stack.enter_context(patch.object(pr, "_atomic_claim", return_value=True))
        stack.enter_context(
            patch.object(pr, "_determine_action", return_value="create_environment")
        )
        stack.enter_context(patch.object(pr, "_create_new_show", return_value=candidate))
        result = pr.sync("abc123f")

    assert result.cleanup_result is not None
    assert result.cleanup_result.deleted_shas == ["abc123f"]
    assert result.cleanup_result.pending_shas == []
    assert "🎪 abc123f 🧹 cleanup-pending" not in github.labels


def test_partial_pointer_promotion_does_not_delete_previous_environment(
    github_fake: Any,
) -> None:
    """Failure retiring an old pointer keeps the candidate healthy and defers teardown."""
    old_pointer = "🎪 🎯 old123f"
    github = github_fake([old_pointer, "🎪 old123f 🚦 running"])
    github.fail_remove.add(old_pointer)
    pr = PullRequest(1234, list(github.labels))
    old_show = pr.shows[0]
    old_show.stop = Mock()  # type: ignore[method-assign]
    candidate = Show(1234, "new456a", "building")
    candidate.build_docker = Mock()  # type: ignore[method-assign]
    candidate.deploy_aws = Mock()  # type: ignore[method-assign]

    with ExitStack() as stack:
        stack.enter_context(patch("showtime.core.pull_request.get_github", return_value=github))
        stack.enter_context(patch.object(pr, "_atomic_claim", return_value=True))
        stack.enter_context(
            patch.object(pr, "_determine_action", return_value="create_environment")
        )
        stack.enter_context(patch.object(pr, "_create_new_show", return_value=candidate))
        result = pr.sync("new456a", dry_run_aws=True, dry_run_docker=True)

    assert result.action_taken == "promotion_pending"
    assert candidate.status == "running"
    assert "🎪 🎯 new456a" in github.labels
    assert old_pointer in github.labels
    old_show.stop.assert_not_called()


def test_no_action_retry_reconciles_pointer_without_rebuild(
    github_fake: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A restart converges a running target's partial promotion without rebuilding."""
    labels = [
        "🎪 old123f 🚦 running",
        "🎪 🎯 old123f",
        "🎪 new456a 🚦 running",
        "🎪 🎯 new456a",
    ]
    github = github_fake(labels)
    pr = PullRequest(1234, labels)
    target = pr.get_show_by_sha("new456a")
    assert target is not None
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    with ExitStack() as stack:
        stack.enter_context(patch("showtime.core.pull_request.get_github", return_value=github))
        create = stack.enter_context(patch.object(pr, "_create_new_show"))
        build = stack.enter_context(patch.object(Show, "build_docker"))
        deploy = stack.enter_context(patch.object(Show, "deploy_aws"))
        stop = stack.enter_context(patch.object(Show, "stop"))
        result = pr.sync("new456a")

    assert result.success is True
    assert {label for label in github.labels if label.startswith("🎪 🎯 ")} == {"🎪 🎯 new456a"}
    create.assert_not_called()
    build.assert_not_called()
    deploy.assert_not_called()
    stop.assert_not_called()


def test_previous_cleanup_uses_predeployment_running_snapshot(github_fake: Any) -> None:
    """Promotion cleanup excludes unrelated building/in-progress shows."""
    labels = [
        "🎪 old123f 🚦 running",
        "🎪 🎯 old123f",
        "🎪 work789 🚦 building",
    ]
    github = github_fake(labels)
    pr = PullRequest(1234, labels)
    old_show = pr.get_show_by_sha("old123f")
    building = pr.get_show_by_sha("work789")
    assert old_show is not None and building is not None
    old_show.stop = Mock(return_value=True)  # type: ignore[method-assign]
    building.stop = Mock()  # type: ignore[method-assign]
    candidate = Show(1234, "new456a", "building")
    candidate.build_docker = Mock()  # type: ignore[method-assign]
    candidate.deploy_aws = Mock()  # type: ignore[method-assign]

    with ExitStack() as stack:
        stack.enter_context(patch("showtime.core.pull_request.get_github", return_value=github))
        stack.enter_context(patch.object(pr, "_atomic_claim", return_value=True))
        stack.enter_context(
            patch.object(pr, "_determine_action", return_value="create_environment")
        )
        stack.enter_context(patch.object(pr, "_create_new_show", return_value=candidate))
        stack.enter_context(patch.object(pr, "_show_service_urls"))
        result = pr.sync("new456a", dry_run_aws=True, dry_run_docker=True)

    assert result.success is True
    old_show.stop.assert_called_once()
    building.stop.assert_not_called()
    assert "🎪 work789 🚦 building" in github.labels


def test_previous_cleanup_failure_keeps_promoted_candidate_healthy(
    github_fake: Any,
) -> None:
    """Old-service cleanup failure must not rewrite the promoted show as failed."""
    labels = ["🎪 old123f 🚦 running", "🎪 🎯 old123f"]
    github = github_fake(labels)
    pr = PullRequest(1234, labels)
    old_show = pr.shows[0]
    old_show.stop = Mock(return_value=False)  # type: ignore[method-assign]
    candidate = Show(1234, "new456a", "building")
    candidate.build_docker = Mock(  # type: ignore[method-assign]
        return_value="apache/superset@sha256:" + "a" * 64
    )
    candidate.deploy_aws = Mock()  # type: ignore[method-assign]

    with ExitStack() as stack:
        stack.enter_context(patch("showtime.core.pull_request.get_github", return_value=github))
        stack.enter_context(patch.object(pr, "_atomic_claim", return_value=True))
        stack.enter_context(
            patch.object(pr, "_determine_action", return_value="create_environment")
        )
        stack.enter_context(patch.object(pr, "_create_new_show", return_value=candidate))
        stack.enter_context(patch.object(pr, "_show_service_urls"))
        result = pr.sync("new456a", dry_run_aws=False, dry_run_docker=False)

    assert result.success is False
    assert result.cleanup_result is not None
    assert result.cleanup_result.pending_shas == ["old123f"]
    assert candidate.status == "running"
    assert "🎪 🎯 new456a" in github.labels
    assert "🎪 old123f 🚦 running" in github.labels
    assert "🎪 old123f 🧹 cleanup-pending" in github.labels


def test_reconstructed_fingerprint_allows_only_matching_cleanup() -> None:
    """Restarted cleanup compares the persisted fingerprint to observed ownership."""
    arn = "arn:task/candidate:1"
    original = Show(1234, "abc123f", "failed", task_definition_arn=arn, cleanup_pending=True)
    rebuilt = Show.from_circus_labels(1234, original.to_circus_labels(), "abc123f")
    assert rebuilt is not None
    ecs = Mock()
    ecs.describe_services.return_value = {
        "services": [{"status": "ACTIVE", "taskDefinition": arn}],
        "failures": [],
    }
    aws = _aws_with_clients(ecs)

    with patch.object(aws, "_wait_for_service_deletion", return_value=True):
        assert aws.delete_environment(
            rebuilt.aws_service_name,
            rebuilt.pr_number,
            expected_task_definition_fingerprint=rebuilt.task_definition_fingerprint,
            delete_image=False,
        )
    ecs.delete_service.assert_called_once()


def test_describe_failures_remain_present_for_deletion_waiter() -> None:
    """A non-missing describe failure cannot confirm service deletion."""
    ecs = Mock()
    ecs.describe_services.return_value = {
        "services": [],
        "failures": [{"reason": "ACCESS_DENIED"}],
    }
    aws = _aws_with_clients(ecs)

    assert aws._service_exists_any_state("pr-1234-abc123f-service") is True


def test_failed_current_show_deletion_preserves_its_pointer(github_fake: Any) -> None:
    """A stop failure must retain both resource state and an existing pointer."""
    pointer = "🎪 🎯 abc123f"
    github = github_fake([pointer, "🎪 abc123f 🚦 running"])
    pr = PullRequest(1234, list(github.labels))
    pr.shows[0].stop = Mock(return_value=False)  # type: ignore[method-assign]

    with patch("showtime.core.pull_request.get_github", return_value=github):
        result = pr.stop_environment()

    assert result.success is False
    assert pointer in github.labels
    assert "🎪 abc123f 🚦 running" in github.labels
    assert "🎪 abc123f 🧹 cleanup-pending" in github.labels


def test_missing_candidate_ownership_refuses_live_service_deletion() -> None:
    """A live service cannot be deleted by compensation without ownership proof."""
    ecs = Mock()
    ecs.describe_services.return_value = {
        "services": [{"status": "ACTIVE", "taskDefinition": "arn:task/current:2"}],
        "failures": [],
    }
    aws = _aws_with_clients(ecs)

    assert (
        aws.delete_environment(
            "pr-1234-abc123f",
            1234,
            expected_task_definition_fingerprint="",
            delete_image=False,
        )
        is False
    )
    ecs.delete_service.assert_not_called()


def test_create_environment_reports_uncertain_service_creation() -> None:
    """A failed create attempt carries service name, task identity, and uncertainty."""
    aws = _aws_with_clients()
    with ExitStack() as stack:
        stack.enter_context(patch.object(aws, "_service_exists_any_state", return_value=False))
        stack.enter_context(
            patch.object(
                aws,
                "_create_task_definition_with_image_and_flags",
                return_value="arn:task/candidate:1",
            )
        )
        stack.enter_context(patch.object(aws, "_create_ecs_service", return_value=False))
        result = aws.create_environment(1234, "abc123f")

    assert result.success is False
    assert result.service_name == "pr-1234-abc123f-service"
    assert result.task_definition_arn == "arn:task/candidate:1"
    assert result.service_created is None


def test_same_sha_rebuild_warns_that_it_is_destructive(
    github_fake: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    """Same-SHA rebuilds must explicitly warn that no fallback exists."""
    labels = ["🎪 ⚡ showtime-trigger-start", "🎪 abc123f 🚦 running", "🎪 🎯 abc123f"]
    github = github_fake(labels)
    pr = PullRequest(1234, labels)
    candidate = Show(1234, "abc123f", "building")
    candidate.build_docker = Mock(side_effect=RuntimeError("stop after warning"))  # type: ignore[method-assign]

    with ExitStack() as stack:
        stack.enter_context(patch("showtime.core.pull_request.get_github", return_value=github))
        stack.enter_context(patch.object(pr, "_atomic_claim", return_value=True))
        stack.enter_context(
            patch.object(pr, "_determine_action", return_value="create_environment")
        )
        stack.enter_context(patch.object(pr, "_create_new_show", return_value=candidate))
        pr.sync("abc123f")

    output = capsys.readouterr().out.lower()
    assert "destructive" in output
    assert "no fallback" in output


def test_post_promotion_failure_preserves_healthy_candidate(
    github_fake: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A later display failure must never compensate an already promoted show."""
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    github = github_fake(["🎪 aaa111a 🚦 running", "🎪 🎯 aaa111a", "🎪 ⚡ showtime-trigger-start"])
    pr = PullRequest(1234, list(github.labels))
    stopped = []

    def deploy(show: Show, *args: Any, **kwargs: Any) -> None:
        """Record an allocated candidate without contacting AWS."""
        show.service_created = True
        show.task_definition_arn = "arn:task-definition/candidate:2"
        show.ip = "192.0.2.10"

    def stop(show: Show, *args: Any, **kwargs: Any) -> bool:
        """Record the identities that orchestration attempts to delete."""
        stopped.append(show.sha)
        return True

    with ExitStack() as stack:
        stack.enter_context(patch("showtime.core.pull_request.get_github", return_value=github))
        stack.enter_context(patch.object(Show, "build_docker"))
        stack.enter_context(patch.object(Show, "deploy_aws", new=deploy))
        stack.enter_context(patch.object(Show, "stop", new=stop))
        stack.enter_context(
            patch.object(pr, "_show_service_urls", side_effect=BrokenPipeError("output closed"))
        )
        result = pr.sync("bbb222b")

    assert not result.success
    assert result.show is not None and result.show.status == "running"
    assert stopped == ["aaa111a"]
    assert "🎪 🎯 bbb222b" in github.labels
    assert "🎪 bbb222b 🚦 running" in github.labels


def test_failed_rebuild_preserves_pending_allocation_ownership(
    github_fake: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A build failure during retry must retain the prior allocation's fingerprint."""
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    previous = Show(
        1234, "abc123f", "failed", task_definition_arn="arn:task/previous:1", cleanup_pending=True
    )
    github = github_fake(previous.to_circus_labels())
    pr = PullRequest(1234, list(github.labels))

    with ExitStack() as stack:
        stack.enter_context(patch("showtime.core.pull_request.get_github", return_value=github))
        stack.enter_context(
            patch.object(Show, "build_docker", side_effect=RuntimeError("build failed"))
        )
        deploy = stack.enter_context(patch.object(Show, "deploy_aws"))
        stop = stack.enter_context(patch.object(Show, "stop"))
        result = pr.sync("abc123f")

    restored = PullRequest(1234, list(github.labels)).shows[0]
    assert not result.success
    assert restored.cleanup_pending
    assert restored.task_definition_fingerprint == previous.task_definition_fingerprint
    assert restored.status == "failed"
    assert result.cleanup_result is not None and not result.cleanup_result.success
    assert result.cleanup_result.pending_shas == ["abc123f"]
    deploy.assert_not_called()
    stop.assert_not_called()


def test_precreation_failure_retains_previous_pending_ownership() -> None:
    """Registering a definition does not prove the prior allocation was replaced."""
    show = Show(
        1234,
        "abc123f",
        "deploying",
        task_definition_arn="arn:task/previous:1",
        cleanup_pending=True,
    )
    previous_fingerprint = show.task_definition_fingerprint
    aws = Mock()
    aws.create_environment.return_value = EnvironmentResult(
        False,
        error="previous deletion unconfirmed",
        task_definition_arn="arn:task/new:2",
        service_created=False,
    )
    with patch("showtime.core.show.get_interfaces", return_value=(Mock(), aws)):
        with pytest.raises(Exception, match="previous deletion unconfirmed"):
            show.deploy_aws()
    assert show.cleanup_pending
    assert show.task_definition_arn == "arn:task/previous:1"
    assert show.task_definition_fingerprint == previous_fingerprint


def test_created_replacement_replaces_pending_ownership() -> None:
    """Confirmed replacement creation retires the prior allocation fingerprint."""
    show = Show(
        1234,
        "abc123f",
        "deploying",
        task_definition_arn="arn:task/previous:1",
        cleanup_pending=True,
    )
    aws = Mock()
    aws.create_environment.return_value = EnvironmentResult(
        True, task_definition_arn="arn:task/new:2", service_created=True
    )
    with patch("showtime.core.show.get_interfaces", return_value=(Mock(), aws)):
        show.deploy_aws()
    assert not show.cleanup_pending
    assert show.task_definition_arn == "arn:task/new:2"


def test_pointer_recovery_ignores_malformed_creation_time() -> None:
    """Malformed timestamps cannot outrank a valid newer running environment."""
    pr = PullRequest(
        1234,
        [
            "🎪 aaa111a 🚦 running",
            "🎪 🎯 aaa111a",
            "🎪 aaa111a 📅 zzz-invalid",
            "🎪 bbb222b 🚦 running",
            "🎪 🎯 bbb222b",
            "🎪 bbb222b 📅 2026-02-01T00-00",
        ],
    )
    assert pr.current_show is not None
    assert pr.current_show.sha == "bbb222b"
