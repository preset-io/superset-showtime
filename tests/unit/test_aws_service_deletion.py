"""
Tests for ECS service deletion race condition fix

When deleting an ECS service and recreating it with the same name,
AWS requires the old service to be fully deleted (not just DRAINING)
before creating a new one. Otherwise, AWS returns:
"Creation of service was not idempotent"

These tests verify that create_environment waits for service deletion
to complete before attempting to create a new service.
"""

from typing import Any, List
from unittest.mock import MagicMock, Mock, patch


class TestECSServiceDeletionRaceCondition:
    """Test that service deletion waits for completion before creating new service"""

    def _create_mock_aws_interface(self) -> MagicMock:
        """Create a mock AWSInterface with mocked boto3 clients"""
        with patch("showtime.core.aws.boto3") as mock_boto3:
            mock_ecs = MagicMock()
            mock_ecr = MagicMock()
            mock_ec2 = MagicMock()

            mock_boto3.client.side_effect = lambda service, **kwargs: {
                "ecs": mock_ecs,
                "ecr": mock_ecr,
                "ec2": mock_ec2,
            }[service]

            from showtime.core.aws import AWSInterface

            aws = AWSInterface()
            aws.ecs_client = mock_ecs
            aws.ecr_client = mock_ecr
            aws.ec2_client = mock_ec2

            return aws

    def test_create_environment_waits_for_deletion_when_service_exists(self) -> None:
        """
        When an existing service with the same name exists, create_environment
        should wait for deletion to complete before creating a new service.

        This test will FAIL until the fix is implemented because the current
        code deletes the service but doesn't wait for deletion to complete.
        """
        aws = self._create_mock_aws_interface()

        # Mock _find_pr_services to return an existing service with same name
        existing_service = {
            "service_name": "pr-1234-abc123f-service",
            "service_arn": "arn:aws:ecs:us-west-2:123456789:service/superset-ci/pr-1234-abc123f-service",
            "sha": "abc123f",
            "status": "ACTIVE",
            "running_count": 1,
            "desired_count": 1,
            "created_at": Mock(),
        }

        # Track method calls in order
        call_order: List[str] = []

        def track_delete(*args: Any, **kwargs: Any) -> bool:
            call_order.append("delete_ecs_service")
            return True

        def track_wait(*args: Any, **kwargs: Any) -> bool:
            call_order.append("wait_for_service_deletion")
            return True

        def track_create(*args: Any, **kwargs: Any) -> bool:
            call_order.append("create_ecs_service")
            return True

        # Mock the methods we want to track
        with patch.object(aws, "_find_pr_services", return_value=[existing_service]):
            with patch.object(
                aws, "_create_task_definition_with_image_and_flags", return_value="arn:task-def"
            ):
                with patch.object(aws, "_delete_ecs_service", side_effect=track_delete):
                    with patch.object(aws, "_wait_for_service_deletion", side_effect=track_wait):
                        with patch.object(aws, "_create_ecs_service", side_effect=track_create):
                            with patch.object(aws, "_deploy_task_definition", return_value=True):
                                with patch.object(
                                    aws, "_wait_for_service_stability", return_value=True
                                ):
                                    with patch.object(
                                        aws, "_health_check_service", return_value=True
                                    ):
                                        with patch.object(
                                            aws, "get_environment_ip", return_value="1.2.3.4"
                                        ):
                                            aws.create_environment(
                                                pr_number=1234,
                                                sha="abc123f",
                                                github_user="testuser",
                                            )

        # Verify the call order: delete -> wait -> create
        assert "delete_ecs_service" in call_order, "Should have called _delete_ecs_service"
        assert "create_ecs_service" in call_order, "Should have called _create_ecs_service"

        # The critical assertion: wait_for_service_deletion must be called
        # between delete and create
        assert (
            "wait_for_service_deletion" in call_order
        ), "Should have called _wait_for_service_deletion after deleting existing service"

        delete_idx = call_order.index("delete_ecs_service")
        wait_idx = call_order.index("wait_for_service_deletion")
        create_idx = call_order.index("create_ecs_service")

        assert (
            delete_idx < wait_idx < create_idx
        ), f"Expected delete -> wait -> create, but got: {call_order}"

    def test_create_environment_no_wait_when_no_existing_service(self) -> None:
        """
        When no existing service exists, should not call wait_for_service_deletion.
        """
        aws = self._create_mock_aws_interface()

        wait_called = False

        def track_wait(*args: Any, **kwargs: Any) -> bool:
            nonlocal wait_called
            wait_called = True
            return True

        # Mock _find_pr_services to return empty list (no existing service)
        with patch.object(aws, "_find_pr_services", return_value=[]):
            with patch.object(
                aws, "_create_task_definition_with_image_and_flags", return_value="arn:task-def"
            ):
                with patch.object(aws, "_wait_for_service_deletion", side_effect=track_wait):
                    with patch.object(aws, "_create_ecs_service", return_value=True):
                        with patch.object(aws, "_deploy_task_definition", return_value=True):
                            with patch.object(aws, "_wait_for_service_stability", return_value=True):
                                with patch.object(aws, "_health_check_service", return_value=True):
                                    with patch.object(
                                        aws, "get_environment_ip", return_value="1.2.3.4"
                                    ):
                                        aws.create_environment(
                                            pr_number=1234,
                                            sha="abc123f",
                                            github_user="testuser",
                                        )

        # Should NOT call wait_for_service_deletion when no service to delete
        assert not wait_called, "Should not wait for deletion when no existing service"

    def test_create_environment_with_force_flag_waits_for_deletion(self) -> None:
        """
        When force=True and service exists, should wait for deletion.
        This tests the existing force flag path which already has the wait.
        """
        aws = self._create_mock_aws_interface()

        call_order: List[str] = []

        def track_delete(*args: Any, **kwargs: Any) -> bool:
            call_order.append("delete_ecs_service")
            return True

        def track_wait(*args: Any, **kwargs: Any) -> bool:
            call_order.append("wait_for_service_deletion")
            return True

        def track_create(*args: Any, **kwargs: Any) -> bool:
            call_order.append("create_ecs_service")
            return True

        # Mock _service_exists to return True (force flag checks this)
        with patch.object(aws, "_service_exists", return_value=True):
            with patch.object(aws, "_find_pr_services", return_value=[]):
                with patch.object(
                    aws, "_create_task_definition_with_image_and_flags", return_value="arn:task-def"
                ):
                    with patch.object(aws, "_delete_ecs_service", side_effect=track_delete):
                        with patch.object(aws, "_wait_for_service_deletion", side_effect=track_wait):
                            with patch.object(aws, "_create_ecs_service", side_effect=track_create):
                                with patch.object(aws, "_deploy_task_definition", return_value=True):
                                    with patch.object(
                                        aws, "_wait_for_service_stability", return_value=True
                                    ):
                                        with patch.object(
                                            aws, "_health_check_service", return_value=True
                                        ):
                                            with patch.object(
                                                aws, "get_environment_ip", return_value="1.2.3.4"
                                            ):
                                                aws.create_environment(
                                                    pr_number=1234,
                                                    sha="abc123f",
                                                    github_user="testuser",
                                                    force=True,
                                                )

        # Force flag path should wait for deletion
        assert "delete_ecs_service" in call_order
        assert "wait_for_service_deletion" in call_order

        delete_idx = call_order.index("delete_ecs_service")
        wait_idx = call_order.index("wait_for_service_deletion")

        assert delete_idx < wait_idx, "Wait should come after delete"
