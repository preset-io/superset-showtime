"""
Tests for PullRequest class - PR-level orchestration
"""

import os
from unittest.mock import Mock, patch

from showtime.core.pull_request import PullRequest, SyncResult
from showtime.core.show import Show
from showtime.core.sync_state import ActionNeeded, AuthStatus, SyncState


def test_pullrequest_creation() -> None:
    """Test basic PullRequest creation"""
    labels = ["🎪 abc123f 🚦 running", "🎪 🎯 abc123f", "bug", "enhancement"]

    pr = PullRequest(1234, labels)

    assert pr.pr_number == 1234
    assert pr.labels == set(labels)
    assert len(pr.shows) == 1
    assert pr.current_show is not None
    assert pr.current_show.sha == "abc123f"


def test_pullrequest_empty() -> None:
    """Test PullRequest with no circus labels"""
    labels = ["bug", "enhancement", "documentation"]

    pr = PullRequest(1234, labels)

    assert len(pr.shows) == 0
    assert pr.current_show is None
    assert pr.has_shows is False  # Property, not method


def test_pullrequest_multiple_shows() -> None:
    """Test PullRequest with multiple shows during update"""
    labels = [
        "🎪 abc123f 🚦 running",  # Old active
        "🎪 def456a 🚦 building",  # New building
        "🎪 🎯 abc123f",  # Active pointer
        "🎪 🏗️ def456a",  # Building pointer
        "🎪 abc123f 📅 2024-01-15T14-30",
        "🎪 def456a 📅 2024-01-15T15-00",
    ]

    pr = PullRequest(1234, labels)

    assert len(pr.shows) == 2
    assert pr.current_show is not None
    assert pr.current_show.sha == "abc123f"
    assert pr.current_show.status == "running"
    assert pr.building_show is not None
    assert pr.building_show.sha == "def456a"
    assert pr.building_show.status == "building"


def test_pullrequest_circus_labels_property() -> None:
    """Test circus_labels property filtering"""
    labels = [
        "🎪 abc123f 🚦 running",
        "bug",
        "🎪 🎯 abc123f",
        "enhancement",
        "🎪 abc123f 📅 2024-01-15T14-30",
    ]

    pr = PullRequest(1234, labels)

    expected_circus = {"🎪 abc123f 🚦 running", "🎪 🎯 abc123f", "🎪 abc123f 📅 2024-01-15T14-30"}

    # Compare as sets since order is not guaranteed from set
    assert set(pr.circus_labels) == expected_circus


def test_pullrequest_get_show_by_sha() -> None:
    """Test getting show by SHA"""
    labels = [
        "🎪 abc123f 🚦 running",
        "🎪 def456a 🚦 building",
        "🎪 🎯 abc123f",
        "🎪 🏗️ def456a",
    ]

    pr = PullRequest(1234, labels)

    show_abc = pr.get_show_by_sha("abc123f")
    assert show_abc is not None
    assert show_abc.sha == "abc123f"
    assert show_abc.status == "running"

    show_def = pr.get_show_by_sha("def456a")
    assert show_def is not None
    assert show_def.sha == "def456a"
    assert show_def.status == "building"

    show_missing = pr.get_show_by_sha("xyz789b")
    assert show_missing is None


def test_pullrequest_determine_action() -> None:
    """Test action determination logic"""
    # Mock GitHub interface to avoid actual API calls
    with patch("showtime.core.pull_request.get_github") as mock_get_github:
        mock_github = Mock()
        mock_get_github.return_value = mock_github

        # No environment, no triggers - no action (no previous environments)
        mock_github.get_labels.return_value = ["bug", "enhancement"]
        pr = PullRequest(1234, ["bug", "enhancement"])
        assert pr._determine_action("abc123f") == "no_action"

        # Start trigger, no environment - create
        mock_github.get_labels.return_value = ["🎪 ⚡ showtime-trigger-start"]
        pr_start = PullRequest(1234, ["🎪 ⚡ showtime-trigger-start"])
        assert pr_start._determine_action("abc123f") == "create_environment"

        # Start trigger, same SHA - force rebuild with trigger
        mock_github.get_labels.return_value = [
            "🎪 ⚡ showtime-trigger-start",
            "🎪 abc123f 🚦 running",
            "🎪 🎯 abc123f",
        ]
        pr_same = PullRequest(
            1234, ["🎪 ⚡ showtime-trigger-start", "🎪 abc123f 🚦 running", "🎪 🎯 abc123f"]
        )
        assert pr_same._determine_action("abc123f") == "create_environment"

        # Start trigger, different SHA - create new environment (SHA-specific logic)
        mock_github.get_labels.return_value = [
            "🎪 ⚡ showtime-trigger-start",
            "🎪 abc123f 🚦 running",
            "🎪 🎯 abc123f",
        ]
        pr_update = PullRequest(
            1234, ["🎪 ⚡ showtime-trigger-start", "🎪 abc123f 🚦 running", "🎪 🎯 abc123f"]
        )
        assert pr_update._determine_action("def456a") == "create_environment"

        # Stop trigger - destroy
        mock_github.get_labels.return_value = [
            "🎪 🛑 showtime-trigger-stop",
            "🎪 abc123f 🚦 running",
            "🎪 🎯 abc123f",
        ]
        pr_stop = PullRequest(
            1234, ["🎪 🛑 showtime-trigger-stop", "🎪 abc123f 🚦 running", "🎪 🎯 abc123f"]
        )
        assert pr_stop._determine_action("def456a") == "destroy_environment"

        # No triggers, but different SHA - create new environment (SHA-specific)
        mock_github.get_labels.return_value = ["🎪 abc123f 🚦 running", "🎪 🎯 abc123f"]
        pr_auto = PullRequest(1234, ["🎪 abc123f 🚦 running", "🎪 🎯 abc123f"])
        assert pr_auto._determine_action("def456a") == "create_environment"

        # Failed environment, no triggers - create new (retry logic)
        mock_github.get_labels.return_value = ["🎪 abc123f 🚦 failed", "🎪 🎯 abc123f"]
        pr_failed = PullRequest(1234, ["🎪 abc123f 🚦 failed", "🎪 🎯 abc123f"])
        assert pr_failed._determine_action("abc123f") == "create_environment"


