"""
Shared pytest fixtures for showtime tests.

Provides:
- Fake AWS credentials to prevent network/IMDS lookups
- Stubber-based fixtures for behavior-based AWS tests
"""

from typing import Dict, List, Set, Tuple, Type

import boto3
import pytest
from botocore.config import Config
from botocore.stub import Stubber


class StatefulGitHubFake:
    """In-memory GitHub fake whose reads observe prior label writes."""

    def __init__(self, labels: List[str]) -> None:
        self.labels = set(labels)
        self.writes: List[Tuple[str, str]] = []
        self.comments: List[str] = []
        self.fail_add: Set[str] = set()
        self.fail_remove: Set[str] = set()
        self.fail_comments = False
        self.base_url = "https://api.github.test"
        self.org = "apache"
        self.repo = "superset"
        self.headers: Dict[str, str] = {}

    def get_labels(self, pr_number: int) -> List[str]:
        """Return the current label attachments."""
        return sorted(self.labels)

    def add_label(self, pr_number: int, label: str) -> None:
        """Attach a label or raise a configured failure."""
        self.writes.append(("add", label))
        if label in self.fail_add:
            raise RuntimeError(f"failed to add {label}")
        self.labels.add(label)

    def remove_label(self, pr_number: int, label: str) -> None:
        """Detach a label or raise a configured failure."""
        self.writes.append(("remove", label))
        if label in self.fail_remove:
            raise RuntimeError(f"failed to remove {label}")
        self.labels.discard(label)

    def post_comment(self, pr_number: int, comment: str) -> None:
        """Record a comment or raise independently of label writes."""
        if self.fail_comments:
            raise RuntimeError("comment failed")
        self.comments.append(comment)

    def get_pr_data(self, pr_number: int) -> Dict[str, str]:
        """Return the minimal PR payload needed by lifecycle tests."""
        return {"body": "", "state": "open"}


@pytest.fixture
def github_fake() -> Type[StatefulGitHubFake]:
    """Provide the stateful GitHub fake constructor to tests."""
    return StatefulGitHubFake


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
        connect_timeout=2,
        read_timeout=5,
        retries={"total_max_attempts": 1, "mode": "standard"},
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

    config = Config(
        region_name="us-west-2",
        connect_timeout=2,
        read_timeout=5,
        retries={"total_max_attempts": 1, "mode": "standard"},
    )
    ecs = boto3.client("ecs", config=config)
    ecr = boto3.client("ecr", config=config)
    ec2 = boto3.client("ec2", config=config)

    ecs_stubber = Stubber(ecs)
    ecr_stubber = Stubber(ecr)
    ec2_stubber = Stubber(ec2)

    aws = AWSInterface(ecs_client=ecs, ecr_client=ecr, ec2_client=ec2)
    return aws, {"ecs": ecs_stubber, "ecr": ecr_stubber, "ec2": ec2_stubber}
