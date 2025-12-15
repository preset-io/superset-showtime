"""
🎪 GitHub PR comment templates and messaging utilities

Centralized PR comment functions with type hints and clean formatting.
"""

import os
from typing import Dict, List, Optional

from .show import Show

# AWS Console URL constants
BASE_AWS_URL = "https://us-west-2.console.aws.amazon.com/ecs/v2/clusters/superset-ci/services"
AWS_REGION = "us-west-2"


def get_github_actor() -> str:
    """Get current GitHub actor with fallback"""
    from .github import GitHubInterface

    return GitHubInterface.get_current_actor()


def get_github_workflow_url() -> str:
    """Get current GitHub Actions workflow URL"""
    return (
        os.getenv("GITHUB_SERVER_URL", "https://github.com")
        + f"/{os.getenv('GITHUB_REPOSITORY', 'repo')}/actions/runs/{os.getenv('GITHUB_RUN_ID', 'run')}"
    )


def get_showtime_footer() -> str:
    """Get consistent Showtime footer for PR comments"""
    return "*Powered by [Superset Showtime](https://github.com/mistercrunch/superset-showtime)*"


def _create_header_links(sha: str) -> Dict[str, str]:
    """Create standard header links for comments

    Args:
        sha: Commit SHA for the environment

    Returns:
        Dict with showtime_link, gha_link, commit_link
    """
    from .show import short_sha

    repo_path = get_repo_path()
    return {
        "showtime_link": "[Showtime](https://github.com/mistercrunch/superset-showtime)",
        "gha_link": f"[GHA]({get_github_workflow_url()})",
        "commit_link": f"[{short_sha(sha)}]({get_commit_url(repo_path, sha)})",
    }


def _format_comment(header: str, bullets: List[str]) -> str:
    """Format comment with header and bullet points

    Args:
        header: Comment header text
        bullets: List of bullet point strings (without •)
    """
    bullet_text = "\n".join(f"• {bullet}" for bullet in bullets)
    return f"{header}\n\n{bullet_text}"


def get_commit_url(repo_path: str, sha: str) -> str:
    """Get GitHub commit URL

    Args:
        repo_path: Repository path like 'apache/superset'
        sha: Full or short commit SHA
    """
    return f"https://github.com/{repo_path}/commit/{sha}"


def get_repo_path() -> str:
    """Get current repository path from environment"""
    return os.getenv("GITHUB_REPOSITORY", "apache/superset")


def get_aws_console_urls(service_name: str) -> Dict[str, str]:
    """Get AWS Console URLs for a service"""
    return {
        "logs": f"{BASE_AWS_URL}/{service_name}/logs?region={AWS_REGION}",
        "service": f"{BASE_AWS_URL}/{service_name}?region={AWS_REGION}",
        "health": f"{BASE_AWS_URL}/{service_name}/health?region={AWS_REGION}",
    }


# Typed comment functions with clear parameter requirements


def building_comment(show: Show) -> str:
    """Create building start comment

    Args:
        show: Show object with SHA and other metadata
    """
    links = _create_header_links(show.sha)
    return f"🎪 {links['showtime_link']} is building environment on {links['gha_link']} for {links['commit_link']}"


def start_comment(show: Show) -> str:
    """Create environment start comment (DEPRECATED - use building_comment)

    Args:
        show: Show object with SHA and other metadata
    """
    links = _create_header_links(show.sha)
    return f"🎪 {links['showtime_link']} is building environment on {links['gha_link']} for {links['commit_link']}"


