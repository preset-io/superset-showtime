"""
Tests for GitHub label cleanup: pagination, orphan detection, and repo-level deletion.
"""

from typing import Any
from unittest.mock import Mock, patch

import pytest

from showtime.core.github import GitHubInterface, is_sha_label


@pytest.fixture
def github():
    """Create a GitHubInterface with a fake token"""
    return GitHubInterface(token="fake-token", org="test-org", repo="test-repo")


class TestIsShaLabel:
    """Tests for the is_sha_label helper function"""

    def test_status_label(self) -> None:
        assert is_sha_label("🎪 abc123f 🚦 running") is True

    def test_ip_label(self) -> None:
        assert is_sha_label("🎪 abc123f 🌐 52.1.2.3:8080") is True

    def test_timestamp_label(self) -> None:
        assert is_sha_label("🎪 abc123f 📅 2024-01-15T14-30") is True

    def test_pointer_label(self) -> None:
        assert is_sha_label("🎪 🎯 abc123f") is True

    def test_static_trigger_label(self) -> None:
        assert is_sha_label("🎪 ⚡ showtime-trigger-start") is False

    def test_freeze_label(self) -> None:
        assert is_sha_label("🎪 🧊 showtime-freeze") is False

    def test_non_circus_label(self) -> None:
        assert is_sha_label("bug") is False

    def test_short_hex_not_matched(self) -> None:
        assert is_sha_label("🎪 ab12 🚦 running") is False

    def test_building_pointer_label(self) -> None:
        assert is_sha_label("🎪 🏗️ abc123f") is True

    def test_empty_string(self) -> None:
        assert is_sha_label("") is False


class TestPaginate:
    """Tests for the _paginate helper"""

    def test_unwrap_function(self, github: GitHubInterface) -> None:
        """unwrap extracts the list from nested JSON (e.g. search API)"""
        mock_response = Mock()
        mock_response.json.return_value = {"items": [{"id": 1}, {"id": 2}]}
        mock_response.raise_for_status = Mock()

        with patch("httpx.Client") as mock_client:
            mock_client.return_value.__enter__ = Mock(return_value=mock_client.return_value)
            mock_client.return_value.__exit__ = Mock(return_value=False)
            mock_client.return_value.get.return_value = mock_response

            result = github._paginate(
                "https://api.github.com/search/issues",
                unwrap=lambda d: d["items"],
            )

        assert len(result) == 2
        assert result[0] == {"id": 1}


class TestGetRepositoryLabelsPagination:
    """Tests for paginated label fetching"""

    def test_single_page(self, github: GitHubInterface) -> None:
        """When < 100 labels, single request suffices"""
        labels = [{"name": f"label-{i}"} for i in range(50)]

        mock_response = Mock()
        mock_response.json.return_value = labels
        mock_response.raise_for_status = Mock()

        with patch("httpx.Client") as mock_client:
            mock_client.return_value.__enter__ = Mock(return_value=mock_client.return_value)
            mock_client.return_value.__exit__ = Mock(return_value=False)
            mock_client.return_value.get.return_value = mock_response

            result = github.get_repository_labels()

        assert len(result) == 50
        assert result[0] == "label-0"
        # Only one request made (< 100 results means no second page)
        assert mock_client.return_value.get.call_count == 1

    def test_multiple_pages(self, github: GitHubInterface) -> None:
        """When > 100 labels, must paginate to fetch all"""
        page1 = [{"name": f"label-{i}"} for i in range(100)]
        page2 = [{"name": f"label-{i}"} for i in range(100, 150)]

        mock_resp1 = Mock()
        mock_resp1.json.return_value = page1
        mock_resp1.raise_for_status = Mock()

        mock_resp2 = Mock()
        mock_resp2.json.return_value = page2
        mock_resp2.raise_for_status = Mock()

        with patch("httpx.Client") as mock_client:
            mock_client.return_value.__enter__ = Mock(return_value=mock_client.return_value)
            mock_client.return_value.__exit__ = Mock(return_value=False)
            mock_client.return_value.get.side_effect = [mock_resp1, mock_resp2]

            result = github.get_repository_labels()

        assert len(result) == 150
        assert result[0] == "label-0"
        assert result[149] == "label-149"
        assert mock_client.return_value.get.call_count == 2

    def test_empty_repo(self, github: GitHubInterface) -> None:
        """Empty repo returns empty list"""
        mock_response = Mock()
        mock_response.json.return_value = []
        mock_response.raise_for_status = Mock()

        with patch("httpx.Client") as mock_client:
            mock_client.return_value.__enter__ = Mock(return_value=mock_client.return_value)
            mock_client.return_value.__exit__ = Mock(return_value=False)
            mock_client.return_value.get.return_value = mock_response

            result = github.get_repository_labels()

        assert result == []


