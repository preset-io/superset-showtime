"""
Tests for feature flag extraction and threading through the deploy flow.
"""

from unittest.mock import Mock, patch

from showtime.core.pull_request import PullRequest, parse_feature_flags
from showtime.core.show import Show


class TestParseFeatureFlags:
    """Tests for parse_feature_flags()"""

    def test_empty_description(self) -> None:
        assert parse_feature_flags("") == []

    def test_none_description(self) -> None:
        assert parse_feature_flags(None) == []

    def test_no_flags(self) -> None:
        assert parse_feature_flags("This is a regular PR description\nNo flags here") == []

    def test_single_flag(self) -> None:
        result = parse_feature_flags("FEATURE_DASHBOARD_NATIVE_FILTERS=true")
        assert result == [
            {"name": "SUPERSET_FEATURE_DASHBOARD_NATIVE_FILTERS", "value": "True"}
        ]

    def test_multiple_flags(self) -> None:
        description = """
## Description
This PR needs some feature flags:

FEATURE_DASHBOARD_NATIVE_FILTERS=true
FEATURE_ENABLE_TEMPLATE_PROCESSING=false
FEATURE_ALERT_REPORTS=true
"""
        result = parse_feature_flags(description)
        assert len(result) == 3
        assert result[0] == {
            "name": "SUPERSET_FEATURE_DASHBOARD_NATIVE_FILTERS",
            "value": "True",
        }
        assert result[1] == {
            "name": "SUPERSET_FEATURE_ENABLE_TEMPLATE_PROCESSING",
            "value": "False",
        }
        assert result[2] == {
            "name": "SUPERSET_FEATURE_ALERT_REPORTS",
            "value": "True",
        }

    def test_flags_mixed_with_other_content(self) -> None:
        description = """Some text before
FEATURE_FOO=true
More text
Not a flag: FEATURE_BAR
FEATURE_BAZ=1
End of description"""
        result = parse_feature_flags(description)
        assert len(result) == 2
        assert result[0] == {"name": "SUPERSET_FEATURE_FOO", "value": "True"}
        assert result[1] == {"name": "SUPERSET_FEATURE_BAZ", "value": "True"}

    def test_values_normalized(self) -> None:
        """Values are normalized to canonical 'True'/'False' format"""
        result = parse_feature_flags("FEATURE_X=True")
        assert result[0]["value"] == "True"

        result = parse_feature_flags("FEATURE_X=FALSE")
        assert result[0]["value"] == "False"

    def test_inline_flag(self) -> None:
        """Flags can appear inline, not just on their own line"""
        result = parse_feature_flags("Please enable FEATURE_X=true for testing")
        assert len(result) == 1
        assert result[0] == {"name": "SUPERSET_FEATURE_X", "value": "True"}

    def test_code_block_flags(self) -> None:
        """Flags inside code blocks are still matched (regex doesn't know about markdown)"""
        description = "```\nFEATURE_TEST=true\n```"
        result = parse_feature_flags(description)
        assert len(result) == 1

    def test_invalid_value_skipped(self) -> None:
        """Invalid values like typos are skipped with a warning"""
        result = parse_feature_flags("FEATURE_X=tru\nFEATURE_Y=true")
        assert len(result) == 1
        assert result[0] == {"name": "SUPERSET_FEATURE_Y", "value": "True"}

    def test_prefixed_feature_not_matched(self) -> None:
        """Words like SOME_FEATURE_X=true should not match due to word boundary"""
        result = parse_feature_flags("SOME_FEATURE_X=true")
        assert result == []

    def test_all_valid_values_accepted(self) -> None:
        """All boolean-like values are accepted"""
        description = (
            "FEATURE_A=true\nFEATURE_B=false\n"
            "FEATURE_C=1\nFEATURE_D=0\n"
            "FEATURE_E=yes\nFEATURE_F=no"
        )
        result = parse_feature_flags(description)
        assert len(result) == 6