def test_pullrequest_analyze() -> None:
    """Test analysis functionality"""
    from showtime.core.sync_state import ActionNeeded, SyncState

    with patch("showtime.core.pull_request.get_github") as mock_get_github:
        mock_github = Mock()
        mock_get_github.return_value = mock_github

        labels = ["🎪 ⚡ showtime-trigger-start", "🎪 abc123f 🚦 running", "🎪 🎯 abc123f"]

        # Mock refresh_labels to return the same labels
        mock_github.get_labels.return_value = labels

        pr = PullRequest(1234, labels)

        # Open PR with update needed
        result = pr.analyze("def456a", "open")

        assert isinstance(result, SyncState)
        assert result.action_needed == ActionNeeded.CREATE_ENVIRONMENT  # Trigger present
        assert result.build_needed is True
        assert result.sync_needed is True
        assert result.target_sha == "def456a"

        # Closed PR
        result_closed = pr.analyze("def456a", "closed")
        assert result_closed.action_needed == ActionNeeded.DESTROY_ENVIRONMENT
        assert result_closed.build_needed is False
        assert result_closed.sync_needed is True


def test_pullrequest_get_status() -> None:
    """Test status reporting"""
    # No environment
    pr_empty = PullRequest(1234, ["bug"])
    status = pr_empty.get_status()
    assert status["status"] == "no_environment"
    assert status["show"] is None

    # With environment
    labels = [
        "🎪 abc123f 🚦 running",
        "🎪 🎯 abc123f",
        "🎪 abc123f 🌐 52.1.2.3:8080",
        "🎪 abc123f 📅 2024-01-15T14-30",
        "🎪 abc123f 🤡 maxime",
    ]

    pr = PullRequest(1234, labels)
    status = pr.get_status()

    assert status["status"] == "active"
    assert status["show"]["sha"] == "abc123f"
    assert status["show"]["status"] == "running"
    assert status["show"]["ip"] == "52.1.2.3"
    assert status["show"]["requested_by"] == "maxime"
    assert status["show"]["aws_service_name"] == "pr-1234-abc123f"


def test_pullrequest_create_new_show() -> None:
    """Test new show creation"""
    pr = PullRequest(1234, [])

    # Mock format_utc_now for consistent testing
    # Mock get_current_actor to return "unknown" (CI sets GITHUB_ACTOR env var)
    with patch("showtime.core.date_utils.format_utc_now") as mock_format:
        with patch(
            "showtime.core.pull_request.GitHubInterface.get_current_actor",
            return_value="unknown",
        ):
            mock_format.return_value = "2024-01-15T14-30"

            show = pr._create_new_show("abc123f1234567890abcdef")

            assert show.pr_number == 1234
            assert show.sha == "abc123f"  # Shortened
            assert show.status == "building"
            assert show.created_at == "2024-01-15T14-30"
            assert show.requested_by == "unknown"  # Default when no actor set
            # Note: TTL is now PR-level, not per-Show


@patch("showtime.core.pull_request.get_github")
def test_pullrequest_from_id(mock_get_github: Mock) -> None:
    """Test loading PR from GitHub"""
    mock_github = Mock()
    mock_github.get_labels.return_value = ["🎪 abc123f 🚦 running", "bug"]
    mock_get_github.return_value = mock_github

    pr = PullRequest.from_id(1234)

    assert pr.pr_number == 1234
    assert pr.labels == {"🎪 abc123f 🚦 running", "bug"}  # Labels is now a set
    mock_github.get_labels.assert_called_once_with(1234)


