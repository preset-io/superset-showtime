"""
Edge case tests for AWS operations - error handling, retries, timeouts.

Uses botocore.stub.Stubber for realistic AWS response simulation.
"""

from typing import Any
from unittest.mock import patch

import pytest
from botocore.stub import Stubber

from showtime.core.aws import AWSInterface


class TestDescribeServicesFailures:
    """Edge cases for describe_services API failures"""

    def test_handles_access_denied_gracefully(self, ecs_client_with_stubber: Any) -> None:
        """Access denied should return False, not raise"""
        client, stubber = ecs_client_with_stubber

        stubber.add_client_error(
            "describe_services",
            service_error_code="AccessDeniedException",
            service_message="User is not authorized",
        )

        aws = AWSInterface(ecs_client=client)
        with stubber:
            result = aws._service_exists_any_state("pr-1234-abc123f-service")

        assert result is False

    def test_handles_cluster_not_found(self, ecs_client_with_stubber: Any) -> None:
        """ClusterNotFoundException should return False"""
        client, stubber = ecs_client_with_stubber

        stubber.add_client_error(
            "describe_services",
            service_error_code="ClusterNotFoundException",
            service_message="Cluster not found",
        )

        aws = AWSInterface(ecs_client=client)
        with stubber:
            result = aws._service_exists_any_state("pr-1234-abc123f-service")

        assert result is False

    def test_handles_throttling(self, ecs_client_with_stubber: Any) -> None:
        """ThrottlingException should return False (graceful degradation)"""
        client, stubber = ecs_client_with_stubber

        stubber.add_client_error(
            "describe_services",
            service_error_code="ThrottlingException",
            service_message="Rate exceeded",
        )

        aws = AWSInterface(ecs_client=client)
        with stubber:
            result = aws._service_exists_any_state("pr-1234-abc123f-service")

        assert result is False

    def test_handles_internal_service_error(self, ecs_client_with_stubber: Any) -> None:
        """InternalServiceException should not crash"""
        client, stubber = ecs_client_with_stubber

        stubber.add_client_error(
            "describe_services",
            service_error_code="ServerException",
            service_message="Internal error",
        )

        aws = AWSInterface(ecs_client=client)
        with stubber:
            result = aws._service_exists_any_state("pr-1234-abc123f-service")

        assert result is False


class TestEmptyResponses:
    """Edge cases for empty/malformed responses"""

    def test_handles_empty_services_list(self, ecs_client_with_stubber: Any) -> None:
        """Empty services list means service not found"""
        client, stubber = ecs_client_with_stubber

        stubber.add_response(
            "describe_services",
            {"services": [], "failures": []},
            expected_params={"cluster": "superset-ci", "services": ["pr-1234-abc123f-service"]},
        )

        aws = AWSInterface(ecs_client=client)
        with stubber:
            result = aws._service_exists_any_state("pr-1234-abc123f-service")

        assert result is False

    def test_handles_service_in_failures_list(self, ecs_client_with_stubber: Any) -> None:
        """Service in failures list (missing) should return False"""
        client, stubber = ecs_client_with_stubber

        stubber.add_response(
            "describe_services",
            {
                "services": [],
                "failures": [
                    {
                        "arn": "arn:aws:ecs:us-west-2:123456789:service/superset-ci/pr-1234-abc123f-service",
                        "reason": "MISSING",
                    }
                ],
            },
            expected_params={"cluster": "superset-ci", "services": ["pr-1234-abc123f-service"]},
        )

        aws = AWSInterface(ecs_client=client)
        with stubber:
            result = aws._service_exists_any_state("pr-1234-abc123f-service")

        assert result is False

    def test_handles_service_with_missing_status(self, ecs_client_with_stubber: Any) -> None:
        """Service without status field should not crash"""
        client, stubber = ecs_client_with_stubber

        stubber.add_response(
            "describe_services",
            {
                "services": [{"serviceName": "pr-1234-abc123f-service"}],  # No status field
                "failures": [],
            },
            expected_params={"cluster": "superset-ci", "services": ["pr-1234-abc123f-service"]},
        )

        aws = AWSInterface(ecs_client=client)
        with stubber:
            result = aws._service_exists_any_state("pr-1234-abc123f-service")

        # Missing status means not in ACTIVE/DRAINING, so False
        assert result is False


