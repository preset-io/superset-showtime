"""
Shared pytest fixtures for showtime tests.

Provides:
- Fake AWS credentials to prevent network/IMDS lookups
- Stubber-based fixtures for behavior-based AWS tests
"""

import boto3
import pytest
from botocore.config import Config
from botocore.stub import Stubber


@pytest.fixture(autouse=True)
def fake_aws_credentials(monkeypatch):
    """Prevent any real AWS credential or IMDS lookups.

    This fixture runs automatically for all tests to ensure no accidental
    network calls or credential lookups occur during testing.
    """
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SECURITY_TOKEN", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-west-2")
    # Prevent IMDS lookups on EC2 instances
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")


@pytest.fixture
def ecs_client_with_stubber(fake_aws_credentials):
    """Create ECS client with Stubber for behavior-based tests.

    Usage:
        def test_something(self, ecs_client_with_stubber):
            client, stubber = ecs_client_with_stubber
            stubber.add_response("describe_services", {...})
            with stubber:
                result = do_something(client)
    """
    config = Config(
        region_name="us-west-2",
        retries={"max_attempts": 0},
    )
    client = boto3.client("ecs", config=config)
    stubber = Stubber(client)
    return client, stubber


@pytest.fixture
def aws_with_stubbed_clients(fake_aws_credentials):
    """Create AWSInterface with all clients stubbed.

    Usage:
        def test_something(self, aws_with_stubbed_clients):
            aws, stubbers = aws_with_stubbed_clients
            stubbers["ecs"].add_response("describe_services", {...})
            with stubbers["ecs"], stubbers["ecr"], stubbers["ec2"]:
                result = aws.some_method()
    """
    from showtime.core.aws import AWSInterface

    config = Config(region_name="us-west-2", retries={"max_attempts": 0})
    ecs = boto3.client("ecs", config=config)
    ecr = boto3.client("ecr", config=config)
    ec2 = boto3.client("ec2", config=config)

    ecs_stubber = Stubber(ecs)
    ecr_stubber = Stubber(ecr)
    ec2_stubber = Stubber(ec2)

    aws = AWSInterface(ecs_client=ecs, ecr_client=ecr, ec2_client=ec2)
    return aws, {"ecs": ecs_stubber, "ecr": ecr_stubber, "ec2": ec2_stubber}