@patch("showtime.core.pull_request.get_github")
def test_pullrequest_refresh_labels(mock_get_github: Mock) -> None:
    """Test refreshing labels from GitHub"""
    mock_github = Mock()
    mock_get_github.return_value = mock_github

    # Initial state
    pr = PullRequest(1234, ["bug"])
    assert len(pr.shows) == 0

    # Mock new labels with shows
    mock_github.get_labels.return_value = ["🎪 abc123f 🚦 running", "🎪 🎯 abc123f", "bug"]

    pr.refresh_labels()

    assert len(pr.shows) == 1
    assert pr.current_show is not None
    assert pr.current_show.sha == "abc123f"
    mock_github.get_labels.assert_called_once_with(1234)


def test_pullrequest_label_parsing_edge_cases() -> None:
    """Test edge cases in label parsing"""
    # Malformed labels should be ignored
    labels = [
        "🎪",  # Too short
        "🎪 abc",  # Too short
        "🎪 toolong123 🚦 running",  # SHA too long
        "🎪 abc123f 🚦 running",  # Valid
        "🎪 🎯 abc123f",  # Valid pointer
        "🎪 invalid format here",  # Invalid
    ]

    pr = PullRequest(1234, labels)

    # Should only parse the valid show
    assert len(pr.shows) == 1
    assert pr.current_show is not None
    assert pr.current_show.sha == "abc123f"


@patch("showtime.core.pull_request.get_github")
def test_pullrequest_list_all_environments(mock_get_github: Mock) -> None:
    """Test listing all environments across PRs"""
    mock_github = Mock()
    mock_github.find_prs_with_shows.return_value = [1234, 5678]
    mock_github.get_labels.side_effect = [
        ["🎪 abc123f 🚦 running", "🎪 🎯 abc123f"],  # PR 1234 - has current_show
        ["🎪 def456a 🚦 running", "🎪 🎯 def456a"],  # PR 5678 - has current_show
    ]
    mock_get_github.return_value = mock_github

    environments = PullRequest.list_all_environments()

    assert len(environments) == 2
    assert environments[0]["pr_number"] == 1234
    assert environments[0]["show"]["sha"] == "abc123f"
    assert environments[1]["pr_number"] == 5678
    assert environments[1]["show"]["sha"] == "def456a"


def test_sync_result_dataclass() -> None:
    """Test SyncResult dataclass"""
    # Success result
    show = Show(pr_number=1234, sha="abc123f", status="running")
    result = SyncResult(success=True, action_taken="create_environment", show=show)

    assert result.success is True
    assert result.action_taken == "create_environment"
    assert result.show == show
    assert result.error is None

    # Error result
    error_result = SyncResult(success=False, action_taken="failed", error="Docker build failed")

    assert error_result.success is False
    assert error_result.action_taken == "failed"
    assert error_result.show is None
    assert error_result.error == "Docker build failed"


def test_sync_state_dataclass() -> None:
    """Test SyncState dataclass"""
    result = SyncState(
        action_needed=ActionNeeded.ROLLING_UPDATE,
        build_needed=True,
        sync_needed=True,
        target_sha="def456a",
        github_actor="test_actor",
        is_github_actions=True,
        permission_level="write",
        auth_status=AuthStatus.AUTHORIZED,
    )

    assert result.action_needed == ActionNeeded.ROLLING_UPDATE
    assert result.build_needed is True
    assert result.sync_needed is True
    assert result.target_sha == "def456a"


def test_pullrequest_no_current_show_properties() -> None:
    """Test properties when no current show exists"""
    pr = PullRequest(1234, ["bug"])

    assert pr.current_show is None
    assert pr.building_show is None
    assert pr.has_shows is False
    assert pr.circus_labels == []


def test_pullrequest_multiple_pointers() -> None:
    """Test handling multiple pointer scenarios"""
    # Both active and building pointers
    labels = [
        "🎪 abc123f 🚦 running",
        "🎪 def456a 🚦 building",
        "🎪 🎯 abc123f",  # Active
        "🎪 🏗️ def456a",  # Building
    ]

    pr = PullRequest(1234, labels)

    current_show = pr.current_show
    building_show = pr.building_show
    assert current_show is not None
    assert building_show is not None
    assert current_show.sha == "abc123f"
    assert building_show.sha == "def456a"
    assert len(pr.shows) == 2


def test_pullrequest_orphaned_shows() -> None:
    """Test shows without proper pointers"""
    # Show data but no pointer labels
    labels = [
        "🎪 abc123f 🚦 running",
        "🎪 abc123f 📅 2024-01-15T14-30",
        # Missing 🎯 pointer - show is created but not current
    ]

    pr = PullRequest(1234, labels)

    # Shows are created based on status labels
    assert len(pr.shows) == 1
    # But there's no current_show without 🎯 pointer
    assert pr.current_show is None


