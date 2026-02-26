"""
Tests for feature flag functionality

Tests cover:
- parse_feature_flag_label() parsing function
- create_feature_flag_label() creation function
- is_feature_flag_label() detection function
- extract_feature_flags_from_labels() extraction
- feature_flags_to_aws_env() conversion
- PullRequest.get_feature_flags() label extraction
- PullRequest.get_feature_flags_as_aws_env() AWS format
- Label color and description generation for feature flag labels
"""

from showtime.core.feature_flags import (
    create_feature_flag_label,
    extract_feature_flags_from_labels,
    feature_flags_to_aws_env,
    feature_flags_to_prefixed_dict,
    is_feature_flag_label,
    parse_feature_flag_label,
)
from showtime.core.label_colors import get_label_color, get_label_description
from showtime.core.pull_request import PullRequest


class TestParseFeatureFlagLabel:
    """Tests for the parse_feature_flag_label helper function"""

    def test_valid_true_flag(self) -> None:
        result = parse_feature_flag_label("🎪 🚩 EMBEDDED_SUPERSET=true")
        assert result == ("EMBEDDED_SUPERSET", "true")

    def test_valid_false_flag(self) -> None:
        result = parse_feature_flag_label("🎪 🚩 DASHBOARD_NATIVE_FILTERS=false")
        assert result == ("DASHBOARD_NATIVE_FILTERS", "false")

    def test_non_flag_label_returns_none(self) -> None:
        assert parse_feature_flag_label("🎪 ⌛ 48h") is None
        assert parse_feature_flag_label("bug") is None
        assert parse_feature_flag_label("🎪 abc123f 🚦 running") is None

    def test_invalid_value_returns_none(self) -> None:
        assert parse_feature_flag_label("🎪 🚩 FLAG=maybe") is None
        assert parse_feature_flag_label("🎪 🚩 FLAG=yes") is None
        assert parse_feature_flag_label("🎪 🚩 FLAG=1") is None

    def test_missing_equals_returns_none(self) -> None:
        assert parse_feature_flag_label("🎪 🚩 JUST_A_NAME") is None

    def test_case_normalization(self) -> None:
        """Flag names are uppercased, values are lowercased"""
        result = parse_feature_flag_label("🎪 🚩 embedded_superset=True")
        assert result == ("EMBEDDED_SUPERSET", "true")

        result = parse_feature_flag_label("🎪 🚩 Drill_To_Detail=FALSE")
        assert result == ("DRILL_TO_DETAIL", "false")

    def test_invalid_flag_name_returns_none(self) -> None:
        """Flag names must start with a letter and contain only A-Z, 0-9, _"""
        assert parse_feature_flag_label("🎪 🚩 123_BAD=true") is None
        assert parse_feature_flag_label("🎪 🚩 =true") is None
        assert parse_feature_flag_label("🎪 🚩 FLAG-NAME=true") is None

    def test_empty_value_returns_none(self) -> None:
        assert parse_feature_flag_label("🎪 🚩 FLAG=") is None


class TestIsFeatureFlagLabel:
    """Tests for the is_feature_flag_label function"""

    def test_valid_flag_label(self) -> None:
        assert is_feature_flag_label("🎪 🚩 EMBEDDED_SUPERSET=true") is True

    def test_non_flag_labels(self) -> None:
        assert is_feature_flag_label("🎪 ⌛ 48h") is False
        assert is_feature_flag_label("bug") is False
        assert is_feature_flag_label("🎪 abc123f 🚦 running") is False