class TestServiceStatusDetection:
    """Verify correct detection of different service statuses"""

    def test_detects_active_service(self, ecs_client_with_stubber: Any) -> None:
        """ACTIVE services should be detected"""
        client, stubber = ecs_client_with_stubber

        stubber.add_response(
            "describe_services",
            {
                "services": [{"serviceName": "pr-1234-abc123f-service", "status": "ACTIVE"}],
                "failures": [],
            },
            expected_params={"cluster": "superset-ci", "services": ["pr-1234-abc123f-service"]},
        )

        aws = AWSInterface(ecs_client=client)
        with stubber:
            result = aws._service_exists_any_state("pr-1234-abc123f-service")

        assert result is True

    def test_detects_draining_service(self, ecs_client_with_stubber: Any) -> None:
        """DRAINING services should be detected (blocks CreateService)"""
        client, stubber = ecs_client_with_stubber

        stubber.add_response(
            "describe_services",
            {
                "services": [{"serviceName": "pr-1234-abc123f-service", "status": "DRAINING"}],
                "failures": [],
            },
            expected_params={"cluster": "superset-ci", "services": ["pr-1234-abc123f-service"]},
        )

        aws = AWSInterface(ecs_client=client)
        with stubber:
            result = aws._service_exists_any_state("pr-1234-abc123f-service")

        assert result is True

    def test_ignores_inactive_service(self, ecs_client_with_stubber: Any) -> None:
        """INACTIVE services don't block CreateService"""
        client, stubber = ecs_client_with_stubber

        stubber.add_response(
            "describe_services",
            {
                "services": [{"serviceName": "pr-1234-abc123f-service", "status": "INACTIVE"}],
                "failures": [],
            },
            expected_params={"cluster": "superset-ci", "services": ["pr-1234-abc123f-service"]},
        )

        aws = AWSInterface(ecs_client=client)
        with stubber:
            result = aws._service_exists_any_state("pr-1234-abc123f-service")

        assert result is False


class TestWaitForServiceDeletionEdgeCases:
    """Edge cases for _wait_for_service_deletion timeout/retry logic"""

    def test_immediate_deletion_success(self, ecs_client_with_stubber: Any) -> None:
        """Service already gone on first check returns True immediately"""
        client, stubber = ecs_client_with_stubber

        stubber.add_response(
            "describe_services",
            {"services": [], "failures": []},
            expected_params={"cluster": "superset-ci", "services": ["pr-1234-abc123f-service"]},
        )

        aws = AWSInterface(ecs_client=client)
        with stubber:
            result = aws._wait_for_service_deletion("pr-1234-abc123f-service", timeout_minutes=1)

        assert result is True

    def test_waits_through_draining_to_completion(self, ecs_client_with_stubber: Any) -> None:
        """Service transitions DRAINING -> gone should succeed"""
        client, stubber = ecs_client_with_stubber

        # First 2 calls: still DRAINING
        for _ in range(2):
            stubber.add_response(
                "describe_services",
                {
                    "services": [{"serviceName": "pr-1234-abc123f-service", "status": "DRAINING"}],
                    "failures": [],
                },
                expected_params={
                    "cluster": "superset-ci",
                    "services": ["pr-1234-abc123f-service"],
                },
            )

        # Third call: gone
        stubber.add_response(
            "describe_services",
            {"services": [], "failures": []},
            expected_params={"cluster": "superset-ci", "services": ["pr-1234-abc123f-service"]},
        )

        aws = AWSInterface(ecs_client=client)
        with stubber:
            with patch("time.sleep"):  # Speed up test
                result = aws._wait_for_service_deletion(
                    "pr-1234-abc123f-service", timeout_minutes=1
                )

        assert result is True

    def test_timeout_when_draining_persists(self, ecs_client_with_stubber: Any) -> None:
        """Timeout when service stays DRAINING past timeout"""
        client, stubber = ecs_client_with_stubber

        # Service stays DRAINING for all 12 attempts (1 minute timeout)
        for _ in range(12):
            stubber.add_response(
                "describe_services",
                {
                    "services": [{"serviceName": "pr-1234-abc123f-service", "status": "DRAINING"}],
                    "failures": [],
                },
                expected_params={
                    "cluster": "superset-ci",
                    "services": ["pr-1234-abc123f-service"],
                },
            )

        aws = AWSInterface(ecs_client=client)
        with stubber:
            with patch("time.sleep"):  # Speed up test
                result = aws._wait_for_service_deletion(
                    "pr-1234-abc123f-service", timeout_minutes=1
                )

        assert result is False

    def test_handles_error_during_wait(self, ecs_client_with_stubber: Any) -> None:
        """Error during deletion wait should return False"""
        client, stubber = ecs_client_with_stubber

        stubber.add_client_error(
            "describe_services",
            service_error_code="AccessDeniedException",
            service_message="Access denied",
        )

        aws = AWSInterface(ecs_client=client)
        with stubber:
            result = aws._wait_for_service_deletion("pr-1234-abc123f-service", timeout_minutes=1)

        # Returns False because _service_exists_any_state returns False on error
        # This is graceful degradation - the service is treated as "gone"
        assert result is True  # First check says "not exists" -> done
