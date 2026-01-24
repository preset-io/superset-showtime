"""
Stubber flow tests - verify full create_environment orchestration with Stubber.

These are high-level flow tests. They verify the complete orchestration
of create_environment with realistic AWS stubbing. Fast, deterministic, runs in CI.

Note: time.sleep and httpx health checks must be patched since Stubber only handles boto3.
"""

from typing import Any
from unittest.mock import Mock, patch

from botocore.stub import ANY, Stubber


class TestCreateEnvironmentStubberFlow:
    """Full flow test using Stubber for realistic AWS interaction simulation"""

    def _stub_register_task_definition(self, stubber: Stubber) -> None:
        """Stub register_task_definition response.

        Note: We don't validate expected_params here because the task definition
        JSON contains many parameters. This flow test verifies orchestration,
        not task definition content.
        """
        stubber.add_response(
            "register_task_definition",
            {
                "taskDefinition": {
                    "taskDefinitionArn": "arn:aws:ecs:us-west-2:123456789:task-definition/superset-ci:100",
                    "family": "superset-ci",
                    "revision": 100,
                    "containerDefinitions": [],
                }
            },
        )

    def _stub_describe_services_not_found(self, stubber: Stubber, service_name: str) -> None:
        """Stub describe_services returning empty (service doesn't exist)"""
        stubber.add_response(
            "describe_services",
            {"services": [], "failures": []},
            expected_params={"cluster": "superset-ci", "services": [service_name]},
        )

    def _stub_create_service(self, stubber: Stubber, service_name: str) -> None:
        """Stub create_service response"""
        stubber.add_response(
            "create_service",
            {
                "service": {
                    "serviceName": service_name,
                    "serviceArn": f"arn:aws:ecs:us-west-2:123456789:service/superset-ci/{service_name}",
                    "status": "ACTIVE",
                    "desiredCount": 1,
                    "runningCount": 0,
                    "taskDefinition": "arn:aws:ecs:us-west-2:123456789:task-definition/superset-ci:100",
                }
            },
            expected_params={
                "cluster": "superset-ci",
                "serviceName": service_name,
                "taskDefinition": ANY,
                "launchType": "FARGATE",
                "desiredCount": 1,
                "platformVersion": "LATEST",
                "networkConfiguration": ANY,
                "tags": ANY,
            },
        )

    def _stub_update_service(self, stubber: Stubber, service_name: str) -> None:
        """Stub update_service response"""
        stubber.add_response(
            "update_service",
            {
                "service": {
                    "serviceName": service_name,
                    "status": "ACTIVE",
                    "desiredCount": 1,
                    "runningCount": 1,
                }
            },
            expected_params={
                "cluster": "superset-ci",
                "service": service_name,
                "taskDefinition": ANY,
            },
        )

    def _stub_list_tasks(self, stubber: Stubber, service_name: str) -> None:
        """Stub list_tasks response"""
        stubber.add_response(
            "list_tasks",
            {
                "taskArns": [
                    f"arn:aws:ecs:us-west-2:123456789:task/superset-ci/task-{service_name}"
                ]
            },
            expected_params={"cluster": "superset-ci", "serviceName": service_name},
        )

    def _stub_describe_tasks(self, stubber: Stubber) -> None:
        """Stub describe_tasks response with network interface"""
        stubber.add_response(
            "describe_tasks",
            {
                "tasks": [
                    {
                        "taskArn": "arn:aws:ecs:us-west-2:123456789:task/superset-ci/task-123",
                        "lastStatus": "RUNNING",
                        "attachments": [
                            {
                                "type": "ElasticNetworkInterface",
                                "details": [
                                    {"name": "networkInterfaceId", "value": "eni-12345678"},
                                ],
                            }
                        ],
                    }
                ]
            },
            expected_params={"cluster": "superset-ci", "tasks": ANY},
        )

    def _stub_describe_network_interfaces(self, ec2_stubber: Stubber) -> None:
        """Stub describe_network_interfaces response with public IP"""
        ec2_stubber.add_response(
            "describe_network_interfaces",
            {
                "NetworkInterfaces": [
                    {
                        "NetworkInterfaceId": "eni-12345678",
                        "Association": {"PublicIp": "54.123.45.67"},
                    }
                ]
            },
            expected_params={"NetworkInterfaceIds": ["eni-12345678"]},
        )

    def test_create_environment_success_flow(self, aws_with_stubbed_clients: Any) -> None:
        """Verify complete create_environment flow with all AWS calls stubbed"""
        aws, stubbers = aws_with_stubbed_clients
        ecs_stubber = stubbers["ecs"]
        ec2_stubber = stubbers["ec2"]

        service_name = "pr-1234-abc123f-service"

        # Stub all AWS calls in expected order
        self._stub_register_task_definition(ecs_stubber)
        self._stub_describe_services_not_found(ecs_stubber, service_name)
        self._stub_create_service(ecs_stubber, service_name)
        self._stub_update_service(ecs_stubber, service_name)

        # get_environment_ip is called twice:
        # 1. By _health_check_service to get IP for HTTP health check
        # 2. By create_environment to return IP in result
        # We need to stub both calls
        self._stub_list_tasks(ecs_stubber, service_name)
        self._stub_describe_tasks(ecs_stubber)
        self._stub_describe_network_interfaces(ec2_stubber)
        # Second call for final IP retrieval
        self._stub_list_tasks(ecs_stubber, service_name)
        self._stub_describe_tasks(ecs_stubber)
        self._stub_describe_network_interfaces(ec2_stubber)

        with ecs_stubber, ec2_stubber, stubbers["ecr"]:
            # Mock the ECS waiter to avoid complex stubbing
            mock_waiter = Mock()
            aws.ecs_client.get_waiter = Mock(return_value=mock_waiter)

            # Patch time.sleep to make test fast
            with patch("time.sleep"):
                # Patch health check (httpx) since we're not stubbing HTTP
                with patch("httpx.Client") as mock_httpx:
                    mock_response = Mock(status_code=200)
                    mock_httpx.return_value.__enter__.return_value.get.return_value = mock_response

                    result = aws.create_environment(
                        pr_number=1234,
                        sha="abc123f",
                        github_user="testuser",
                    )

        # Verify success
        assert result.success is True, f"Expected success but got error: {result.error}"
        assert result.ip == "54.123.45.67"
        assert result.service_name == service_name

        # Verify all stubs were used
        ecs_stubber.assert_no_pending_responses()
        ec2_stubber.assert_no_pending_responses()

    def test_create_environment_with_existing_draining_service(
        self, aws_with_stubbed_clients: Any
    ) -> None:
        """Verify flow when existing DRAINING service must be waited out"""
        aws, stubbers = aws_with_stubbed_clients
        ecs_stubber = stubbers["ecs"]
        ec2_stubber = stubbers["ec2"]

        service_name = "pr-1234-abc123f-service"

        # Stub task definition
        self._stub_register_task_definition(ecs_stubber)

        # First describe_services: service exists and is DRAINING
        ecs_stubber.add_response(
            "describe_services",
            {
                "services": [{"serviceName": service_name, "status": "DRAINING"}],
                "failures": [],
            },
            expected_params={"cluster": "superset-ci", "services": [service_name]},
        )

        # delete_service call
        ecs_stubber.add_response(
            "delete_service",
            {"service": {"serviceName": service_name, "status": "DRAINING"}},
            expected_params={"cluster": "superset-ci", "service": service_name, "force": True},
        )

        # Wait loop: first check still DRAINING, second check gone
        ecs_stubber.add_response(
            "describe_services",
            {
                "services": [{"serviceName": service_name, "status": "DRAINING"}],
                "failures": [],
            },
            expected_params={"cluster": "superset-ci", "services": [service_name]},
        )
        ecs_stubber.add_response(
            "describe_services",
            {"services": [], "failures": []},
            expected_params={"cluster": "superset-ci", "services": [service_name]},
        )

        # Now create succeeds
        self._stub_create_service(ecs_stubber, service_name)
        self._stub_update_service(ecs_stubber, service_name)
        # get_environment_ip is called twice (health check + final result)
        self._stub_list_tasks(ecs_stubber, service_name)
        self._stub_describe_tasks(ecs_stubber)
        self._stub_describe_network_interfaces(ec2_stubber)
        self._stub_list_tasks(ecs_stubber, service_name)
        self._stub_describe_tasks(ecs_stubber)
        self._stub_describe_network_interfaces(ec2_stubber)

        with ecs_stubber, ec2_stubber, stubbers["ecr"]:
            mock_waiter = Mock()
            aws.ecs_client.get_waiter = Mock(return_value=mock_waiter)

            with patch("time.sleep"):
                with patch("httpx.Client") as mock_httpx:
                    mock_response = Mock(status_code=200)
                    mock_httpx.return_value.__enter__.return_value.get.return_value = mock_response

                    result = aws.create_environment(
                        pr_number=1234,
                        sha="abc123f",
                        github_user="testuser",
                    )

        assert result.success is True, f"Expected success but got error: {result.error}"
        ecs_stubber.assert_no_pending_responses()
