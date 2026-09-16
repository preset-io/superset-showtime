"""
Tests for Showtime PR comment cleanup: superseded comments are deleted
(by their own author, never by content match alone) when new lifecycle
comments are posted.
"""

from typing import Any
from unittest.mock import Mock, patch

import pytest

from showtime.core.constants import LEGACY_COMMENT_PREFIX, SHOWTIME_COMMENT_MARKER
from showtime.core.github import GitHubInterface, is_showtime_comment
from showtime.core.github_messages import (
    building_comment,
    cleanup_comment,
    rolling_start_comment,
    rolling_success_comment,
    success_comment,
)
from showtime.core.pull_request import PullRequest
from showtime.core.show import Show


@pytest.fixture
def github():
    """Create a GitHubInterface with a fake token"""
    return GitHubInterface(token="fake-token", org="test-org", repo="test-repo")


def _quote_reply(body: str, extra: str = "") -> str:
    """Simulate GitHub's "Quote reply", which copies the raw markdown of the
    quoted comment (marker included) into the new body, prefixed with '> '."""
    quoted = "\n".join(f"> {line}" for line in body.split("\n"))
    return f"{quoted}\n\n{extra}" if extra else quoted


class TestIsShowtimeComment:
    """Tests for the is_showtime_comment helper function"""

    def test_marker_comment(self) -> None:
        body = f"🎪 Showtime deployed environment\n\n{SHOWTIME_COMMENT_MARKER}"
        assert is_showtime_comment(body) is True

    def test_marker_alone(self) -> None:
        assert is_showtime_comment(SHOWTIME_COMMENT_MARKER) is True

    def test_legacy_building_comment(self) -> None:
        body = (
            f"{LEGACY_COMMENT_PREFIX} is building environment on "
            "[GHA](https://github.com/apache/superset/actions/runs/123) for "
            "[abc123f](https://github.com/apache/superset/commit/abc123f)"
        )
        assert is_showtime_comment(body) is True

    def test_legacy_success_comment(self) -> None:
        body = (
            f"{LEGACY_COMMENT_PREFIX} deployed environment on [GHA](url) for [abc123f](url)\n\n"
            "• **Environment:** http://1.2.3.4:8080 (admin/admin)\n"
            "• **Lifetime:** 48h auto-cleanup"
        )
        assert is_showtime_comment(body) is True

    def test_user_comment(self) -> None:
        assert is_showtime_comment("LGTM, thanks for the fix!") is False

    def test_user_comment_mentioning_showtime(self) -> None:
        """A human discussing superset-showtime mid-comment is not a bot comment"""
        body = "I think superset-showtime should handle this differently"
        assert is_showtime_comment(body) is False

    def test_empty_body(self) -> None:
        assert is_showtime_comment("") is False

    def test_quote_reply_containing_marker_is_not_showtime(self) -> None:
        """GitHub 'Quote reply' copies the marker into a human's comment; the
        marker must be anchored to the end of the body, not a bare substring"""
        original = f"🎪 Showtime deployed\n\n{SHOWTIME_COMMENT_MARKER}"
        quoted = _quote_reply(original, extra="This URL 404s for me.")
        assert is_showtime_comment(quoted) is False

    def test_quote_reply_with_nothing_appended_still_matches(self) -> None:
        """A bare quote-reply with no added text is indistinguishable from
        the original by content alone - the author-identity gate in
        delete_showtime_comments is the real guard here, not this predicate"""
        original = f"🎪 Showtime deployed\n\n{SHOWTIME_COMMENT_MARKER}"
        quoted = _quote_reply(original)
        assert is_showtime_comment(quoted) is True

    def test_human_comment_starting_with_tent(self) -> None:
        """A human bug report opening with the tent emoji and linking the repo
        must not match the legacy heuristic"""
        assert is_showtime_comment("🎪 superset-showtime keeps eating my comments") is False

    def test_human_comment_linking_repo_issue(self) -> None:
        body = "🎪 opened https://github.com/mistercrunch/superset-showtime/issues/42"
        assert is_showtime_comment(body) is False

    @pytest.mark.parametrize(
        "produced",
        [
            building_comment(Show(pr_number=1, sha="abc123f", status="building")),
            success_comment(
                Show(pr_number=1, sha="abc123f", status="running", ip="1.2.3.4"), ttl="48h"
            ),
            cleanup_comment(Show(pr_number=1, sha="abc123f", status="stopped")),
            rolling_start_comment(
                Show(pr_number=1, sha="abc123f", status="running", ip="1.2.3.4"),
                "d" * 40,
            ),
            rolling_success_comment(
                Show(pr_number=1, sha="abc123f", status="running", ip="1.2.3.4"),
                Show(pr_number=1, sha="d" * 7, status="running", ip="5.6.7.8"),
                ttl="48h",
            ),
        ],
    )
    def test_real_producer_output_matches(self, produced: str) -> None:
        """The actual comment producers (not hand-typed approximations) must
        be recognized once the marker is appended, and a quote-reply of that
        real output must not be"""
        posted = f"{produced}\n\n{SHOWTIME_COMMENT_MARKER}"
        assert is_showtime_comment(posted) is True
        assert is_showtime_comment(_quote_reply(posted, extra="reporting an issue")) is False


