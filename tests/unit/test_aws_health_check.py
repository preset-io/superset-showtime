"""
Tests for health check retry behavior.
"""

from unittest.mock import MagicMock, patch

import httpx
import pytest

from showtime.core.aws import AWSInterface


@pytest.fixture
def aws() -> AWSInterface:
    """AWSInterface with mocked clients"""
    return AWSInterface(
        ecs_client=MagicMock(),
        ecr_client=MagicMock(),
        ec2_client=MagicMock(),
    )


class TestHealthCheckRetries:
    """Core retry behavior tests"""

    def test_succeeds_on_healthy_response(self, aws: AWSInterface) -> None:
        """Returns True when health endpoint responds 200"""
        with patch.object(aws, "get_environment_ip", return_value="1.2.3.4"):
            with patch("httpx.Client") as mock_client:
                mock_response = MagicMock(status_code=200)
                mock_client.return_value.__enter__.return_value.get.return_value = mock_response

                assert aws._health_check_service("test-service", max_attempts=3) is True

    def test_retries_and_succeeds(self, aws: AWSInterface) -> None:
        """Retries on failure and succeeds when health check eventually passes"""
        with patch.object(aws, "get_environment_ip", return_value="1.2.3.4"):
            with patch("httpx.Client") as mock_client:
                mock_instance = mock_client.return_value.__enter__.return_value
                # Fail twice (health + fallback each), then succeed
                mock_instance.get.side_effect = [
                    httpx.RequestError("refused"), MagicMock(status_code=503),  # attempt 1
                    httpx.RequestError("refused"), MagicMock(status_code=503),  # attempt 2
                    MagicMock(status_code=200),  # attempt 3 succeeds
                ]

                with patch("time.sleep"):
                    assert aws._health_check_service("test-service", max_attempts=5) is True

    def test_fails_after_max_attempts_exhausted(self, aws: AWSInterface) -> None:
        """Returns False after all attempts fail"""
        with patch.object(aws, "get_environment_ip", return_value="1.2.3.4"):
            with patch("httpx.Client") as mock_client:
                mock_client.return_value.__enter__.return_value.get.side_effect = (
                    httpx.RequestError("refused")
                )

                with patch("time.sleep"):
                    assert aws._health_check_service("test-service", max_attempts=3) is False