class TestGetLabelsPagination:
    """Tests for paginated PR label fetching"""

    def test_single_page_pr_labels(self, github: GitHubInterface) -> None:
        """PR with few labels returns all in one request"""
        labels = [{"name": "bug"}, {"name": "🎪 abc123f 🚦 running"}]

        mock_response = Mock()
        mock_response.json.return_value = labels
        mock_response.raise_for_status = Mock()

        with patch("httpx.Client") as mock_client:
            mock_client.return_value.__enter__ = Mock(return_value=mock_client.return_value)
            mock_client.return_value.__exit__ = Mock(return_value=False)
            mock_client.return_value.get.return_value = mock_response

            result = github.get_labels(1234)

        assert len(result) == 2
        assert "🎪 abc123f 🚦 running" in result

    def test_multiple_pages_pr_labels(self, github: GitHubInterface) -> None:
        """PR with 100+ labels paginates correctly"""
        page1 = [{"name": f"label-{i}"} for i in range(100)]
        page2 = [{"name": "bug"}, {"name": "🎪 abc123f 🚦 running"}]

        mock_resp1 = Mock()
        mock_resp1.json.return_value = page1
        mock_resp1.raise_for_status = Mock()

        mock_resp2 = Mock()
        mock_resp2.json.return_value = page2
        mock_resp2.raise_for_status = Mock()

        with patch("httpx.Client") as mock_client:
            mock_client.return_value.__enter__ = Mock(return_value=mock_client.return_value)
            mock_client.return_value.__exit__ = Mock(return_value=False)
            mock_client.return_value.get.side_effect = [mock_resp1, mock_resp2]

            result = github.get_labels(1234)

        assert len(result) == 102
        assert result[0] == "label-0"
        assert "🎪 abc123f 🚦 running" in result
        assert mock_client.return_value.get.call_count == 2


class TestFindPrsWithShows:
    """Tests for PR search with pagination and closed PR support"""

    def test_includes_closed_prs(self, github: GitHubInterface) -> None:
        """include_closed=True should not filter by is:open"""
        mock_response = Mock()
        mock_response.json.return_value = {"items": [{"number": 1}, {"number": 2}]}
        mock_response.raise_for_status = Mock()

        with patch("httpx.Client") as mock_client:
            mock_client.return_value.__enter__ = Mock(return_value=mock_client.return_value)
            mock_client.return_value.__exit__ = Mock(return_value=False)
            mock_client.return_value.get.return_value = mock_response

            result = github.find_prs_with_shows(include_closed=True)

        assert result == [1, 2]
        # Verify query does NOT contain "is:open"
        call_kwargs = mock_client.return_value.get.call_args
        query = call_kwargs.kwargs["params"]["q"] if "params" in call_kwargs.kwargs else call_kwargs[1]["params"]["q"]
        assert "is:open" not in query

    def test_default_only_open_prs(self, github: GitHubInterface) -> None:
        """Default behavior should only search open PRs"""
        mock_response = Mock()
        mock_response.json.return_value = {"items": [{"number": 1}]}
        mock_response.raise_for_status = Mock()

        with patch("httpx.Client") as mock_client:
            mock_client.return_value.__enter__ = Mock(return_value=mock_client.return_value)
            mock_client.return_value.__exit__ = Mock(return_value=False)
            mock_client.return_value.get.return_value = mock_response

            result = github.find_prs_with_shows()

        assert result == [1]
        call_kwargs = mock_client.return_value.get.call_args
        query = call_kwargs.kwargs["params"]["q"] if "params" in call_kwargs.kwargs else call_kwargs[1]["params"]["q"]
        assert "is:open" in query

    def test_pagination(self, github: GitHubInterface) -> None:
        """Should paginate through multiple pages of results"""
        page1_items = [{"number": i} for i in range(100)]
        page2_items = [{"number": i} for i in range(100, 120)]

        mock_resp1 = Mock()
        mock_resp1.json.return_value = {"items": page1_items}
        mock_resp1.raise_for_status = Mock()

        mock_resp2 = Mock()
        mock_resp2.json.return_value = {"items": page2_items}
        mock_resp2.raise_for_status = Mock()

        with patch("httpx.Client") as mock_client:
            mock_client.return_value.__enter__ = Mock(return_value=mock_client.return_value)
            mock_client.return_value.__exit__ = Mock(return_value=False)
            mock_client.return_value.get.side_effect = [mock_resp1, mock_resp2]

            result = github.find_prs_with_shows()

        assert len(result) == 120