@patch("showtime.core.pull_request.get_github")
def test_pullrequest_find_all_with_environments(mock_get_github: Mock) -> None:
    """Test finding all PRs with environments"""
    mock_github = Mock()
    mock_github.find_prs_with_shows.return_value = [1234, 5678, 9012]
    mock_get_github.return_value = mock_github

    pr_numbers = PullRequest.find_all_with_environments()

    assert pr_numbers == [1234, 5678, 9012]
    mock_github.find_prs_with_shows.assert_called_once()


def test_pullrequest_stop_if_expired() -> None:
    """Test expiration-based cleanup"""
    # Create PR with old environment
    old_time = "2024-01-14T14-30"  # Should be expired after 24h
    labels = [
        "🎪 abc123f 🚦 running",
        "🎪 🎯 abc123f",
        f"🎪 abc123f 📅 {old_time}",
    ]

    pr = PullRequest(1234, labels)

    # Mock the show's expiration check
    with patch.object(pr.current_show, "is_expired", return_value=True):
        with patch.object(
            pr, "stop_environment", return_value=SyncResult(success=True, action_taken="stopped")
        ):
            result = pr.stop_if_expired(24, dry_run=False)
            assert result is True

    # Test dry run
    with patch.object(pr.current_show, "is_expired", return_value=True):
        result = pr.stop_if_expired(24, dry_run=True)
        assert result is True

    # Test not expired
    with patch.object(pr.current_show, "is_expired", return_value=False):
        result = pr.stop_if_expired(24, dry_run=False)
        assert result is False


def test_pullrequest_no_environment_methods() -> None:
    """Test methods when no environment exists"""
    with patch("showtime.core.pull_request.get_github") as mock_get_github:
        mock_github = Mock()
        mock_get_github.return_value = mock_github

        pr = PullRequest(1234, ["bug"])

        # stop_environment with no environment - still succeeds (removes labels)
        result = pr.stop_environment(dry_run_github=True)
        assert result.success is True
        assert result.action_taken == "stopped"

        # stop_if_expired with no environment
        assert pr.stop_if_expired(24) is False


@patch("showtime.core.pull_request.get_github")
def test_pullrequest_sync_create_environment(mock_get_github: Mock) -> None:
    """Test sync method creating new environment"""
    mock_github = Mock()
    mock_get_github.return_value = mock_github

    # Mock refresh_labels to return start trigger
    mock_github.get_labels.return_value = ["🎪 ⚡ showtime-trigger-start"]

    # PR with start trigger, no existing environment
    pr = PullRequest(1234, ["🎪 ⚡ showtime-trigger-start"])

    # Mock the atomic claim and other operations
    with patch.object(pr, "_atomic_claim", return_value=True):
        with patch.object(pr, "_create_new_show") as mock_create:
            with patch.object(pr, "_post_building_comment"):
                with patch.object(pr, "_update_show_labels"):
                    with patch.object(pr, "_post_success_comment"):
                        # Mock show for testing
                        mock_show = Show(pr_number=1234, sha="abc123f", status="building")
                        mock_create.return_value = mock_show

                        # Mock show methods
                        mock_show.build_docker = Mock()  # type: ignore[method-assign]
                        mock_show.deploy_aws = Mock()  # type: ignore[method-assign]

                        result = pr.sync(
                            "abc123f", dry_run_github=True, dry_run_aws=True, dry_run_docker=True
                        )

                        assert result.success is True
                        assert result.action_taken == "create_environment"
                        assert result.show == mock_show

                        # Verify state transitions
                        mock_show.build_docker.assert_called_once_with(True)
                        mock_show.deploy_aws.assert_called_once_with(True)
                        assert mock_show.status == "running"


@patch("showtime.core.pull_request.get_github")
def test_pullrequest_sync_same_sha_no_action(mock_get_github: Mock) -> None:
    """Test sync method when no action needed (same SHA, healthy environment)"""
    mock_github = Mock()
    mock_get_github.return_value = mock_github

    # Mock refresh_labels to return existing environment
    mock_github.get_labels.return_value = [
        "🎪 abc123f 🚦 running",
        "🎪 🎯 abc123f",
        "bug",
        "enhancement",
    ]

    # PR with existing healthy environment, same SHA, no triggers
    pr = PullRequest(1234, ["🎪 abc123f 🚦 running", "🎪 🎯 abc123f", "bug", "enhancement"])

    result = pr.sync("abc123f")  # Same SHA as current

    assert result.success is True
    assert result.action_taken == "no_action"
    assert result.show is None