class TestFeatureFlagsInSync:
    """Tests for feature flag threading through sync()"""

    @patch("showtime.core.pull_request.get_github")
    def test_sync_passes_feature_flags_to_deploy(self, mock_get_github: Mock) -> None:
        """Feature flags from PR description are passed to deploy_aws()"""
        mock_github = Mock()
        mock_get_github.return_value = mock_github

        mock_github.get_labels.return_value = ["🎪 ⚡ showtime-trigger-start"]
        mock_github.get_pr_data.return_value = {
            "body": "Testing with FEATURE_DASHBOARD_NATIVE_FILTERS=true"
        }

        pr = PullRequest(1234, ["🎪 ⚡ showtime-trigger-start"])

        with patch.object(pr, "_atomic_claim", return_value=True):
            with patch.object(pr, "_create_new_show") as mock_create:
                with patch.object(pr, "_post_building_comment"):
                    with patch.object(pr, "_update_show_labels"):
                        with patch.object(pr, "_post_success_comment"):
                            mock_show = Show(
                                pr_number=1234, sha="abc123f", status="building"
                            )
                            mock_create.return_value = mock_show
                            mock_show.build_docker = Mock()  # type: ignore[method-assign]
                            mock_show.deploy_aws = Mock()  # type: ignore[method-assign]

                            result = pr.sync(
                                "abc123f",
                                dry_run_github=True,
                                dry_run_aws=True,
                                dry_run_docker=True,
                            )

                            assert result.success is True
                            expected_flags = [
                                {
                                    "name": "SUPERSET_FEATURE_DASHBOARD_NATIVE_FILTERS",
                                    "value": "True",
                                }
                            ]
                            mock_show.deploy_aws.assert_called_once_with(
                                True, feature_flags=expected_flags
                            )

    @patch("showtime.core.pull_request.get_github")
    def test_sync_empty_flags_when_no_flags_in_description(
        self, mock_get_github: Mock
    ) -> None:
        """No feature flags in description results in empty list passed to deploy"""
        mock_github = Mock()
        mock_get_github.return_value = mock_github

        mock_github.get_labels.return_value = ["🎪 ⚡ showtime-trigger-start"]
        mock_github.get_pr_data.return_value = {
            "body": "Regular PR, no feature flags"
        }

        pr = PullRequest(1234, ["🎪 ⚡ showtime-trigger-start"])

        with patch.object(pr, "_atomic_claim", return_value=True):
            with patch.object(pr, "_create_new_show") as mock_create:
                with patch.object(pr, "_post_building_comment"):
                    with patch.object(pr, "_update_show_labels"):
                        with patch.object(pr, "_post_success_comment"):
                            mock_show = Show(
                                pr_number=1234, sha="abc123f", status="building"
                            )
                            mock_create.return_value = mock_show
                            mock_show.build_docker = Mock()  # type: ignore[method-assign]
                            mock_show.deploy_aws = Mock()  # type: ignore[method-assign]

                            pr.sync(
                                "abc123f",
                                dry_run_github=True,
                                dry_run_aws=True,
                                dry_run_docker=True,
                            )

                            mock_show.deploy_aws.assert_called_once_with(
                                True, feature_flags=[]
                            )

    @patch("showtime.core.pull_request.get_aws")
    @patch("showtime.core.pull_request.get_github")
    def test_sync_updates_flags_on_running_env(
        self, mock_get_github: Mock, mock_get_aws: Mock
    ) -> None:
        """When env is running and flags differ from current, reconcile is triggered"""
        mock_github = Mock()
        mock_get_github.return_value = mock_github
        mock_aws = Mock()
        mock_get_aws.return_value = mock_aws

        # Existing running environment, same SHA, no triggers
        labels = [
            "🎪 abc123f 🚦 running",
            "🎪 🎯 abc123f",
        ]
        mock_github.get_labels.return_value = labels
        mock_github.get_pr_data.return_value = {
            "body": "FEATURE_ALERTS=true\nFEATURE_EMBEDDED=false"
        }
        # Current flags differ from desired (ALERTS was False, now should be True)
        mock_aws.get_current_feature_flags.return_value = {
            "SUPERSET_FEATURE_ALERTS": "False",
            "SUPERSET_FEATURE_EMBEDDED": "False",
        }
        mock_aws.reconcile_feature_flags.return_value = True

        pr = PullRequest(1234, labels)

        result = pr.sync("abc123f")

        assert result.success is True
        assert result.action_taken == "no_action"

        # Should reconcile with the full desired flag set
        mock_aws.reconcile_feature_flags.assert_called_once_with(
            "pr-1234-abc123f-service",
            {
                "SUPERSET_FEATURE_ALERTS": "True",
                "SUPERSET_FEATURE_EMBEDDED": "False",
            },
        )

    @patch("showtime.core.pull_request.get_aws")
    @patch("showtime.core.pull_request.get_github")
    def test_sync_skips_update_when_flags_match(
        self, mock_get_github: Mock, mock_get_aws: Mock
    ) -> None:
        """When env is running and flags already match, no update is triggered"""
        mock_github = Mock()
        mock_get_github.return_value = mock_github
        mock_aws = Mock()
        mock_get_aws.return_value = mock_aws

        labels = [
            "🎪 abc123f 🚦 running",
            "🎪 🎯 abc123f",
        ]
        mock_github.get_labels.return_value = labels
        mock_github.get_pr_data.return_value = {
            "body": "FEATURE_ALERTS=true"
        }
        # Current flags already match desired
        mock_aws.get_current_feature_flags.return_value = {
            "SUPERSET_FEATURE_ALERTS": "True",
        }

        pr = PullRequest(1234, labels)

        result = pr.sync("abc123f")

        assert result.success is True
        assert result.action_taken == "no_action"
        mock_aws.reconcile_feature_flags.assert_not_called()

    @patch("showtime.core.pull_request.get_aws")
    @patch("showtime.core.pull_request.get_github")
    def test_sync_no_flags_skips_ecs_calls(
        self, mock_get_github: Mock, mock_get_aws: Mock
    ) -> None:
        """When PR has no flags, skip ECS API calls entirely (no unnecessary cost)"""
        mock_github = Mock()
        mock_get_github.return_value = mock_github
        mock_aws = Mock()
        mock_get_aws.return_value = mock_aws

        labels = [
            "🎪 abc123f 🚦 running",
            "🎪 🎯 abc123f",
        ]
        mock_github.get_labels.return_value = labels
        mock_github.get_pr_data.return_value = {
            "body": "No feature flags here"
        }

        pr = PullRequest(1234, labels)

        result = pr.sync("abc123f")

        assert result.success is True
        assert result.action_taken == "no_action"
        # No ECS API calls when PR has no flags
        mock_aws.get_current_feature_flags.assert_not_called()
        mock_aws.reconcile_feature_flags.assert_not_called()

    @patch("showtime.core.pull_request.get_aws")
    @patch("showtime.core.pull_request.get_github")
    def test_sync_removes_partial_flags(
        self, mock_get_github: Mock, mock_get_aws: Mock
    ) -> None:
        """Removing one flag from PR description removes it from running env"""
        mock_github = Mock()
        mock_get_github.return_value = mock_github
        mock_aws = Mock()
        mock_get_aws.return_value = mock_aws

        labels = [
            "🎪 abc123f 🚦 running",
            "🎪 🎯 abc123f",
        ]
        mock_github.get_labels.return_value = labels
        # Only ALERTS remains in description, EMBEDDED was removed
        mock_github.get_pr_data.return_value = {
            "body": "FEATURE_ALERTS=true"
        }
        # ECS still has both flags
        mock_aws.get_current_feature_flags.return_value = {
            "SUPERSET_FEATURE_ALERTS": "True",
            "SUPERSET_FEATURE_EMBEDDED": "False",
        }
        mock_aws.reconcile_feature_flags.return_value = True

        pr = PullRequest(1234, labels)

        result = pr.sync("abc123f")

        assert result.success is True
        assert result.action_taken == "no_action"
        # Should reconcile with only ALERTS — EMBEDDED will be removed
        mock_aws.reconcile_feature_flags.assert_called_once_with(
            "pr-1234-abc123f-service",
            {"SUPERSET_FEATURE_ALERTS": "True"},
        )

    @patch("showtime.core.pull_request.get_github")
    def test_sync_graceful_on_pr_data_failure(self, mock_get_github: Mock) -> None:
        """If fetching PR data fails, sync continues without feature flags"""
        mock_github = Mock()
        mock_get_github.return_value = mock_github

        mock_github.get_labels.return_value = ["🎪 ⚡ showtime-trigger-start"]
        mock_github.get_pr_data.side_effect = Exception("API error")

        pr = PullRequest(1234, ["🎪 ⚡ showtime-trigger-start"])

        with patch.object(pr, "_atomic_claim", return_value=True):
            with patch.object(pr, "_create_new_show") as mock_create:
                with patch.object(pr, "_post_building_comment"):
                    with patch.object(pr, "_update_show_labels"):
                        with patch.object(pr, "_post_success_comment"):
                            mock_show = Show(
                                pr_number=1234, sha="abc123f", status="building"
                            )
                            mock_create.return_value = mock_show
                            mock_show.build_docker = Mock()  # type: ignore[method-assign]
                            mock_show.deploy_aws = Mock()  # type: ignore[method-assign]

                            result = pr.sync(
                                "abc123f",
                                dry_run_github=True,
                                dry_run_aws=True,
                                dry_run_docker=True,
                            )

                            assert result.success is True
                            # Should pass empty flags when PR data fetch fails
                            mock_show.deploy_aws.assert_called_once_with(
                                True, feature_flags=[]
                            )