class TestFindOrphanedLabels:
    """Tests for orphan detection logic"""

    def test_detects_orphaned_labels(self, github: GitHubInterface) -> None:
        """Labels in repo but not on any PR are orphaned"""
        # Mock get_repository_labels to return SHA labels
        repo_labels = [
            "🎪 abc123f 🚦 running",  # orphaned - not on any PR
            "🎪 def456a 🚦 stopped",  # orphaned
            "🎪 xyz789b 🚦 running",  # used on PR #1
            "bug",  # not a circus label
            "🎪 ⚡ showtime-trigger-start",  # static label, no SHA
        ]

        pr1_labels = ["🎪 xyz789b 🚦 running", "🎪 🎯 xyz789b", "bug"]

        with patch.object(github, "get_repository_labels", return_value=repo_labels), \
             patch.object(github, "find_prs_with_shows", return_value=[1]), \
             patch.object(github, "get_labels", return_value=pr1_labels):

            orphaned = github.find_orphaned_labels(dry_run=True)

        assert len(orphaned) == 2
        assert "🎪 abc123f 🚦 running" in orphaned
        assert "🎪 def456a 🚦 stopped" in orphaned

    def test_no_orphans_when_all_used(self, github: GitHubInterface) -> None:
        """No orphans when all SHA labels are on PRs"""
        repo_labels = ["🎪 abc123f 🚦 running", "bug"]
        pr_labels = ["🎪 abc123f 🚦 running", "bug"]

        with patch.object(github, "get_repository_labels", return_value=repo_labels), \
             patch.object(github, "find_prs_with_shows", return_value=[1]), \
             patch.object(github, "get_labels", return_value=pr_labels):

            orphaned = github.find_orphaned_labels(dry_run=True)

        assert len(orphaned) == 0

    def test_deletes_orphans_when_not_dry_run(self, github: GitHubInterface) -> None:
        """Non-dry-run should delete orphaned labels"""
        repo_labels = ["🎪 abc123f 🚦 running", "🎪 def456a 🌐 1.2.3.4:8080"]

        with patch.object(github, "get_repository_labels", return_value=repo_labels), \
             patch.object(github, "find_prs_with_shows", return_value=[]), \
             patch.object(github, "get_labels", return_value=[]), \
             patch.object(github, "delete_repository_label", return_value=True) as mock_delete:

            deleted = github.find_orphaned_labels(dry_run=False)

        assert len(deleted) == 2
        assert mock_delete.call_count == 2

    def test_searches_closed_prs(self, github: GitHubInterface) -> None:
        """Orphan detection should include closed PRs"""
        with patch.object(github, "get_repository_labels", return_value=[]), \
             patch.object(github, "find_prs_with_shows", return_value=[]) as mock_find, \
             patch.object(github, "get_labels", return_value=[]):

            github.find_orphaned_labels(dry_run=True)

        mock_find.assert_called_once_with(include_closed=True)