@patch("showtime.core.pull_request.get_github")
def test_pullrequest_sync_rolling_update(mock_get_github: Mock) -> None:
    """Test sync method performing rolling update"""
    mock_github = Mock()
    mock_get_github.return_value = mock_github

    # Mock refresh_labels to return start trigger with existing environment
    mock_github.get_labels.return_value = [
        "🎪 ⚡ showtime-trigger-start",
        "🎪 abc123f 🚦 running",
        "🎪 🎯 abc123f",
    ]

    # PR with existing environment and start trigger
    pr = PullRequest(
        1234, ["🎪 ⚡ showtime-trigger-start", "🎪 abc123f 🚦 running", "🎪 🎯 abc123f"]
    )

    with patch.object(pr, "_atomic_claim", return_value=True):
        with patch.object(pr, "_create_new_show") as mock_create:
            with patch.object(pr, "_post_building_comment"):
                with patch.object(pr, "_update_show_labels"):
                    with patch.object(pr, "_post_success_comment"):
                        # Mock new show
                        mock_new_show = Show(pr_number=1234, sha="def456a", status="building")
                        mock_create.return_value = mock_new_show

                        mock_new_show.build_docker = Mock()  # type: ignore[method-assign]
                        mock_new_show.deploy_aws = Mock()  # type: ignore[method-assign]

                        result = pr.sync(
                            "def456a", dry_run_github=True, dry_run_aws=True, dry_run_docker=True
                        )

                        assert result.success is True
                        # With start trigger and different SHA, should create environment
                        assert result.action_taken == "create_environment"
                        assert result.show == mock_new_show


@patch("showtime.core.pull_request.get_github")
def test_pullrequest_sync_destroy_environment(mock_get_github: Mock) -> None:
    """Test sync method destroying environment"""
    mock_github = Mock()
    mock_get_github.return_value = mock_github

    # Mock refresh_labels to return stop trigger with existing environment
    mock_github.get_labels.return_value = [
        "🎪 🛑 showtime-trigger-stop",
        "🎪 abc123f 🚦 running",
        "🎪 🎯 abc123f",
    ]

    # PR with stop trigger and existing environment
    pr = PullRequest(
        1234, ["🎪 🛑 showtime-trigger-stop", "🎪 abc123f 🚦 running", "🎪 🎯 abc123f"]
    )

    with patch.object(pr, "_atomic_claim", return_value=True):
        # Mock at the Show class level since refresh_labels creates new Show objects
        with patch("showtime.core.show.Show.stop", return_value=True) as mock_stop:
            with patch.object(pr, "_post_cleanup_comment"):
                result = pr.sync("abc123f", dry_run_github=True, dry_run_aws=True)

                assert result.success is True
                assert result.action_taken == "destroy_environment"
                mock_stop.assert_called_once_with(dry_run_github=True, dry_run_aws=True)


@patch("showtime.core.pull_request.get_github")
def test_pullrequest_sync_claim_failed(mock_get_github: Mock) -> None:
    """Test sync method when atomic claim fails"""
    mock_github = Mock()
    mock_get_github.return_value = mock_github

    # Mock refresh_labels to return start trigger
    mock_github.get_labels.return_value = ["🎪 ⚡ showtime-trigger-start"]

    pr = PullRequest(1234, ["🎪 ⚡ showtime-trigger-start"])

    with patch.object(pr, "_atomic_claim", return_value=False):
        result = pr.sync("abc123f")

        assert result.success is False
        assert result.action_taken == "claim_failed"
        assert result.error is not None
        assert "Another job is already active" in result.error


@patch("showtime.core.pull_request.get_github")
def test_pullrequest_atomic_claim_success(mock_get_github: Mock) -> None:
    """Test successful atomic claim"""
    mock_github = Mock()
    mock_get_github.return_value = mock_github

    # Mock refresh_labels to return start trigger
    mock_github.get_labels.return_value = ["🎪 ⚡ showtime-trigger-start"]

    pr = PullRequest(1234, ["🎪 ⚡ showtime-trigger-start"])

    # Mock GitHub operations
    mock_github.remove_label = Mock()
    mock_github.add_label = Mock()

    with patch.object(pr, "_create_new_show") as mock_create:
        mock_show = Show(pr_number=1234, sha="abc123f", status="building")
        mock_create.return_value = mock_show

        with patch.object(pr, "remove_label") as mock_remove_label:
            with patch.object(pr, "remove_sha_labels") as mock_remove_sha:
                with patch.object(pr, "add_label"):
                    result = pr._atomic_claim("abc123f", "create_environment", dry_run=False)

                    assert result is True
                    # Verify trigger labels removed
                    mock_remove_label.assert_called_with("🎪 ⚡ showtime-trigger-start")
                    # Verify SHA labels removed
                    mock_remove_sha.assert_called_with("abc123f")


