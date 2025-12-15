"""
Tests for TTL (time-to-live) functionality

Tests cover:
- ttl_to_hours() parsing function
- PullRequest.get_pr_ttl_hours() label extraction
- Label description generation for TTL labels
- Default fallback behavior
"""

from showtime.core.date_utils import ttl_to_hours
from showtime.core.label_colors import get_label_description
from showtime.core.pull_request import PullRequest


class TestTtlToHours:
    """Tests for the ttl_to_hours helper function"""

    def test_hours_format(self) -> None:
        """Test hour-based TTL strings"""
        assert ttl_to_hours("24h") == 24
        assert ttl_to_hours("48h") == 48
        assert ttl_to_hours("72h") == 72
        assert ttl_to_hours("1h") == 1
        assert ttl_to_hours("168h") == 168

    def test_days_format(self) -> None:
        """Test day-based TTL strings"""
        assert ttl_to_hours("1d") == 24
        assert ttl_to_hours("2d") == 48
        assert ttl_to_hours("7d") == 168
        assert ttl_to_hours("14d") == 336

    def test_weeks_format(self) -> None:
        """Test week-based TTL strings"""
        assert ttl_to_hours("1w") == 168  # 7 * 24
        assert ttl_to_hours("2w") == 336  # 14 * 24

    def test_close_returns_none(self) -> None:
        """Test that 'close' TTL returns None (never expires by time)"""
        assert ttl_to_hours("close") is None
        assert ttl_to_hours("CLOSE") is None
        assert ttl_to_hours("Close") is None

    def test_empty_and_none(self) -> None:
        """Test empty/None inputs"""
        assert ttl_to_hours("") is None
        assert ttl_to_hours(None) is None  # type: ignore

    def test_invalid_formats(self) -> None:
        """Test invalid TTL formats return None"""
        assert ttl_to_hours("invalid") is None
        assert ttl_to_hours("24") is None  # Missing unit
        assert ttl_to_hours("h24") is None  # Wrong order
        assert ttl_to_hours("24x") is None  # Invalid unit
        assert ttl_to_hours("-24h") is None  # Negative

    def test_whitespace_handling(self) -> None:
        """Test that whitespace is trimmed"""
        assert ttl_to_hours(" 24h ") == 24
        assert ttl_to_hours("  1w  ") == 168

    def test_case_insensitive(self) -> None:
        """Test that units are case-insensitive"""
        assert ttl_to_hours("24H") == 24
        assert ttl_to_hours("1D") == 24
        assert ttl_to_hours("1W") == 168


class TestPullRequestTtl:
    """Tests for PullRequest TTL methods"""

    def test_get_pr_ttl_hours_with_1w_label(self) -> None:
        """Test PR-level TTL label parsing for 1 week"""
        pr = PullRequest(1234, ["🎪 ⌛ 1w", "bug", "enhancement"])
        assert pr.get_pr_ttl_hours() == 168

    def test_get_pr_ttl_hours_with_72h_label(self) -> None:
        """Test PR-level TTL label parsing for 72 hours"""
        pr = PullRequest(1234, ["🎪 ⌛ 72h"])
        assert pr.get_pr_ttl_hours() == 72

    def test_get_pr_ttl_hours_with_48h_label(self) -> None:
        """Test PR-level TTL label parsing for 48 hours (default value)"""
        pr = PullRequest(1234, ["🎪 ⌛ 48h"])
        assert pr.get_pr_ttl_hours() == 48

    def test_get_pr_ttl_hours_with_24h_label(self) -> None:
        """Test PR-level TTL label parsing for 24 hours"""
        pr = PullRequest(1234, ["🎪 ⌛ 24h"])
        assert pr.get_pr_ttl_hours() == 24

    def test_get_pr_ttl_hours_with_close_label(self) -> None:
        """Test PR-level TTL label with 'close' returns None (never expire by time)"""
        pr = PullRequest(1234, ["🎪 ⌛ close"])
        assert pr.get_pr_ttl_hours() is None

    def test_get_pr_ttl_hours_no_label_returns_none(self) -> None:
        """Test PR without TTL label returns None (caller should use default)"""
        pr = PullRequest(1234, ["bug", "enhancement"])
        assert pr.get_pr_ttl_hours() is None

    def test_get_pr_ttl_hours_empty_labels(self) -> None:
        """Test PR with no labels returns None"""
        pr = PullRequest(1234, [])
        assert pr.get_pr_ttl_hours() is None

    def test_get_pr_ttl_hours_ignores_sha_ttl_labels(self) -> None:
        """Test that per-SHA TTL labels (legacy format) are ignored"""
        # Per-SHA TTL label format: "🎪 abc123f ⌛ 24h" - has SHA before ⌛
        pr = PullRequest(1234, ["🎪 abc123f ⌛ 24h"])
        # Should NOT match because "🎪 abc123f ⌛ 24h" doesn't start with "🎪 ⌛ "
        assert pr.get_pr_ttl_hours() is None

    def test_get_pr_ttl_hours_with_mixed_labels(self) -> None:
        """Test PR with TTL and other circus labels"""
        labels = [
            "🎪 ⌛ 1w",  # PR-level TTL - should be found
            "🎪 abc123f 🚦 running",
            "🎪 🎯 abc123f",
            "🎪 abc123f 📅 2024-01-15T14-30",
            "🎪 abc123f ⌛ 48h",  # Per-SHA TTL - should be ignored
            "bug",
        ]
        pr = PullRequest(1234, labels)
        assert pr.get_pr_ttl_hours() == 168  # 1w = 168h

    def test_get_pr_ttl_hours_first_match_wins(self) -> None:
        """Test that first matching TTL label is used (edge case)"""
        # If somehow multiple PR-level TTL labels exist, first one wins
        pr = PullRequest(1234, ["🎪 ⌛ 1w", "🎪 ⌛ 24h"])
        # Implementation iterates over set, so order is not guaranteed
        # Just verify we get a valid result (either 168 or 24)
        result = pr.get_pr_ttl_hours()
        assert result in [168, 24]