class TestCreateFeatureFlagLabel:
    """Tests for the create_feature_flag_label function"""

    def test_create_true_flag(self) -> None:
        label = create_feature_flag_label("EMBEDDED_SUPERSET", "true")
        assert label == "🎪 🚩 EMBEDDED_SUPERSET=true"

    def test_create_false_flag(self) -> None:
        label = create_feature_flag_label("DASHBOARD_NATIVE_FILTERS", "false")
        assert label == "🎪 🚩 DASHBOARD_NATIVE_FILTERS=false"

    def test_default_value_is_true(self) -> None:
        label = create_feature_flag_label("EMBEDDED_SUPERSET")
        assert label == "🎪 🚩 EMBEDDED_SUPERSET=true"

    def test_case_normalization(self) -> None:
        label = create_feature_flag_label("embedded_superset", "True")
        assert label == "🎪 🚩 EMBEDDED_SUPERSET=true"

    def test_roundtrip(self) -> None:
        """Creating and parsing should roundtrip correctly"""
        label = create_feature_flag_label("DRILL_TO_DETAIL", "false")
        result = parse_feature_flag_label(label)
        assert result == ("DRILL_TO_DETAIL", "false")


class TestExtractFeatureFlags:
    """Tests for extract_feature_flags_from_labels"""

    def test_single_flag(self) -> None:
        labels = {"🎪 🚩 EMBEDDED_SUPERSET=true", "bug"}
        flags = extract_feature_flags_from_labels(labels)
        assert flags == {"EMBEDDED_SUPERSET": True}

    def test_multiple_flags(self) -> None:
        labels = {
            "🎪 🚩 EMBEDDED_SUPERSET=true",
            "🎪 🚩 DRILL_TO_DETAIL=false",
            "🎪 abc123f 🚦 running",
        }
        flags = extract_feature_flags_from_labels(labels)
        assert flags == {"EMBEDDED_SUPERSET": True, "DRILL_TO_DETAIL": False}

    def test_no_flags(self) -> None:
        labels = {"bug", "🎪 ⌛ 48h", "🎪 abc123f 🚦 running"}
        flags = extract_feature_flags_from_labels(labels)
        assert flags == {}

    def test_empty_labels(self) -> None:
        flags = extract_feature_flags_from_labels(set())
        assert flags == {}

    def test_ignores_invalid_flag_labels(self) -> None:
        labels = {
            "🎪 🚩 VALID_FLAG=true",
            "🎪 🚩 INVALID=maybe",  # bad value
            "🎪 🚩 JUST_NAME",  # no value
        }
        flags = extract_feature_flags_from_labels(labels)
        assert flags == {"VALID_FLAG": True}


class TestFeatureFlagsToAwsEnv:
    """Tests for feature_flags_to_aws_env conversion"""

    def test_single_true_flag(self) -> None:
        flags = {"EMBEDDED_SUPERSET": True}
        result = feature_flags_to_aws_env(flags)
        assert result == [{"name": "SUPERSET_FEATURE_EMBEDDED_SUPERSET", "value": "True"}]

    def test_single_false_flag(self) -> None:
        flags = {"DRILL_TO_DETAIL": False}
        result = feature_flags_to_aws_env(flags)
        assert result == [{"name": "SUPERSET_FEATURE_DRILL_TO_DETAIL", "value": "False"}]

    def test_multiple_flags_sorted(self) -> None:
        flags = {"DRILL_TO_DETAIL": False, "EMBEDDED_SUPERSET": True}
        result = feature_flags_to_aws_env(flags)
        assert result == [
            {"name": "SUPERSET_FEATURE_DRILL_TO_DETAIL", "value": "False"},
            {"name": "SUPERSET_FEATURE_EMBEDDED_SUPERSET", "value": "True"},
        ]

    def test_empty_flags(self) -> None:
        assert feature_flags_to_aws_env({}) == []