class TestRemoveShowtimeLabelsDeletesDefinitions:
    """Tests that label teardown also deletes repo-level definitions"""

    @patch("showtime.core.pull_request.get_github")
    def test_deletes_sha_label_definitions(self, mock_get_github: Any) -> None:
        """remove_showtime_labels(delete_definitions=True) should delete repo-level defs for SHA labels"""
        from showtime.core.pull_request import PullRequest

        mock_github = Mock()
        mock_get_github.return_value = mock_github

        pr = PullRequest(
            1234,
            [
                "🎪 abc123f 🚦 running",
                "🎪 abc123f 🌐 52.1.2.3:8080",
                "🎪 abc123f 📅 2024-01-15T14-30",
                "🎪 🎯 abc123f",
                "🎪 ⚡ showtime-trigger-start",  # static - should NOT be deleted
            ],
        )

        pr.remove_showtime_labels(delete_definitions=True)

        # All labels removed from PR
        assert mock_github.remove_label.call_count == 5

        # Only SHA-containing labels should have repo definitions deleted
        delete_calls = [c.args[0] for c in mock_github.delete_repository_label.call_args_list]
        assert len(delete_calls) == 4  # all labels containing 7+ hex chars
        assert "🎪 abc123f 🚦 running" in delete_calls
        assert "🎪 abc123f 🌐 52.1.2.3:8080" in delete_calls
        assert "🎪 abc123f 📅 2024-01-15T14-30" in delete_calls
        assert "🎪 🎯 abc123f" in delete_calls  # pointer label contains SHA
        # Static labels should NOT be deleted from repo
        assert "🎪 ⚡ showtime-trigger-start" not in delete_calls

    @patch("showtime.core.pull_request.get_github")
    def test_remove_sha_labels_deletes_definitions(self, mock_get_github: Any) -> None:
        """remove_sha_labels(delete_definitions=True) should also delete repo-level definitions"""
        from showtime.core.pull_request import PullRequest

        mock_github = Mock()
        mock_get_github.return_value = mock_github

        pr = PullRequest(
            1234,
            [
                "🎪 abc123f 🚦 running",
                "🎪 abc123f 🌐 52.1.2.3:8080",
                "🎪 def456a 🚦 building",  # different SHA - should NOT be removed
            ],
        )

        pr.remove_sha_labels("abc123f", delete_definitions=True)

        # Only abc123f labels removed from PR
        remove_calls = [c.args[1] for c in mock_github.remove_label.call_args_list]
        assert "🎪 abc123f 🚦 running" in remove_calls
        assert "🎪 abc123f 🌐 52.1.2.3:8080" in remove_calls
        assert "🎪 def456a 🚦 building" not in remove_calls

        # Repo definitions deleted for removed labels
        delete_calls = [c.args[0] for c in mock_github.delete_repository_label.call_args_list]
        assert len(delete_calls) == 2

    @patch("showtime.core.pull_request.get_github")
    def test_default_skips_definition_deletion(self, mock_get_github: Any) -> None:
        """Default (delete_definitions=False) should skip repo-level deletion"""
        from showtime.core.pull_request import PullRequest

        mock_github = Mock()
        mock_get_github.return_value = mock_github

        pr = PullRequest(1234, ["🎪 abc123f 🚦 running"])

        pr.remove_showtime_labels()  # default is now False

        # Label removed from PR
        assert mock_github.remove_label.call_count == 1
        # But repo definition NOT deleted
        mock_github.delete_repository_label.assert_not_called()

    @patch("showtime.core.pull_request.get_github")
    def test_remove_sha_labels_default_skips_definitions(self, mock_get_github: Any) -> None:
        """Default remove_sha_labels() should NOT delete repo-level definitions"""
        from showtime.core.pull_request import PullRequest

        mock_github = Mock()
        mock_get_github.return_value = mock_github

        pr = PullRequest(1234, ["🎪 abc123f 🚦 running"])

        pr.remove_sha_labels("abc123f")  # default is now False

        assert mock_github.remove_label.call_count == 1
        mock_github.delete_repository_label.assert_not_called()


class TestPaginateBoundary:
    """Tests for the exact-100-item pagination boundary"""

    def _make_mock_client(self, mock_client: Any, responses: list) -> None:
        mock_client.return_value.__enter__ = Mock(return_value=mock_client.return_value)
        mock_client.return_value.__exit__ = Mock(return_value=False)
        mock_client.return_value.get.side_effect = responses

    def test_exactly_100_items_fetches_second_page(self, github: GitHubInterface) -> None:
        """Exactly 100 results must trigger a second page fetch (may have more)"""
        page1 = [{"name": f"label-{i}"} for i in range(100)]
        page2: list = []  # empty → done

        mock_resp1 = Mock()
        mock_resp1.json.return_value = page1
        mock_resp1.raise_for_status = Mock()

        mock_resp2 = Mock()
        mock_resp2.json.return_value = page2
        mock_resp2.raise_for_status = Mock()

        with patch("httpx.Client") as mock_client:
            self._make_mock_client(mock_client, [mock_resp1, mock_resp2])
            result = github.get_repository_labels()

        assert len(result) == 100
        assert mock_client.return_value.get.call_count == 2

    def test_exactly_100_items_then_100_more(self, github: GitHubInterface) -> None:
        """200 total items across two full pages fetches a third (empty) page"""
        page1 = [{"name": f"label-{i}"} for i in range(100)]
        page2 = [{"name": f"label-{i}"} for i in range(100, 200)]
        page3: list = []

        mock_resp1 = Mock()
        mock_resp1.json.return_value = page1
        mock_resp1.raise_for_status = Mock()

        mock_resp2 = Mock()
        mock_resp2.json.return_value = page2
        mock_resp2.raise_for_status = Mock()

        mock_resp3 = Mock()
        mock_resp3.json.return_value = page3
        mock_resp3.raise_for_status = Mock()

        with patch("httpx.Client") as mock_client:
            self._make_mock_client(mock_client, [mock_resp1, mock_resp2, mock_resp3])
            result = github.get_repository_labels()

        assert len(result) == 200
        assert mock_client.return_value.get.call_count == 3


