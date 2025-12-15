"""
Tests for circus tent label state transitions and cleanup
"""

from typing import Any
from unittest.mock import Mock, patch

from showtime.core.pull_request import PullRequest
from showtime.core.show import Show


@patch("showtime.core.pull_request.get_github")
def test_status_transition_building_to_running(mock_get_github: Any) -> None:
    """Test clean transition from building to running state"""
    mock_github = Mock()
    mock_get_github.return_value = mock_github

    # Start with building state
    pr = PullRequest(
        1234,
        [
            "🎪 abc123f 🚦 building",
            "🎪 🎯 abc123f",
            "🎪 abc123f 📅 2024-01-15T14-30",
        ],
    )

    # Transition to running
    show = Show(
        pr_number=1234,
        sha="abc123f",
        status="running",  # New status
        created_at="2024-01-15T14-30",
        ip="52.1.2.3",  # Added during deployment
    )

    with patch.object(pr, "refresh_labels"):
        pr._update_show_labels(show, dry_run=False)

        # Should remove old building status
        remove_calls = [call.args[1] for call in mock_github.remove_label.call_args_list]
        assert "🎪 abc123f 🚦 building" in remove_calls

        # Should add running status and IP
        add_calls = [call.args[1] for call in mock_github.add_label.call_args_list]
        assert "🎪 abc123f 🚦 running" in add_calls
        assert "🎪 abc123f 🌐 52.1.2.3:8080" in add_calls


@patch("showtime.core.pull_request.get_github")
def test_status_transition_building_to_failed(mock_get_github: Any) -> None:
    """Test transition from building to failed state (Docker failure)"""
    mock_github = Mock()
    mock_get_github.return_value = mock_github

    # Start with building state
    pr = PullRequest(
        1234,
        [
            "🎪 abc123f 🚦 building",
            "🎪 🏗️ abc123f",  # Building pointer
            "🎪 abc123f 📅 2024-01-15T14-30",
        ],
    )

    # Docker build fails, transition to failed
    show = Show(
        pr_number=1234,
        sha="abc123f",
        status="failed",  # Build failed
        created_at="2024-01-15T14-30",
    )

    with patch.object(pr, "refresh_labels"):
        pr._update_show_labels(show, dry_run=False)

        # Should remove building status
        remove_calls = [call.args[1] for call in mock_github.remove_label.call_args_list]
        assert "🎪 abc123f 🚦 building" in remove_calls

        # Should add failed status
        add_calls = [call.args[1] for call in mock_github.add_label.call_args_list]
        assert "🎪 abc123f 🚦 failed" in add_calls


@patch("showtime.core.pull_request.get_github")
def test_multiple_orphaned_status_cleanup(mock_get_github: Any) -> None:
    """Test cleanup of multiple orphaned status labels (the bug scenario)"""
    mock_github = Mock()
    mock_get_github.return_value = mock_github

    # PR with multiple orphaned status labels from previous failed transitions
    pr = PullRequest(
        1234,
        [
            "🎪 abc123f 🚦 building",  # Old status 1
            "🎪 abc123f 🚦 failed",  # Old status 2
            "🎪 abc123f 🚦 deploying",  # Old status 3
            "🎪 🎯 abc123f",  # Pointer
            "🎪 abc123f 📅 2024-01-15T14-30",
            "🎪 abc123f 🤡 maxime",
        ],
    )

    # Clean transition to running
    show = Show(
        pr_number=1234,
        sha="abc123f",
        status="running",
        created_at="2024-01-15T14-30",
        requested_by="maxime",
        ip="52.1.2.3",
    )

    with patch.object(pr, "refresh_labels"):
        pr._update_show_labels(show, dry_run=False)

        # Should remove ALL orphaned status labels
        remove_calls = [call.args[1] for call in mock_github.remove_label.call_args_list]
        assert "🎪 abc123f 🚦 building" in remove_calls
        assert "🎪 abc123f 🚦 failed" in remove_calls
        assert "🎪 abc123f 🚦 deploying" in remove_calls

        # Should add only the new running status
        add_calls = [call.args[1] for call in mock_github.add_label.call_args_list]
        running_status_calls = [call for call in add_calls if "🚦 running" in call]
        assert len(running_status_calls) == 1
        assert "🎪 abc123f 🚦 running" in add_calls