class TestShowDeployAwsFeatureFlags:
    """Tests for Show.deploy_aws() feature_flags parameter"""

    @patch("showtime.core.show.get_interfaces")
    def test_deploy_aws_forwards_feature_flags(self, mock_get_interfaces: Mock) -> None:
        """deploy_aws() forwards feature_flags to aws.create_environment()"""
        mock_github = Mock()
        mock_aws = Mock()
        mock_get_interfaces.return_value = (mock_github, mock_aws)
        mock_aws.create_environment.return_value = Mock(success=True, ip="1.2.3.4")

        show = Show(pr_number=1234, sha="abc123f", status="deploying")
        flags = [{"name": "SUPERSET_FEATURE_X", "value": "true"}]

        show.deploy_aws(dry_run=False, feature_flags=flags)

        mock_aws.create_environment.assert_called_once_with(
            pr_number=1234,
            sha="abc123f" + "0" * 33,
            github_user="unknown",
            feature_flags=flags,
        )

    @patch("showtime.core.show.get_interfaces")
    def test_deploy_aws_none_feature_flags(self, mock_get_interfaces: Mock) -> None:
        """deploy_aws() passes None when no feature_flags provided"""
        mock_github = Mock()
        mock_aws = Mock()
        mock_get_interfaces.return_value = (mock_github, mock_aws)
        mock_aws.create_environment.return_value = Mock(success=True, ip="1.2.3.4")

        show = Show(pr_number=1234, sha="abc123f", status="deploying")

        show.deploy_aws(dry_run=False)

        mock_aws.create_environment.assert_called_once_with(
            pr_number=1234,
            sha="abc123f" + "0" * 33,
            github_user="unknown",
            feature_flags=None,
        )

    @patch("showtime.core.show.get_interfaces")
    def test_deploy_aws_dry_run_ignores_flags(self, mock_get_interfaces: Mock) -> None:
        """In dry-run mode, feature flags are accepted but AWS is not called"""
        mock_get_interfaces.return_value = (Mock(), Mock())

        show = Show(pr_number=1234, sha="abc123f", status="deploying")
        flags = [{"name": "SUPERSET_FEATURE_X", "value": "true"}]

        show.deploy_aws(dry_run=True, feature_flags=flags)

        assert show.ip == "52.1.2.3"