class TestDeleteComment:
    """Tests for GitHubInterface.delete_comment at the httpx.Client level"""

    def _mock_response(self, status_code: int) -> Any:
        response = Mock()
        response.status_code = status_code
        response.raise_for_status = Mock()
        return response

    def test_204_returns_true(self, github: GitHubInterface) -> None:
        with patch("httpx.Client") as mock_client:
            mock_client.return_value.__enter__ = Mock(return_value=mock_client.return_value)
            mock_client.return_value.__exit__ = Mock(return_value=False)
            mock_client.return_value.delete.return_value = self._mock_response(204)

            assert github.delete_comment(1) is True

    def test_404_returns_false(self, github: GitHubInterface) -> None:
        with patch("httpx.Client") as mock_client:
            mock_client.return_value.__enter__ = Mock(return_value=mock_client.return_value)
            mock_client.return_value.__exit__ = Mock(return_value=False)
            mock_client.return_value.delete.return_value = self._mock_response(404)

            assert github.delete_comment(1) is False

    def test_403_raises(self, github: GitHubInterface) -> None:
        import httpx

        response = self._mock_response(403)
        response.raise_for_status = Mock(
            side_effect=httpx.HTTPStatusError("forbidden", request=Mock(), response=response)
        )

        with patch("httpx.Client") as mock_client:
            mock_client.return_value.__enter__ = Mock(return_value=mock_client.return_value)
            mock_client.return_value.__exit__ = Mock(return_value=False)
            mock_client.return_value.delete.return_value = response

            with pytest.raises(httpx.HTTPStatusError):
                github.delete_comment(1)


class TestGetAuthenticatedLogin:
    """Tests for GitHubInterface.get_authenticated_login"""

    def _mock_response(self, status_code: int, json_data: Any = None) -> Any:
        response = Mock()
        response.status_code = status_code
        response.json.return_value = json_data
        response.raise_for_status = Mock()
        return response

    def test_returns_login_from_api(self, github: GitHubInterface) -> None:
        with patch("httpx.Client") as mock_client:
            mock_client.return_value.__enter__ = Mock(return_value=mock_client.return_value)
            mock_client.return_value.__exit__ = Mock(return_value=False)
            mock_client.return_value.get.return_value = self._mock_response(
                200, {"login": "some-user"}
            )

            assert github.get_authenticated_login() == "some-user"

    def test_403_falls_back_to_actions_bot(self, github: GitHubInterface) -> None:
        """The default secrets.GITHUB_TOKEN 403s on /user - expected, not an error"""
        with patch("httpx.Client") as mock_client:
            mock_client.return_value.__enter__ = Mock(return_value=mock_client.return_value)
            mock_client.return_value.__exit__ = Mock(return_value=False)
            mock_client.return_value.get.return_value = self._mock_response(403)

            assert github.get_authenticated_login() == "github-actions[bot]"

    def test_result_is_cached(self, github: GitHubInterface) -> None:
        with patch("httpx.Client") as mock_client:
            mock_client.return_value.__enter__ = Mock(return_value=mock_client.return_value)
            mock_client.return_value.__exit__ = Mock(return_value=False)
            mock_client.return_value.get.return_value = self._mock_response(
                200, {"login": "some-user"}
            )

            github.get_authenticated_login()
            github.get_authenticated_login()

        assert mock_client.return_value.get.call_count == 1