@patch("showtime.core.pull_request.get_github")
def test_status_transition_with_concurrent_changes(mock_get_github: Any) -> None:
    """Test status transition doesn't interfere with other SHA labels"""
    mock_github = Mock()
    mock_get_github.return_value = mock_github

    # PR with two environments - one transitioning, one stable
    pr = PullRequest(
        1234,
        [
            "🎪 abc123f 🚦 building",  # Transitioning environment
            "🎪 def456a 🚦 running",  # Stable environment
            "🎪 🎯 abc123f",  # Active pointer
            "🎪 🏗️ def456a",  # Building pointer
            "🎪 abc123f 📅 2024-01-15T14-30",
            "🎪 def456a 📅 2024-01-15T15-00",
        ],
    )

    # Transition abc123f to running (should not affect def456a)
    show = Show(
        pr_number=1234,
        sha="abc123f",
        status="running",
        created_at="2024-01-15T14-30",
        ip="52.1.2.3",
    )

    with patch.object(pr, "refresh_labels"):
        pr._update_show_labels(show, dry_run=False)

        # Should only remove abc123f building status
        remove_calls = [call.args[1] for call in mock_github.remove_label.call_args_list]
        assert "🎪 abc123f 🚦 building" in remove_calls

        # Should NOT remove def456a status
        assert "🎪 def456a 🚦 running" not in remove_calls

        # Should add abc123f running status
        add_calls = [call.args[1] for call in mock_github.add_label.call_args_list]
        assert "🎪 abc123f 🚦 running" in add_calls


@patch("showtime.core.pull_request.get_github")
def test_status_replacement_preserves_other_labels(mock_get_github: Any) -> None:
    """Test that status replacement preserves non-status labels"""
    mock_github = Mock()
    mock_get_github.return_value = mock_github

    # PR with various label types
    pr = PullRequest(
        1234,
        [
            "🎪 abc123f 🚦 building",  # Status - should be replaced
            "🎪 🎯 abc123f",  # Pointer - should stay
            "🎪 abc123f 📅 2024-01-15T14-30",  # Timestamp - should stay
            "🎪 ⌛ 24h",  # PR-level TTL - should stay
            "🎪 abc123f 🤡 maxime",  # User - should stay
            "bug",  # Non-circus - should stay
            "enhancement",  # Non-circus - should stay
        ],
    )

    # Same show, just status change
    show = Show(
        pr_number=1234,
        sha="abc123f",
        status="failed",  # Status changed
        created_at="2024-01-15T14-30",
        requested_by="maxime",
    )

    with patch.object(pr, "refresh_labels"):
        pr._update_show_labels(show, dry_run=False)

        # Should remove only building status
        remove_calls = [call.args[1] for call in mock_github.remove_label.call_args_list]
        assert "🎪 abc123f 🚦 building" in remove_calls
        assert len([call for call in remove_calls if "🎪 abc123f" in call]) == 1

        # Should add only failed status (other labels already exist)
        add_calls = [call.args[1] for call in mock_github.add_label.call_args_list]
        assert "🎪 abc123f 🚦 failed" in add_calls


@patch("showtime.core.pull_request.get_github")
def test_status_transition_dry_run_mode(mock_get_github: Any) -> None:
    """Test that dry run mode doesn't make GitHub API calls"""
    mock_github = Mock()
    mock_get_github.return_value = mock_github

    pr = PullRequest(
        1234,
        [
            "🎪 abc123f 🚦 building",
            "🎪 🎯 abc123f",
        ],
    )

    show = Show(pr_number=1234, sha="abc123f", status="running")

    # Dry run should not make any API calls
    pr._update_show_labels(show, dry_run=True)

    mock_github.remove_label.assert_not_called()
    mock_github.add_label.assert_not_called()


