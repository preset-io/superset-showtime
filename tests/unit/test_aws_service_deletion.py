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

        The code now uses _service_exists_any_state() to detect services in ANY
        state (including DRAINING), not _find_pr_services() which used list_services().
        """
        aws = self._create_mock_aws_interface()

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

        # Mock _service_exists_any_state to return True (service exists)
        with patch.object(aws, "_service_exists_any_state", return_value=True):
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

        # Mock _service_exists_any_state to return False (no existing service)
        with patch.object(aws, "_service_exists_any_state", return_value=False):
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

    def test_create_environment_detects_draining_service(self) -> None:
        """
        BUG REPRODUCTION: When a service is in DRAINING state (from a previous
        failed/cancelled deployment), list_services() doesn't return it.
        This causes create_environment to skip the deletion wait, and AWS
        rejects the create with "Creation of service was not idempotent".

        The fix should use describe_services() directly with the exact service
        name, which DOES return DRAINING services.

        Scenario:
        1. Previous deployment created pr-1234-abc123f-service
        2. Previous deployment failed/was cancelled, service is now DRAINING
        3. New deployment starts for same SHA
        4. list_services() returns empty (no ACTIVE services)
        5. _find_pr_services() returns empty
        6. Code skips deletion wait
        7. create_service() fails: "Creation of service was not idempotent"
        """
        aws = self._create_mock_aws_interface()

        service_name = "pr-1234-abc123f-service"

        # Simulate the bug scenario:
        # - list_services returns empty (DRAINING services not listed)
        # - BUT describe_services finds the DRAINING service
        aws.ecs_client.list_services.return_value = {"serviceArns": []}

        # describe_services DOES return DRAINING services
        aws.ecs_client.describe_services.return_value = {
            "services": [
                {
                    "serviceName": service_name,
                    "status": "DRAINING",  # The key: service exists but is DRAINING
                    "runningCount": 0,
                    "desiredCount": 0,
                }
            ]
        }

        # Track if we waited for deletion
        wait_called = False

        def track_wait(svc_name: str, *args: Any, **kwargs: Any) -> bool:
            nonlocal wait_called
            wait_called = True
            return True

        # Simulate AWS rejecting create because service exists in DRAINING state
        def mock_create_service(*args: Any, **kwargs: Any) -> None:
            if not wait_called:
                # This is what AWS returns when you try to create a service
                # that already exists (even in DRAINING state)
                from botocore.exceptions import ClientError

                raise ClientError(
                    {
                        "Error": {
                            "Code": "InvalidParameterException",
                            "Message": "Creation of service was not idempotent.",
                        }
                    },
                    "CreateService",
                )
            # If we waited, the service would be gone and create succeeds
            return None

        aws.ecs_client.create_service.side_effect = mock_create_service

        with patch.object(
            aws, "_create_task_definition_with_image_and_flags", return_value="arn:task-def"
        ):
            with patch.object(aws, "_wait_for_service_deletion", side_effect=track_wait):
                with patch.object(aws, "_deploy_task_definition", return_value=True):
                    with patch.object(aws, "_wait_for_service_stability", return_value=True):
                        with patch.object(aws, "_health_check_service", return_value=True):
                            with patch.object(aws, "get_environment_ip", return_value="1.2.3.4"):
                                result = aws.create_environment(
                                    pr_number=1234,
                                    sha="abc123f",
                                    github_user="testuser",
                                )

        # The fix should detect the DRAINING service and wait for deletion
        assert wait_called, (
            "Should detect DRAINING service via describe_services and wait for deletion. "
            "Current code uses list_services which doesn't return DRAINING services."
        )
        assert result.success, f"Should succeed after waiting for DRAINING service: {result.error}"

    def test_service_exists_any_state_detects_draining(self) -> None:
        """
        Test that _service_exists_any_state() detects services in ANY state,
        including DRAINING (unlike _service_exists which only checks ACTIVE).

        This method should be used before creating a service to detect
        DRAINING services that would cause "not idempotent" errors.
        """
        aws = self._create_mock_aws_interface()

        service_name = "pr-1234-abc123f-service"

        # Service is DRAINING (not ACTIVE)
        aws.ecs_client.describe_services.return_value = {
            "services": [
                {
                    "serviceName": service_name,
                    "status": "DRAINING",
                }
            ]
        }

        # Current _service_exists only checks for ACTIVE
        # This should return False for DRAINING (current behavior)
        exists_active_only = aws._service_exists(service_name)

        # New method should detect ANY state including DRAINING
        # This test will fail until _service_exists_any_state is implemented
        exists_any_state = aws._service_exists_any_state(service_name)

        assert not exists_active_only, "_service_exists should only detect ACTIVE services"
        assert exists_any_state, "_service_exists_any_state should detect DRAINING services"

    def test_wait_for_service_deletion_waits_through_draining(self) -> None:
        """
        Test that _wait_for_service_deletion waits until service is fully gone,
        not just until it's no longer ACTIVE.

        Simulates a service that stays in DRAINING state for 3 iterations
        before being fully deleted.
        """
        aws = self._create_mock_aws_interface()

        service_name = "pr-1234-abc123f-service"
        call_count = 0

        def mock_describe_services(*args: Any, **kwargs: Any) -> dict:
            nonlocal call_count
            call_count += 1

            # First 3 calls: service is DRAINING (still blocking)
            if call_count <= 3:
                return {
                    "services": [
                        {
                            "serviceName": service_name,
                            "status": "DRAINING",
                        }
                    ]
                }
            # After 3 calls: service is gone
            return {"services": []}

        aws.ecs_client.describe_services.side_effect = mock_describe_services

        # Patch time.sleep to speed up test
        with patch("time.sleep", return_value=None):
            result = aws._wait_for_service_deletion(service_name, timeout_minutes=1)

        # Should have waited through DRAINING iterations
        assert call_count >= 4, f"Should have checked at least 4 times, but only checked {call_count}"
        assert result is True, "Should return True after service is fully deleted"

    def test_wait_for_service_deletion_fails_if_draining_persists(self) -> None:
        """
        Test that _wait_for_service_deletion times out if service stays DRAINING.
        """
        aws = self._create_mock_aws_interface()

        service_name = "pr-1234-abc123f-service"

        # Service stays DRAINING forever
        aws.ecs_client.describe_services.return_value = {
            "services": [
                {
                    "serviceName": service_name,
                    "status": "DRAINING",
                }
            ]
        }

        # Patch time.sleep to speed up test
        with patch("time.sleep", return_value=None):
            # Very short timeout (1 minute = 12 attempts at 5s intervals)
            result = aws._wait_for_service_deletion(service_name, timeout_minutes=1)

        # Should timeout and return False
        assert result is False, "Should return False when service stays DRAINING past timeout"
        # Should have made multiple attempts
        assert aws.ecs_client.describe_services.call_count >= 10, "Should have retried multiple times"