class TestCleanupShaLabels:
    """Tests for cleanup_sha_labels standalone entrypoint"""

    def test_dry_run_returns_sha_labels_without_deleting(
        self, github: GitHubInterface
    ) -> None:
        repo_labels = [
            "🎪 abc123f 🚦 running",
            "🎪 def456a 🌐 1.2.3.4:8080",
            "bug",
            "🎪 ⚡ showtime-trigger-start",
        ]
        with patch.object(github, "get_repository_labels", return_value=repo_labels), \
             patch.object(github, "delete_repository_label") as mock_delete:

            result = github.cleanup_sha_labels(dry_run=True)

        assert "🎪 abc123f 🚦 running" in result
        assert "🎪 def456a 🌐 1.2.3.4:8080" in result
        assert "bug" not in result
        assert "🎪 ⚡ showtime-trigger-start" not in result
        mock_delete.assert_not_called()

    def test_non_dry_run_deletes_sha_labels(self, github: GitHubInterface) -> None:
        repo_labels = ["🎪 abc123f 🚦 running", "bug"]
        with patch.object(github, "get_repository_labels", return_value=repo_labels), \
             patch.object(
                 github, "delete_repository_label", return_value=True
             ) as mock_delete:

            result = github.cleanup_sha_labels(dry_run=False)

        assert result == ["🎪 abc123f 🚦 running"]
        mock_delete.assert_called_once_with("🎪 abc123f 🚦 running")

    def test_skips_already_deleted_labels(self, github: GitHubInterface) -> None:
        """delete_repository_label returning False (404) should not include in result"""
        repo_labels = ["🎪 abc123f 🚦 running", "🎪 def456a 🚦 stopped"]
        with patch.object(github, "get_repository_labels", return_value=repo_labels), \
             patch.object(
                 github, "delete_repository_label", side_effect=[True, False]
             ):

            result = github.cleanup_sha_labels(dry_run=False)

        assert result == ["🎪 abc123f 🚦 running"]  # def456a was already gone


class TestFindOrphanedLabelsErrorPropagation:
    """Tests that find_orphaned_labels propagates errors to the caller"""

    def test_propagates_api_errors(self, github: GitHubInterface) -> None:
        """API errors must propagate so the CLI caller can report the failure"""
        with patch.object(github, "get_repository_labels", return_value=["🎪 abc123f 🚦 running"]), \
             patch.object(github, "find_prs_with_shows", side_effect=RuntimeError("API down")):

            with pytest.raises(RuntimeError, match="API down"):
                github.find_orphaned_labels(dry_run=True)

    def test_multi_pr_orphan_detection(self, github: GitHubInterface) -> None:
        """Labels used on any PR across multiple PRs are not orphaned"""
        repo_labels = [
            "🎪 abc123f 🚦 running",  # on PR #1 → not orphaned
            "🎪 def456a 🚦 running",  # on PR #2 → not orphaned
            "🎪 fff9999 🚦 stopped",  # on no PR → orphaned
        ]
        pr1_labels = ["🎪 abc123f 🚦 running"]
        pr2_labels = ["🎪 def456a 🚦 running"]

        with patch.object(github, "get_repository_labels", return_value=repo_labels), \
             patch.object(github, "find_prs_with_shows", return_value=[1, 2]), \
             patch.object(github, "get_labels", side_effect=[pr1_labels, pr2_labels]):

            orphaned = github.find_orphaned_labels(dry_run=True)

        assert orphaned == ["🎪 fff9999 🚦 stopped"]