@patch("showtime.core.pull_request.get_github")
def test_no_status_labels_to_clean(mock_get_github: Any) -> None:
    """Test behavior when no existing status labels exist"""
    mock_github = Mock()
    mock_get_github.return_value = mock_github

    # PR with no status labels
    pr = PullRequest(
        1234,
        [
            "🎪 🎯 abc123f",  # Just pointer
            "bug",
            "enhancement",
        ],
    )

    # Add first status
    show = Show(pr_number=1234, sha="abc123f", status="building", created_at="2024-01-15T14-30")

    with patch.object(pr, "refresh_labels"):
        pr._update_show_labels(show, dry_run=False)

        # Should not try to remove any status labels
        remove_calls = [call.args[1] for call in mock_github.remove_label.call_args_list]
        status_removes = [call for call in remove_calls if "🚦" in call]
        assert len(status_removes) == 0

        # Should add new status and timestamp
        add_calls = [call.args[1] for call in mock_github.add_label.call_args_list]
        assert "🎪 abc123f 🚦 building" in add_calls


def test_status_label_identification_edge_cases() -> None:
    """Test edge cases in status label identification"""

    # Test various malformed labels that should not be treated as status
    labels = [
        "🎪 abc123f 🚦 running",  # Valid status
        "🎪 abc123f🚦building",  # No spaces - invalid
        "🎪 abc123f 🚦",  # No status value - invalid
        "🎪 🚦 building abc123f",  # Wrong order - invalid
        "🎪 abc123 🚦 failed",  # Wrong SHA length - invalid
        "🎪 abc123f 🚦 weird-status",  # Valid format, weird status
        "🎪 def456a 🚦 running",  # Different SHA - should not match
    ]

    # Test that status filtering only matches the correct SHA
    sha_status_labels = [label for label in labels if label.startswith("🎪 abc123f 🚦 ")]

    # Should match exactly 2: running and weird-status
    assert len(sha_status_labels) == 2
    assert "🎪 abc123f 🚦 running" in sha_status_labels
    assert "🎪 abc123f 🚦 weird-status" in sha_status_labels

    # Should not match other SHAs or malformed labels
    assert "🎪 def456a 🚦 running" not in sha_status_labels
    assert "🎪 abc123f🚦building" not in sha_status_labels


@patch("showtime.core.pull_request.get_github")
def test_atomic_claim_actually_creates_labels(mock_get_github: Any) -> None:
    """Test that atomic claim ACTUALLY creates labels, not just claims success"""
    mock_github = Mock()
    mock_github.get_labels.return_value = ["🎪 ⚡ showtime-trigger-start", "bug"]
    mock_get_github.return_value = mock_github

    pr = PullRequest(1234, ["🎪 ⚡ showtime-trigger-start", "bug"])

    # Mock show creation
    with patch.object(pr, "_create_new_show") as mock_create:
        mock_show = Show(pr_number=1234, sha="abc123f", status="building")
        mock_create.return_value = mock_show

        result = pr._atomic_claim("abc123f", "create_environment", dry_run=False)

        assert result is True

        # The CRITICAL assertions - verify actual label operations happened
        mock_github.remove_label.assert_called()  # Should remove triggers
        mock_github.add_label.assert_called()  # Should add building labels

        # Verify trigger was removed
        trigger_removes = [
            call
            for call in mock_github.remove_label.call_args_list
            if "showtime-trigger-start" in str(call)
        ]
        assert len(trigger_removes) > 0, "Trigger label should be removed"

        # Verify building labels were added
        building_adds = [
            call for call in mock_github.add_label.call_args_list if "🚦 building" in str(call)
        ]
        assert len(building_adds) > 0, "Building status label should be added"