def success_comment(
    show: Show, feature_count: Optional[int] = None, ttl: Optional[str] = None
) -> str:
    """Environment success comment

    Args:
        show: Show object with SHA, IP, etc.
        feature_count: Number of enabled feature flags (optional)
        ttl: Override TTL display (PR-level TTL takes precedence)
    """
    links = _create_header_links(show.sha)
    header = f"🎪 {links['showtime_link']} deployed environment on {links['gha_link']} for {links['commit_link']}"

    effective_ttl = ttl or show.ttl
    bullets = [
        f"**Environment:** http://{show.ip}:8080 (admin/admin)",
        f"**Lifetime:** {effective_ttl} auto-cleanup",
    ]

    if feature_count:
        bullets.insert(-1, f"**Features:** {feature_count} enabled")

    bullets.append("**Updates:** New commits create fresh environments automatically")

    return _format_comment(header, bullets)


def failure_comment(show: Show, error: str) -> str:
    """Environment failure comment

    Args:
        show: Show object with SHA and metadata
        error: Error message describing what went wrong
    """
    links = _create_header_links(show.sha)
    header = f"🎪 {links['showtime_link']} failed building environment on {links['gha_link']} for {links['commit_link']}"

    bullets = [
        f"**Error:** {error}",
        "**Action:** Check logs and try again with `🎪 ⚡ showtime-trigger-start`",
    ]

    return _format_comment(header, bullets)


def cleanup_comment(show: Show) -> str:
    """Environment cleanup comment

    Args:
        show: Show object with SHA and metadata
    """
    links = _create_header_links(show.sha)
    header = f"🎪 {links['showtime_link']} cleaned up environment on {links['gha_link']} for {links['commit_link']}"

    bullets = [
        "**Resources:** ECS service and ECR image deleted",
        "**Cost:** No further charges",
        "**Action:** Add `🎪 ⚡ showtime-trigger-start` to create new environment",
    ]

    return _format_comment(header, bullets)


def rolling_start_comment(current_show: Show, new_sha: str) -> str:
    """Rolling update start comment

    Args:
        current_show: Current Show object with SHA and IP
        new_sha: New environment SHA (full SHA, will be truncated)
    """
    from .show import short_sha

    links = _create_header_links(new_sha)
    header = f"🎪 {links['showtime_link']} is updating {current_show.short_sha}→{short_sha(new_sha)} on {links['gha_link']} for {links['commit_link']}"

    bullets = [
        f"**Current:** http://{current_show.ip}:8080 (still active)",
        "**Update:** Zero-downtime blue-green deployment",
    ]

    return _format_comment(header, bullets)


def rolling_success_comment(
    old_show: Show, new_show: Show, ttl: Optional[str] = None
) -> str:
    """Rolling update success comment

    Args:
        old_show: Previous Show object
        new_show: New Show object with updated IP, SHA
        ttl: Override TTL display (PR-level TTL takes precedence)
    """
    links = _create_header_links(new_show.sha)
    header = f"🎪 {links['showtime_link']} updated environment {old_show.short_sha}→{new_show.short_sha} on {links['gha_link']} for {links['commit_link']}"

    effective_ttl = ttl or new_show.ttl
    bullets = [
        f"**Environment:** http://{new_show.ip}:8080 (admin/admin)",
        f"**Lifetime:** {effective_ttl} auto-cleanup",
        "**Deployment:** Zero-downtime blue-green",
        "**Updates:** New commits create fresh environments automatically",
    ]

    return _format_comment(header, bullets)


def rolling_failure_comment(current_show: Show, new_sha: str, error: str) -> str:
    """Rolling update failure comment

    Args:
        current_show: Current Show object (still active)
        new_sha: Failed new environment SHA (full SHA, will be truncated)
        error: Error message describing what went wrong
    """
    from .show import short_sha

    links = _create_header_links(new_sha)
    header = f"🎪 {links['showtime_link']} failed updating to {short_sha(new_sha)} on {links['gha_link']} for {links['commit_link']}"

    bullets = [
        f"**Error:** {error}",
        f"**Current:** http://{current_show.ip}:8080 (still active)",
        "**Action:** Check logs and try again with `🎪 ⚡ showtime-trigger-start`",
    ]

    return _format_comment(header, bullets)