@patch("showtime.core.pull_request.get_github")
def test_pullrequest_atomic_claim_dry_run(mock_get_github: Mock) -> None:
    """Test atomic claim in dry run mode"""
    mock_github = Mock()
    mock_get_github.return_value = mock_github

    # Mock refresh_labels to return start trigger
    mock_github.get_labels.return_value = ["🎪 ⚡ showtime-trigger-start"]

    pr = PullRequest(1234, ["🎪 ⚡ showtime-trigger-start"])

    result = pr._atomic_claim("abc123f", "create_environment", dry_run=True)

    assert result is True
    # Should refresh labels even in dry run (for validation)
    mock_github.get_labels.assert_called()
    # But should not make other GitHub calls in dry run
    assert not mock_github.remove_label.called
    assert not mock_github.add_label.called


@patch("showtime.core.pull_request.get_github")
def test_pullrequest_start_environment_wrapper(mock_get_github: Mock) -> None:
    """Test start_environment wrapper method"""
    mock_github = Mock()
    mock_github.get_latest_commit_sha.return_value = "abc123f1234567890"
    mock_get_github.return_value = mock_github

    pr = PullRequest(1234, [])

    with patch.object(pr, "sync") as mock_sync:
        mock_sync.return_value = SyncResult(success=True, action_taken="create_environment")

        # Test with explicit SHA
        pr.start_environment(sha="def456a", dry_run_aws=True)
        mock_sync.assert_called_once_with("def456a", dry_run_aws=True)

        # Test without SHA (should get latest)
        pr.start_environment()
        # Second call should use the fetched SHA
        assert mock_sync.call_args[0][0] == "abc123f1234567890"


@patch("showtime.core.pull_request.get_github")
def test_pullrequest_update_show_labels(mock_get_github: Mock) -> None:
    """Test differential label updates"""
    mock_github = Mock()
    mock_github.add_label = Mock()
    mock_github.remove_label = Mock()
    mock_get_github.return_value = mock_github

    # PR with some existing labels
    pr = PullRequest(
        1234,
        [
            "🎪 abc123f 🚦 building",  # Will be updated to running
            "🎪 🎯 abc123f",  # Will stay
            "🎪 abc123f 📅 2024-01-15T14-30",  # Will stay
        ],
    )

    # Show with new state
    show = Show(
        pr_number=1234,
        sha="abc123f",
        status="running",  # Changed from building
        created_at="2024-01-15T14-30",
        ip="52.1.2.3",  # New
    )

    with patch.object(pr, "refresh_labels"):
        pr._update_show_labels(show, dry_run=False)

        # Should add IP label and update status
        mock_github.add_label.assert_called()
        mock_github.remove_label.assert_called()


@patch("showtime.core.pull_request.get_github")
def test_pullrequest_update_show_labels_status_replacement(mock_get_github: Mock) -> None:
    """Test that status updates properly remove old status labels"""
    mock_github = Mock()
    mock_get_github.return_value = mock_github

    # PR with multiple status labels (the bug scenario)
    pr = PullRequest(
        1234,
        [
            "🎪 abc123f 🚦 building",  # Old status
            "🎪 abc123f 🚦 failed",  # Another old status
            "🎪 🎯 abc123f",  # Pointer
            "🎪 abc123f 📅 2024-01-15T14-30",
        ],
    )

    # Show transitioning to running
    show = Show(
        pr_number=1234,
        sha="abc123f",
        status="running",  # New status
        created_at="2024-01-15T14-30",
    )

    with patch.object(pr, "refresh_labels"):
        pr._update_show_labels(show, dry_run=False)

        # Should remove BOTH old status labels
        remove_calls = [call.args[1] for call in mock_github.remove_label.call_args_list]
        assert "🎪 abc123f 🚦 building" in remove_calls
        assert "🎪 abc123f 🚦 failed" in remove_calls

        # Should add new status label
        add_calls = [call.args[1] for call in mock_github.add_label.call_args_list]
        assert "🎪 abc123f 🚦 running" in add_calls


# Test new centralized label management methods


@patch("showtime.core.pull_request.get_github")
def test_pullrequest_add_label_with_logging(mock_get_github: Mock) -> None:
    """Test PullRequest.add_label() with logging and state update"""
    mock_github = Mock()
    mock_get_github.return_value = mock_github

    pr = PullRequest(1234, ["existing-label"])

    # Test adding new label
    pr.add_label("new-label")

    # Should call GitHub API
    mock_github.add_label.assert_called_once_with(1234, "new-label")

    # Should update local state
    assert "new-label" in pr.labels
    assert "existing-label" in pr.labels


@patch("showtime.core.pull_request.get_github")
def test_pullrequest_remove_label_with_logging(mock_get_github: Mock) -> None:
    """Test PullRequest.remove_label() with logging and state update"""
    mock_github = Mock()
    mock_get_github.return_value = mock_github

    pr = PullRequest(1234, ["label1", "label2"])

    # Test removing existing label
    pr.remove_label("label1")

    # Should call GitHub API
    mock_github.remove_label.assert_called_once_with(1234, "label1")

    # Should update local state
    assert "label1" not in pr.labels
    assert "label2" in pr.labels

    # Test removing non-existent label (should be safe)
    pr.remove_label("nonexistent")
    assert len(mock_github.remove_label.call_args_list) == 2


