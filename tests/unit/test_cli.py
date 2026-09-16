# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.

from contextlib import ExitStack
from unittest.mock import Mock, patch

from typer.testing import CliRunner

from showtime.cli import app
from showtime.core.pull_request import PullRequest, SyncResult


def test_stop_without_pointer_still_invokes_teardown() -> None:
    """The CLI must stop tracked shows even when no active pointer exists."""
    pr = PullRequest(1234, ["🎪 abc123f 🚦 failed"])
    pr.stop_environment = Mock(return_value=SyncResult(False, "stop_failed", error="pending"))

    with patch.object(PullRequest, "from_id", return_value=pr):
        result = CliRunner().invoke(app, ["stop", "1234", "--force"])

    assert result.exit_code == 1
    pr.stop_environment.assert_called_once()


def test_closed_pr_sync_cleanup_failure_exits_nonzero() -> None:
    """Closed-PR cleanup failure must propagate to automation."""
    github = Mock()
    github.get_pr_data.return_value = {"state": "closed"}
    pr = PullRequest(1234, ["🎪 abc123f 🚦 failed"])
    pr.stop_environment = Mock(return_value=SyncResult(False, "stop_failed", error="pending"))

    with ExitStack() as stack:
        stack.enter_context(patch.object(PullRequest, "from_id", return_value=pr))
        stack.enter_context(patch("showtime.core.pull_request.get_github", return_value=github))
        stack.enter_context(
            patch("showtime.core.git_validation.should_skip_validation", return_value=True)
        )
        result = CliRunner().invoke(app, ["sync", "1234", "--sha", "abc123f"])

    assert result.exit_code == 1


def test_cleanup_false_aws_delete_exits_nonzero() -> None:
    """Aggregate cleanup must count a false AWS deletion as failure."""
    aws = Mock()
    aws.list_circus_environments.return_value = [
        {
            "service_name": "pr-1234-abc123f-service",
            "task_definition_arn": "arn:task/candidate:1",
        }
    ]
    aws.delete_environment.return_value = False

    with ExitStack() as stack:
        stack.enter_context(
            patch.object(PullRequest, "find_all_with_environments", return_value=[])
        )
        stack.enter_context(patch("showtime.core.aws.AWSInterface", return_value=aws))
        result = CliRunner().invoke(
            app,
            [
                "cleanup",
                "--force",
                "--no-cleanup-labels",
                "--no-cleanup-closed-pr-labels",
            ],
        )

    assert result.exit_code == 1
    assert "everything is clean" not in result.output