class TestGetCommentsPagination:
    """Tests for GitHubInterface.get_comments pagination (apache/superset PRs
    routinely exceed 100 comments)"""

    def test_paginates_across_multiple_pages(self, github: GitHubInterface) -> None:
        page1 = [{"id": i, "body": "hi"} for i in range(100)]
        page2 = [{"id": i, "body": "hi"} for i in range(100, 150)]

        resp1 = Mock()
        resp1.json.return_value = page1
        resp1.raise_for_status = Mock()

        resp2 = Mock()
        resp2.json.return_value = page2
        resp2.raise_for_status = Mock()

        with patch("httpx.Client") as mock_client:
            mock_client.return_value.__enter__ = Mock(return_value=mock_client.return_value)
            mock_client.return_value.__exit__ = Mock(return_value=False)
            mock_client.return_value.get.side_effect = [resp1, resp2]

            result = github.get_comments(1234)

        assert len(result) == 150
        assert mock_client.return_value.get.call_count == 2


class TestDeleteShowtimeComments:
    """Tests for GitHubInterface.delete_showtime_comments"""

    def _bot_comment(self, comment_id: int, body: str, login: str = "showtime-bot") -> dict:
        return {"id": comment_id, "body": body, "user": {"login": login}}

    def test_deletes_only_own_showtime_comments(self, github: GitHubInterface) -> None:
        comments = [
            self._bot_comment(1, f"🎪 Showtime is building\n\n{SHOWTIME_COMMENT_MARKER}"),
            self._bot_comment(2, "Great work on this PR!", login="a-human"),
            self._bot_comment(3, f"{LEGACY_COMMENT_PREFIX} deployed environment"),
            {"id": 4, "body": None, "user": {"login": "showtime-bot"}},
        ]

        with patch.object(github, "get_authenticated_login", return_value="showtime-bot"):
            with patch.object(github, "get_comments", return_value=comments):
                with patch.object(github, "delete_comment", return_value=True) as mock_delete:
                    deleted = github.delete_showtime_comments(1234)

        assert deleted == 2
        assert [call.args[0] for call in mock_delete.call_args_list] == [1, 3]

    def test_no_showtime_comments(self, github: GitHubInterface) -> None:
        comments = [self._bot_comment(1, "Just a regular comment")]

        with patch.object(github, "get_authenticated_login", return_value="showtime-bot"):
            with patch.object(github, "get_comments", return_value=comments):
                with patch.object(github, "delete_comment") as mock_delete:
                    deleted = github.delete_showtime_comments(1234)

        assert deleted == 0
        mock_delete.assert_not_called()

    def test_skips_matching_body_from_other_author(self, github: GitHubInterface) -> None:
        """The load-bearing safety test: a comment whose body matches the
        Showtime shape (a forged or quoted body) is NOT deleted unless it
        was actually authored by this token's own identity"""
        comments = [
            self._bot_comment(
                1, f"🎪 forged Showtime comment\n\n{SHOWTIME_COMMENT_MARKER}", login="an-attacker"
            ),
        ]

        with patch.object(github, "get_authenticated_login", return_value="showtime-bot"):
            with patch.object(github, "get_comments", return_value=comments):
                with patch.object(github, "delete_comment") as mock_delete:
                    deleted = github.delete_showtime_comments(1234)

        assert deleted == 0
        mock_delete.assert_not_called()

    def test_except_id_is_preserved(self, github: GitHubInterface) -> None:
        comments = [
            self._bot_comment(1, f"🎪 old\n\n{SHOWTIME_COMMENT_MARKER}"),
            self._bot_comment(2, f"🎪 just posted\n\n{SHOWTIME_COMMENT_MARKER}"),
        ]

        with patch.object(github, "get_authenticated_login", return_value="showtime-bot"):
            with patch.object(github, "get_comments", return_value=comments):
                with patch.object(github, "delete_comment", return_value=True) as mock_delete:
                    deleted = github.delete_showtime_comments(1234, except_id=2)

        assert deleted == 1
        assert [call.args[0] for call in mock_delete.call_args_list] == [1]

    def test_continues_after_one_delete_raises(self, github: GitHubInterface) -> None:
        """One comment's delete failing (e.g. secondary rate limit) must not
        abort the rest of the sweep"""
        comments = [
            self._bot_comment(1, f"🎪 a\n\n{SHOWTIME_COMMENT_MARKER}"),
            self._bot_comment(2, f"🎪 b\n\n{SHOWTIME_COMMENT_MARKER}"),
        ]

        with patch.object(github, "get_authenticated_login", return_value="showtime-bot"):
            with patch.object(github, "get_comments", return_value=comments):
                with patch.object(
                    github, "delete_comment", side_effect=[Exception("rate limited"), True]
                ):
                    deleted = github.delete_showtime_comments(1234)

        assert deleted == 1

    def test_already_gone_comment_not_counted(self, github: GitHubInterface) -> None:
        """delete_comment returning False (404, already gone) must not inflate
        the reported deleted count"""
        comments = [self._bot_comment(1, f"🎪 a\n\n{SHOWTIME_COMMENT_MARKER}")]

        with patch.object(github, "get_authenticated_login", return_value="showtime-bot"):
            with patch.object(github, "get_comments", return_value=comments):
                with patch.object(github, "delete_comment", return_value=False):
                    deleted = github.delete_showtime_comments(1234)

        assert deleted == 0