@patch("showtime.core.pull_request.get_github")
def test_pullrequest_remove_sha_labels(mock_get_github: Mock) -> None:
    """Test PullRequest.remove_sha_labels() for SHA-specific cleanup"""
    mock_github = Mock()
    mock_github.get_labels.return_value = [
        "🎪 abc123f 🚦 building",
        "🎪 abc123f 📅 2025-08-26",
        "🎪 def456a 🚦 running",  # Different SHA
        "🎪 🎯 def456a",  # Different SHA
        "regular-label",
    ]
    mock_get_github.return_value = mock_github

    initial_labels = [
        "🎪 abc123f 🚦 building",
        "🎪 abc123f 📅 2025-08-26",
        "🎪 def456a 🚦 running",  # Different SHA
        "🎪 🎯 def456a",  # Different SHA
        "regular-label",
    ]

    pr = PullRequest(1234, initial_labels)

    # Test removing labels for specific SHA
    pr.remove_sha_labels("abc123f789")  # Full SHA

    # Should call GitHub API for abc123f labels only
    remove_calls = [call.args[1] for call in mock_github.remove_label.call_args_list]
    assert "🎪 abc123f 🚦 building" in remove_calls
    assert "🎪 abc123f 📅 2025-08-26" in remove_calls
    assert "🎪 def456a 🚦 running" not in remove_calls
    assert "🎪 🎯 def456a" not in remove_calls
    assert "regular-label" not in remove_calls


@patch("showtime.core.pull_request.get_github")
def test_pullrequest_remove_showtime_labels(mock_get_github: Mock) -> None:
    """Test PullRequest.remove_showtime_labels() for complete cleanup"""
    mock_github = Mock()
    mock_github.get_labels.return_value = [
        "🎪 abc123f 🚦 running",
        "🎪 🎯 abc123f",
        "🎪 def456a 🚦 building",
        "regular-label",
        "bug",
    ]
    mock_get_github.return_value = mock_github

    initial_labels = [
        "🎪 abc123f 🚦 running",
        "🎪 🎯 abc123f",
        "🎪 def456a 🚦 building",
        "regular-label",
        "bug",
    ]

    pr = PullRequest(1234, initial_labels)

    # Test removing all showtime labels
    pr.remove_showtime_labels()

    # Should call GitHub API for all circus labels
    remove_calls = [call.args[1] for call in mock_github.remove_label.call_args_list]
    assert "🎪 abc123f 🚦 running" in remove_calls
    assert "🎪 🎯 abc123f" in remove_calls
    assert "🎪 def456a 🚦 building" in remove_calls
    assert "regular-label" not in remove_calls
    assert "bug" not in remove_calls


@patch("showtime.core.pull_request.get_github")
def test_pullrequest_set_show_status(mock_get_github: Mock) -> None:
    """Test PullRequest.set_show_status() atomic status transitions"""
    mock_github = Mock()
    mock_github.get_labels.return_value = [
        "🎪 abc123f 🚦 building",
        "🎪 abc123f 🚦 failed",  # Duplicate/stale status
        "🎪 abc123f 📅 2025-08-26",
    ]
    mock_get_github.return_value = mock_github

    pr = PullRequest(1234, [])
    show = Show(pr_number=1234, sha="abc123f", status="building")

    # Test status transition with cleanup
    pr.set_show_status(show, "deploying")

    # Should remove all existing status labels
    remove_calls = [call.args[1] for call in mock_github.remove_label.call_args_list]
    assert "🎪 abc123f 🚦 building" in remove_calls
    assert "🎪 abc123f 🚦 failed" in remove_calls

    # Should add new status label
    add_calls = [call.args[1] for call in mock_github.add_label.call_args_list]
    assert "🎪 abc123f 🚦 deploying" in add_calls

    # Should update show status
    assert show.status == "deploying"


@patch("showtime.core.pull_request.get_github")
def test_pullrequest_set_active_show(mock_get_github: Mock) -> None:
    """Test PullRequest.set_active_show() atomic active pointer management"""
    mock_github = Mock()
    mock_github.get_labels.return_value = [
        "🎪 🎯 old123f",  # Old active pointer
        "🎪 🎯 other456",  # Another old pointer
        "🎪 abc123f 🚦 running",
    ]
    mock_get_github.return_value = mock_github

    pr = PullRequest(1234, [])
    show = Show(pr_number=1234, sha="abc123f", status="running")

    # Test setting active show
    pr.set_active_show(show)

    # Should remove all existing active pointers
    remove_calls = [call.args[1] for call in mock_github.remove_label.call_args_list]
    assert "🎪 🎯 old123f" in remove_calls
    assert "🎪 🎯 other456" in remove_calls

    # Should add new active pointer
    add_calls = [call.args[1] for call in mock_github.add_label.call_args_list]
    assert "🎪 🎯 abc123f" in add_calls