class TestPullRequestFeatureFlags:
    """Tests for PullRequest feature flag methods"""

    def test_get_feature_flags_single(self) -> None:
        pr = PullRequest(1234, ["🎪 🚩 EMBEDDED_SUPERSET=true", "bug"])
        flags = pr.get_feature_flags()
        assert flags == {"EMBEDDED_SUPERSET": True}

    def test_get_feature_flags_multiple(self) -> None:
        pr = PullRequest(
            1234,
            [
                "🎪 🚩 EMBEDDED_SUPERSET=true",
                "🎪 🚩 DRILL_TO_DETAIL=false",
                "🎪 abc123f 🚦 running",
            ],
        )
        flags = pr.get_feature_flags()
        assert flags == {"EMBEDDED_SUPERSET": True, "DRILL_TO_DETAIL": False}

    def test_get_feature_flags_none(self) -> None:
        pr = PullRequest(1234, ["bug", "🎪 ⌛ 48h"])
        assert pr.get_feature_flags() == {}

    def test_get_feature_flags_empty_labels(self) -> None:
        pr = PullRequest(1234, [])
        assert pr.get_feature_flags() == {}

    def test_get_feature_flags_as_aws_env(self) -> None:
        pr = PullRequest(1234, ["🎪 🚩 EMBEDDED_SUPERSET=true"])
        env = pr.get_feature_flags_as_aws_env()
        assert env == [{"name": "SUPERSET_FEATURE_EMBEDDED_SUPERSET", "value": "True"}]

    def test_get_feature_flags_as_aws_env_empty(self) -> None:
        pr = PullRequest(1234, ["bug"])
        env = pr.get_feature_flags_as_aws_env()
        assert env == []

    def test_feature_flags_persist_across_sha(self) -> None:
        """Feature flags are PR-level - they don't change when SHA changes"""
        labels_v1 = [
            "🎪 🚩 EMBEDDED_SUPERSET=true",
            "🎪 abc123f 🚦 running",
            "🎪 🎯 abc123f",
        ]
        pr1 = PullRequest(1234, labels_v1)
        flags_before = pr1.get_feature_flags()

        # Simulate SHA change - flag labels stay, SHA labels change
        labels_v2 = [
            "🎪 🚩 EMBEDDED_SUPERSET=true",
            "🎪 def456a 🚦 building",
        ]
        pr2 = PullRequest(1234, labels_v2)
        flags_after = pr2.get_feature_flags()

        assert flags_before == flags_after

    def test_feature_flags_with_mixed_labels(self) -> None:
        """Feature flags work alongside all other label types"""
        labels = [
            "🎪 ⚡ showtime-trigger-start",
            "🎪 ⌛ 1w",
            "🎪 🚩 EMBEDDED_SUPERSET=true",
            "🎪 🚩 DRILL_TO_DETAIL=false",
            "🎪 abc123f 🚦 running",
            "🎪 🎯 abc123f",
            "🎪 abc123f 📅 2024-01-15T14-30",
            "🎪 abc123f 🤡 maxime",
            "bug",
            "enhancement",
        ]
        pr = PullRequest(1234, labels)
        flags = pr.get_feature_flags()
        assert flags == {"EMBEDDED_SUPERSET": True, "DRILL_TO_DETAIL": False}

        # TTL should still work independently
        assert pr.get_pr_ttl_hours() == 168


class TestFeatureFlagLabelColors:
    """Tests for feature flag label color and description generation"""

    def test_predefined_flag_color(self) -> None:
        color = get_label_color("🎪 🚩 EMBEDDED_SUPERSET=true")
        assert color == "A855F7"  # Purple

    def test_dynamic_flag_color(self) -> None:
        """Non-predefined flags also get purple"""
        color = get_label_color("🎪 🚩 CUSTOM_FLAG=true")
        assert color == "A855F7"

    def test_predefined_flag_description(self) -> None:
        desc = get_label_description("🎪 🚩 EMBEDDED_SUPERSET=true")
        assert "Embedded Superset" in desc

    def test_dynamic_flag_enabled_description(self) -> None:
        desc = get_label_description("🎪 🚩 CUSTOM_FLAG=true")
        assert "CUSTOM_FLAG" in desc
        assert "enabled" in desc

    def test_dynamic_flag_disabled_description(self) -> None:
        desc = get_label_description("🎪 🚩 CUSTOM_FLAG=false")
        assert "CUSTOM_FLAG" in desc
        assert "disabled" in desc