class TestPostShowtimeComment:
    """Tests for PullRequest._post_showtime_comment"""

    @patch("showtime.core.pull_request.get_github")
    def test_posts_before_deleting_old_comments(self, mock_get_github: Mock) -> None:
        call_order = []
        mock_github = Mock()
        mock_github.post_comment.side_effect = lambda *a, **k: (
            call_order.append("post"),
            {"id": 999},
        )[1]
        mock_github.delete_showtime_comments.side_effect = lambda *a, **k: (
            call_order.append("delete"),
            2,
        )[1]
        mock_get_github.return_value = mock_github

        pr = PullRequest(1234, [])
        pr._post_showtime_comment("🎪 Showtime deployed environment")

        assert call_order == ["post", "delete"]
        mock_github.delete_showtime_comments.assert_called_once_with(1234, except_id=999)
        posted_body = mock_github.post_comment.call_args.args[1]
        assert posted_body.startswith("🎪 Showtime deployed environment")
        assert SHOWTIME_COMMENT_MARKER in posted_body

    @patch("showtime.core.pull_request.get_github")
    def test_cleanup_failure_still_posts(self, mock_get_github: Mock) -> None:
        """A failed cleanup should never block, or be blocked by, the new comment"""
        mock_github = Mock()
        mock_github.post_comment.return_value = {"id": 999}
        mock_github.delete_showtime_comments.side_effect = Exception("API error")
        mock_get_github.return_value = mock_github

        pr = PullRequest(1234, [])
        pr._post_showtime_comment("🎪 Showtime deployed environment")

        mock_github.post_comment.assert_called_once()
        posted_body = mock_github.post_comment.call_args.args[1]
        assert SHOWTIME_COMMENT_MARKER in posted_body

    @patch("showtime.core.pull_request.get_github")
    def test_dry_run_does_nothing(self, mock_get_github: Mock) -> None:
        mock_github = Mock()
        mock_get_github.return_value = mock_github

        pr = PullRequest(1234, [])
        pr._post_showtime_comment("🎪 Showtime deployed environment", dry_run=True)

        mock_github.delete_showtime_comments.assert_not_called()
        mock_github.post_comment.assert_not_called()