class TestTtlLabelDescriptions:
    """Tests for TTL label description generation"""

    def test_pr_level_ttl_label_description_from_definitions(self) -> None:
        """Test description for predefined PR-level TTL labels (exact matches)"""
        # These are defined in LABEL_DEFINITIONS with specific descriptions
        assert get_label_description("🎪 ⌛ 24h") == "Environment expires after 24 hours"
        assert (
            get_label_description("🎪 ⌛ 48h")
            == "Environment expires after 48 hours (default)"
        )
        assert get_label_description("🎪 ⌛ 72h") == "Environment expires after 72 hours"
        assert get_label_description("🎪 ⌛ 1w") == "Environment expires after 1 week"
        assert (
            get_label_description("🎪 ⌛ close")
            == "Environment expires only when PR is closed"
        )

    def test_pr_level_ttl_label_description_dynamic(self) -> None:
        """Test description for non-predefined PR-level TTL labels (dynamic parsing)"""
        # These are NOT in LABEL_DEFINITIONS, so they fall through to dynamic parsing
        desc = get_label_description("🎪 ⌛ 2w")
        assert "2w" in desc

    def test_per_sha_ttl_label_description(self) -> None:
        """Test description for per-SHA TTL labels (legacy format)"""
        desc = get_label_description("🎪 abc123f ⌛ 24h")
        assert desc == "Environment abc123f expires after 24h"

    def test_pr_level_ttl_description_no_sha(self) -> None:
        """Test PR-level TTL label description doesn't include SHA"""
        desc = get_label_description("🎪 ⌛ 1w")
        # Should NOT include a random SHA-like string
        assert "abc123" not in desc.lower()
        # Should include the TTL value or human-readable equivalent
        assert "1 week" in desc or "1w" in desc


class TestDefaultFallbackBehavior:
    """Tests to verify default TTL fallback behavior

    When --respect-ttl is used and no TTL label is present,
    the cleanup should fall back to the default (--older-than value).
    """

    def test_no_ttl_label_means_use_default(self) -> None:
        """Verify that missing TTL label returns None (signals: use default)"""
        pr = PullRequest(1234, ["bug", "🎪 abc123f 🚦 running"])
        # None means "no override specified, use default"
        assert pr.get_pr_ttl_hours() is None

    def test_close_ttl_means_never_expire_by_time(self) -> None:
        """Verify that 'close' TTL returns None but has different semantics"""
        pr_with_close = PullRequest(1234, ["🎪 ⌛ close"])
        pr_without_ttl = PullRequest(1234, ["bug"])

        # Both return None, but the cleanup logic distinguishes them:
        # - pr_with_close has a TTL label starting with "🎪 ⌛ " -> skip cleanup
        # - pr_without_ttl has no such label -> use default
        assert pr_with_close.get_pr_ttl_hours() is None
        assert pr_without_ttl.get_pr_ttl_hours() is None

        # The distinction is made by checking if any label starts with "🎪 ⌛ "
        has_ttl_label_with_close = any(
            label.startswith("🎪 ⌛ ") for label in pr_with_close.labels
        )
        has_ttl_label_without = any(
            label.startswith("🎪 ⌛ ") for label in pr_without_ttl.labels
        )

        assert has_ttl_label_with_close is True  # Has explicit "close" TTL
        assert has_ttl_label_without is False  # No TTL label at all

    def test_explicit_48h_same_as_default(self) -> None:
        """Test that explicit 48h TTL label works (even though it's the default)"""
        pr = PullRequest(1234, ["🎪 ⌛ 48h"])
        assert pr.get_pr_ttl_hours() == 48