class TestFeatureFlagsToPrefixedDict:
    """Tests for feature_flags_to_prefixed_dict conversion (used by hot-update)"""

    def test_single_flag(self) -> None:
        flags = {"EMBEDDED_SUPERSET": True}
        result = feature_flags_to_prefixed_dict(flags)
        assert result == {"SUPERSET_FEATURE_EMBEDDED_SUPERSET": True}

    def test_multiple_flags(self) -> None:
        flags = {"EMBEDDED_SUPERSET": True, "DRILL_TO_DETAIL": False}
        result = feature_flags_to_prefixed_dict(flags)
        assert result == {
            "SUPERSET_FEATURE_EMBEDDED_SUPERSET": True,
            "SUPERSET_FEATURE_DRILL_TO_DETAIL": False,
        }

    def test_empty_flags(self) -> None:
        assert feature_flags_to_prefixed_dict({}) == {}


class TestAnalyzeSyncNeededWithFeatureFlags:
    """Tests that analyze() returns sync_needed=True for feature flag hot-updates"""

    def test_analyze_sync_needed_with_flags_and_running_env(self) -> None:
        """sync_needed should be True when flags exist on a running environment"""
        from unittest.mock import Mock, patch

        from showtime.core.sync_state import ActionNeeded

        labels = [
            "🎪 🚩 EMBEDDED_SUPERSET=true",
            "🎪 abc123f 🚦 running",
            "🎪 🎯 abc123f",
            "🎪 abc123f 📅 2024-01-15T14-30",
        ]

        with patch("showtime.core.pull_request.get_github") as mock_get_github:
            mock_github = Mock()
            mock_get_github.return_value = mock_github
            mock_github.get_labels.return_value = labels

            pr = PullRequest(1234, labels)
            result = pr.analyze("abc123f", "open")

            assert result.action_needed == ActionNeeded.NO_ACTION
            assert result.build_needed is False
            assert result.sync_needed is True  # Feature flags need hot-update!

    def test_analyze_sync_needed_false_without_flags(self) -> None:
        """sync_needed should be False when no flags exist on a running environment"""
        from unittest.mock import Mock, patch

        from showtime.core.sync_state import ActionNeeded

        labels = [
            "🎪 abc123f 🚦 running",
            "🎪 🎯 abc123f",
        ]

        with patch("showtime.core.pull_request.get_github") as mock_get_github:
            mock_github = Mock()
            mock_get_github.return_value = mock_github
            mock_github.get_labels.return_value = labels

            pr = PullRequest(1234, labels)
            result = pr.analyze("abc123f", "open")

            assert result.action_needed == ActionNeeded.NO_ACTION
            assert result.build_needed is False
            assert result.sync_needed is False  # No flags, no hot-update needed


class TestSyncHotUpdateFeatureFlags:
    """Tests for hot-update of feature flags on running environments"""

    def test_sync_no_action_with_flags_triggers_hot_update(self) -> None:
        """When sync has no_action but flags exist, it should hot-update"""
        from unittest.mock import Mock, patch

        # PR with a running environment and a feature flag label
        labels = [
            "🎪 🚩 EMBEDDED_SUPERSET=true",
            "🎪 abc123f 🚦 running",
            "🎪 🎯 abc123f",
            "🎪 abc123f 📅 2024-01-15T14-30",
        ]
        pr = PullRequest(1234, labels)

        with patch.object(pr, "_determine_action", return_value="no_action"):
            with patch.object(pr, "refresh_labels"):
                # Mock the current_show's update_feature_flags
                assert pr.current_show is not None
                pr.current_show.update_feature_flags = Mock(return_value=True)  # type: ignore[method-assign]

                result = pr.sync(
                    "abc123f", dry_run_aws=True, dry_run_github=True, dry_run_docker=True
                )

                assert result.success is True
                assert result.action_taken == "update_feature_flags"

    def test_sync_no_action_without_flags_returns_no_action(self) -> None:
        """When sync has no_action and no flags, it should return no_action"""
        from unittest.mock import patch

        labels = [
            "🎪 abc123f 🚦 running",
            "🎪 🎯 abc123f",
        ]
        pr = PullRequest(1234, labels)

        with patch.object(pr, "_determine_action", return_value="no_action"):
            with patch.object(pr, "refresh_labels"):
                result = pr.sync(
                    "abc123f", dry_run_aws=True, dry_run_github=True, dry_run_docker=True
                )

                assert result.success is True
                assert result.action_taken == "no_action"