@patch("showtime.core.pull_request.get_github")
def test_pullrequest_blocked_state(mock_get_github: Mock) -> None:
    """Test that blocked state prevents all operations"""
    mock_github = Mock()
    mock_github.get_labels.return_value = [
        "🎪 🔒 showtime-blocked",
        "🎪 abc123f 🚦 running",  # Existing environment
        "🎪 ⚡ showtime-trigger-start",  # Trigger should be ignored
    ]
    mock_get_github.return_value = mock_github

    pr = PullRequest(1234, [])

    # Test sync with blocked state
    result = pr.sync("def456a")

    # Should fail with blocked error
    assert result.success is False
    assert result.action_taken == "blocked"
    error_msg = result.error
    assert error_msg is not None
    assert "🔒 Showtime operations are blocked" in error_msg
    assert "showtime-blocked" in error_msg

    # Should not perform any operations
    assert not mock_github.add_label.called
    assert not mock_github.remove_label.called


@patch("showtime.core.pull_request.get_github")
def test_pullrequest_determine_action_blocked(mock_get_github: Mock) -> None:
    """Test _determine_action returns 'blocked' when blocked label present"""
    mock_github = Mock()
    mock_github.get_labels.return_value = ["🎪 🔒 showtime-blocked", "🎪 ⚡ showtime-trigger-start"]
    mock_get_github.return_value = mock_github

    pr = PullRequest(1234, ["🎪 🔒 showtime-blocked", "🎪 ⚡ showtime-trigger-start"])

    action = pr._determine_action("abc123f")

    assert action == "blocked"


@patch.dict(os.environ, {"GITHUB_ACTIONS": "true", "GITHUB_ACTOR": "external-user"})
@patch("showtime.core.pull_request.get_github")
def test_pullrequest_authorization_check_unauthorized(mock_get_github: Mock) -> None:
    """Test authorization check blocks unauthorized users"""
    mock_github = Mock()
    mock_github.base_url = "https://api.github.com"
    mock_github.org = "apache"
    mock_github.repo = "superset"
    mock_github.headers = {"Authorization": "Bearer token"}

    # Mock unauthorized response
    mock_response = Mock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"permission": "read"}  # Not write/admin

    with patch("httpx.Client") as mock_client_class:
        mock_client = Mock()
        mock_client.get.return_value = mock_response
        mock_client_instance = Mock()
        mock_client_instance.__enter__ = Mock(return_value=mock_client)
        mock_client_instance.__exit__ = Mock(return_value=None)
        mock_client_class.return_value = mock_client_instance

        mock_get_github.return_value = mock_github

        pr = PullRequest(1234, [])

        # Test unauthorized actor
        authorized, debug_info = pr._check_authorization()

        assert authorized is False
        assert debug_info["auth_status"] == "denied_insufficient_perms"
        # Should have added blocked label
        mock_github.add_label.assert_called_once_with(1234, "🎪 🔒 showtime-blocked")


@patch.dict(os.environ, {"GITHUB_ACTIONS": "true", "GITHUB_ACTOR": "maintainer-user"})
@patch("showtime.core.pull_request.get_github")
def test_pullrequest_authorization_check_authorized(mock_get_github: Mock) -> None:
    """Test authorization check allows authorized users"""
    mock_github = Mock()
    mock_github.base_url = "https://api.github.com"
    mock_github.org = "apache"
    mock_github.repo = "superset"
    mock_github.headers = {"Authorization": "Bearer token"}

    # Mock authorized response
    mock_response = Mock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"permission": "write"}  # Authorized

    with patch("httpx.Client") as mock_client_class:
        mock_client = Mock()
        mock_client.get.return_value = mock_response
        mock_client_instance = Mock()
        mock_client_instance.__enter__ = Mock(return_value=mock_client)
        mock_client_instance.__exit__ = Mock(return_value=None)
        mock_client_class.return_value = mock_client_instance

        mock_get_github.return_value = mock_github

        pr = PullRequest(1234, [])

        # Test authorized actor
        authorized, debug_info = pr._check_authorization()

        assert authorized is True
        assert debug_info["permission"] == "write"
        # Should not add blocked label
        assert not mock_github.add_label.called


@patch.dict(os.environ, {"GITHUB_ACTIONS": "false"})
def test_pullrequest_authorization_check_local() -> None:
    """Test authorization check skipped in non-GHA environment"""
    pr = PullRequest(1234, [])

    # Should always return True for local development
    authorized, debug_info = pr._check_authorization()

    assert authorized is True
    assert debug_info["auth_status"] == "skipped_not_actions"
