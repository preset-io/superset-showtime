"""
🎪 PullRequest class - PR-level orchestration and state management

Handles atomic transactions, trigger processing, and environment orchestration.
"""

from dataclasses import dataclass
from typing import Any, List, Optional

from .aws import AWSInterface
from .github import GitHubInterface
from .show import Show, short_sha
from .sync_state import ActionNeeded, AuthStatus, BlockedReason, SyncState

# Lazy singletons to avoid import-time failures
_github = None
_aws = None


def get_github() -> GitHubInterface:
    global _github
    if _github is None:
        _github = GitHubInterface()
    return _github


def get_aws() -> AWSInterface:
    global _aws
    if _aws is None:
        _aws = AWSInterface()
    return _aws


# Use get_github() and get_aws() directly in methods


@dataclass
class SyncResult:
    """Result of a PullRequest.sync() operation"""

    success: bool
    action_taken: str  # create_environment, rolling_update, cleanup, no_action
    show: Optional[Show] = None
    error: Optional[str] = None


@dataclass
class AnalysisResult:
    """Result of a PullRequest.analyze() operation"""

    action_needed: str
    build_needed: bool
    sync_needed: bool
    target_sha: str


class PullRequest:
    """GitHub PR with its shows parsed from circus labels"""

    def __init__(self, pr_number: int, labels: List[str]):
        self.pr_number = pr_number
        self.labels = set(labels)  # Convert to set for O(1) operations
        self._shows = self._parse_shows_from_labels()

    @property
    def shows(self) -> List[Show]:
        """All shows found in labels"""
        return self._shows

    @property
    def current_show(self) -> Optional[Show]:
        """The currently active show (from 🎯 label)"""
        # Find the SHA that's marked as active (🎯)
        active_sha = None
        for label in self.labels:
            if label.startswith("🎪 🎯 "):
                active_sha = label.split(" ")[2]
                break

        if not active_sha:
            return None

        # Find the show with that SHA
        for show in self.shows:
            if show.sha == active_sha:
                return show

        return None

    @property
    def building_show(self) -> Optional[Show]:
        """The currently building show (from building/deploying status)"""
        for show in self.shows:
            if show.status in ["building", "deploying"]:
                return show
        return None

    @property
    def circus_labels(self) -> List[str]:
        """All circus tent emoji labels"""
        return [label for label in self.labels if label.startswith("🎪")]

    @property
    def has_shows(self) -> bool:
        """Check if PR has any active shows"""
        return len(self.shows) > 0

    def get_show_by_sha(self, sha: str) -> Optional[Show]:
        """Get show by SHA"""
        for show in self.shows:
            if show.sha == sha:
                return show
        return None

    def get_pr_ttl_hours(self) -> Optional[int]:
        """Get PR-level TTL override from labels.

        Looks for reusable PR-level TTL labels like "🎪 ⌛ 1w".
        Returns None if no TTL override is set or if TTL is "close".

        Returns:
            Number of hours, or None if no override / "close" TTL
        """
        from .date_utils import ttl_to_hours

        for label in self.labels:
            # Match PR-level TTL labels: "🎪 ⌛ {ttl}"
            if label.startswith("🎪 ⌛ "):
                ttl_value = label.replace("🎪 ⌛ ", "").strip()
                return ttl_to_hours(ttl_value)

        return None

    def _get_effective_ttl_display(self) -> str:
        """Get effective TTL for display purposes.

        Returns the PR-level TTL label value if set, otherwise the default.
        """
        from .constants import DEFAULT_TTL

        for label in self.labels:
            if label.startswith("🎪 ⌛ "):
                return label.replace("🎪 ⌛ ", "").strip()

        return DEFAULT_TTL

    def _parse_shows_from_labels(self) -> List[Show]:
        """Parse all shows from circus tent labels"""
        # Find all unique SHAs from circus labels
        shas = set()
        for label in self.labels:
            if not label.startswith("🎪"):
                continue
            parts = label.split(" ")
            if len(parts) >= 3 and len(parts[1]) == 7:  # SHA is 7 chars
                shas.add(parts[1])

        # Create Show objects for each SHA
        shows = []
        for sha in shas:
            show = Show.from_circus_labels(self.pr_number, list(self.labels), sha)
            if show:
                shows.append(show)

        return shows

    @classmethod
    def from_id(cls, pr_number: int) -> "PullRequest":
        """Load PR with current labels from GitHub"""
        labels = get_github().get_labels(pr_number)
        return cls(pr_number, labels)

    def refresh_labels(self) -> None:
        """Refresh labels from GitHub and reparse shows"""
        self.labels = set(get_github().get_labels(self.pr_number))
        self._shows = self._parse_shows_from_labels()

    def add_label(self, label: str) -> None:
        """Add label with logging and optimistic state update"""
        print(f"🏷️ Added: {label}")
        get_github().add_label(self.pr_number, label)
        self.labels.add(label)

    def remove_label(self, label: str) -> None:
        """Remove label with logging and optimistic state update"""
        print(f"🗑️ Removed: {label}")
        get_github().remove_label(self.pr_number, label)
        self.labels.discard(label)  # Safe - won't raise if not present

    def remove_sha_labels(self, sha: str) -> None:
        """Remove all labels for a specific SHA"""
        sha_short = sha[:7]
        labels_to_remove = [
            label for label in self.labels if label.startswith("🎪") and sha_short in label
        ]
        if labels_to_remove:
            print(f"🗑️ Removing SHA {sha_short} labels: {labels_to_remove}")
            for label in labels_to_remove:
                self.remove_label(label)

    def remove_showtime_labels(self) -> None:
        """Remove ALL circus tent labels"""
        circus_labels = [label for label in self.labels if label.startswith("🎪 ")]
        if circus_labels:
            print(f"🎪 Removing all showtime labels: {circus_labels}")
            for label in circus_labels:
                self.remove_label(label)

    def set_show_status(self, show: Show, new_status: str) -> None:
        """Atomically update show status with thorough label cleanup"""
        show.status = new_status

        # 1. Refresh labels to get current GitHub state
        self.refresh_labels()

        # 2. Remove ALL existing status labels for this SHA (not just the "expected" one)
        status_labels_to_remove = [
            label for label in self.labels if label.startswith(f"🎪 {show.sha} 🚦 ")
        ]

        for label in status_labels_to_remove:
            self.remove_label(label)

        # 3. Add the new status label
        new_status_label = f"🎪 {show.sha} 🚦 {new_status}"
        self.add_label(new_status_label)

    def set_active_show(self, show: Show) -> None:
        """Atomically set this show as the active environment"""
        from .emojis import CIRCUS_PREFIX, MEANING_TO_EMOJI

        # 1. Refresh to get current state
        self.refresh_labels()

        # 2. Remove ALL existing active pointers (ensure only one)
        active_emoji = MEANING_TO_EMOJI["active"]  # Gets 🎯
        active_prefix = f"{CIRCUS_PREFIX} {active_emoji} "  # "🎪 🎯 "
        active_pointers = [label for label in self.labels if label.startswith(active_prefix)]

        for pointer in active_pointers:
            self.remove_label(pointer)

        # 3. Set this show as the new active one
        active_pointer = f"{active_prefix}{show.sha}"  # "🎪 🎯 abc123f"
        self.add_label(active_pointer)

    def _check_authorization(self) -> tuple[bool, dict]:
        """Check if current GitHub actor is authorized for operations

        Returns:
            tuple: (is_authorized, debug_info_dict)
        """
        import httpx

        # Get actor info using centralized function
        actor_info = GitHubInterface.get_actor_debug_info()
        debug_info = {**actor_info, "permission": "unknown", "auth_status": "unknown"}

        # Only check in GitHub Actions context
        if not debug_info["is_github_actions"]:
            debug_info["auth_status"] = "skipped_not_actions"
            return True, debug_info

        actor = debug_info["actor"]
        if not actor or actor == "unknown":
            debug_info["auth_status"] = "allowed_no_actor"
            return True, debug_info

        try:
            # Use existing GitHubInterface for consistency
            github = get_github()

            # Check collaborator permissions
            perm_url = f"{github.base_url}/repos/{github.org}/{github.repo}/collaborators/{actor}/permission"

            with httpx.Client() as client:
                response = client.get(perm_url, headers=github.headers)
                if response.status_code == 404:
                    debug_info["permission"] = "not_collaborator"
                    debug_info["auth_status"] = "denied_404"
                    return False, debug_info

                response.raise_for_status()

                data = response.json()
                permission = data.get("permission", "none")
                debug_info["permission"] = permission

                # Allow write and admin permissions only
                authorized = permission in ["write", "admin"]

                if not authorized:
                    debug_info["auth_status"] = "denied_insufficient_perms"
                    print(f"🚨 Unauthorized actor {actor} (permission: {permission})")
                    # Set blocked label for security
                    self.add_label("🎪 🔒 showtime-blocked")
                else:
                    debug_info["auth_status"] = "authorized"

                return authorized, debug_info

        except Exception as e:
            debug_info["auth_status"] = f"error_{type(e).__name__}"
            debug_info["error"] = str(e)
            print(f"⚠️ Authorization check failed: {e}")
            return True, debug_info  # Fail open for non-security operations

    def analyze(self, target_sha: str, pr_state: str = "open") -> SyncState:
        """Analyze what actions are needed with comprehensive debugging info

        Args:
            target_sha: Target commit SHA to analyze
            pr_state: PR state (open/closed)

        Returns:
            SyncState with complete analysis and debug info
        """
        import os

        # Handle closed PRs
        if pr_state == "closed":
            return SyncState(
                action_needed=ActionNeeded.DESTROY_ENVIRONMENT,
                build_needed=False,
                sync_needed=True,
                target_sha=target_sha,
                github_actor=GitHubInterface.get_current_actor(),
                is_github_actions=os.getenv("GITHUB_ACTIONS") == "true",
                permission_level="cleanup",
                auth_status=AuthStatus.SKIPPED_NOT_ACTIONS,
                action_reason="pr_closed",
            )

        # Get fresh labels
        self.refresh_labels()

        # Initialize state tracking
        target_sha_short = target_sha[:7]
        target_show = self.get_show_by_sha(target_sha_short)
        trigger_labels = [label for label in self.labels if "showtime-trigger-" in label]

        # Check for existing blocked label
        blocked_reason = BlockedReason.NOT_BLOCKED
        if "🎪 🔒 showtime-blocked" in self.labels:
            blocked_reason = BlockedReason.EXISTING_BLOCKED_LABEL

        # Check authorization
        is_authorized, auth_debug = self._check_authorization()
        if not is_authorized and blocked_reason == BlockedReason.NOT_BLOCKED:
            blocked_reason = BlockedReason.AUTHORIZATION_FAILED

        # Determine action needed
        action_needed_str = (
            "blocked"
            if blocked_reason != BlockedReason.NOT_BLOCKED
            else self._evaluate_action_logic(target_sha_short, target_show, trigger_labels)
        )

        # Map string to enum
        action_map = {
            "no_action": ActionNeeded.NO_ACTION,
            "create_environment": ActionNeeded.CREATE_ENVIRONMENT,
            "rolling_update": ActionNeeded.ROLLING_UPDATE,
            "auto_sync": ActionNeeded.AUTO_SYNC,
            "destroy_environment": ActionNeeded.DESTROY_ENVIRONMENT,
            "blocked": ActionNeeded.BLOCKED,
        }
        action_needed = action_map.get(action_needed_str, ActionNeeded.NO_ACTION)

        # Build sync state
        return SyncState(
            action_needed=action_needed,
            build_needed=action_needed
            in [
                ActionNeeded.CREATE_ENVIRONMENT,
                ActionNeeded.ROLLING_UPDATE,
                ActionNeeded.AUTO_SYNC,
            ],
            sync_needed=action_needed not in [ActionNeeded.NO_ACTION, ActionNeeded.BLOCKED],
            target_sha=target_sha,
            github_actor=auth_debug.get("actor", "unknown"),
            is_github_actions=auth_debug.get("is_github_actions", False),
            permission_level=auth_debug.get("permission", "unknown"),
            auth_status=self._parse_auth_status(auth_debug.get("auth_status", "unknown")),
            blocked_reason=blocked_reason,
            trigger_labels=trigger_labels,
            target_show_status=target_show.status if target_show else None,
            has_previous_shows=len(self.shows) > 0,
            action_reason=self._get_action_reason(action_needed_str, target_show, trigger_labels),
            auth_error=auth_debug.get("error"),
        )

    def _evaluate_action_logic(
        self, target_sha_short: str, target_show: Optional[Show], trigger_labels: List[str]
    ) -> str:
        """Pure logic for evaluating what action is needed (no side effects, for testability)"""
        if trigger_labels:
            for trigger in trigger_labels:
                if "showtime-trigger-start" in trigger:
                    if not target_show or target_show.status == "failed":
                        return "create_environment"  # New SHA or failed SHA
                    elif target_show.status in ["building", "built", "deploying"]:
                        return "no_action"  # Target SHA already in progress
                    elif target_show.status == "running":
                        return "create_environment"  # Force rebuild with trigger
                    else:
                        return "create_environment"  # Default for unknown states
                elif "showtime-trigger-stop" in trigger:
                    return "destroy_environment"

        # No explicit triggers - only auto-create if there's ANY previous environment
        if not target_show:
            # Target SHA doesn't exist - only create if there's any previous environment
            if self.shows:  # Any previous environment exists
                return "create_environment"
            else:
                # No previous environments - don't auto-create without explicit trigger
                return "no_action"
        elif target_show.status == "failed":
            # Target SHA failed - rebuild it
            return "create_environment"
        elif target_show.status in ["building", "built", "deploying"]:
            # Target SHA in progress - wait
            return "no_action"
        elif target_show.status == "running":
            # Target SHA already running - no action needed
            return "no_action"

        return "no_action"

    def _get_action_reason(
        self, action_needed: str, target_show: Optional[Show], trigger_labels: List[str]
    ) -> str:
        """Get human-readable reason for the action"""
        if action_needed == "blocked":
            return "operation_blocked"
        elif trigger_labels:
            if any("trigger-start" in label for label in trigger_labels):
                if not target_show:
                    return "explicit_start_new_sha"
                elif target_show.status == "failed":
                    return "explicit_start_failed_sha"
                elif target_show.status == "running":
                    return "explicit_start_force_rebuild"
                else:
                    return "explicit_start_trigger"
            elif any("trigger-stop" in label for label in trigger_labels):
                return "explicit_stop_trigger"
        elif action_needed == "create_environment":
            if not target_show:
                return "auto_sync_new_commit"
            elif target_show.status == "failed":
                return "auto_rebuild_failed"
            else:
                return "create_environment"
        elif action_needed == "no_action":
            if target_show and target_show.status == "running":
                return "already_running"
            elif target_show and target_show.status in ["building", "deploying"]:
                return "in_progress"
            else:
                return "no_previous_environments"
        return action_needed

    def _parse_auth_status(self, auth_status_str: str) -> AuthStatus:
        """Parse auth status string to enum, handling errors gracefully"""
        try:
            return AuthStatus(auth_status_str)
        except ValueError:
            # Handle error cases that include exception type (e.g., "error_UnsupportedProtocol")
            if auth_status_str.startswith("error_"):
                return AuthStatus.ERROR
            return AuthStatus.ERROR

    def sync(
        self,
        target_sha: str,
        dry_run_github: bool = False,
        dry_run_aws: bool = False,
        dry_run_docker: bool = False,
    ) -> SyncResult:
        """Sync PR to desired state with atomic transaction management

        Args:
            target_sha: Target commit SHA to sync to
            github: GitHub interface for label operations
            aws: AWS interface for environment operations
            dry_run_github: Skip GitHub operations if True
            dry_run_aws: Skip AWS operations if True
            dry_run_docker: Skip Docker operations if True

        Returns:
            SyncResult with success status and details

        Raises:
            Exception: On unrecoverable errors (caller should handle)
        """

        # 1. Determine what action is needed
        action_needed = self._determine_action(target_sha)

        # 2. Check for blocked state (fast bailout)
        if action_needed == "blocked":
            return SyncResult(
                success=False,
                action_taken="blocked",
                error="🔒 Showtime operations are blocked for this PR. Remove '🎪 🔒 showtime-blocked' label to re-enable.",
            )

        # 3. Atomic claim for environment changes (PR-level lock)
        if action_needed in [
            "create_environment",
            "rolling_update",
            "auto_sync",
            "destroy_environment",
        ]:
            print(f"🔒 Claiming environment for {action_needed}...")
            if not self._atomic_claim(target_sha, action_needed, dry_run_github):
                print("❌ Claim failed - another job is active")
                return SyncResult(
                    success=False,
                    action_taken="claim_failed",
                    error="Another job is already active",
                )
            print("✅ Environment claimed successfully")

        try:
            # 3. Execute action with error handling
            if action_needed == "create_environment":
                show = self._create_new_show(target_sha)
                print(f"🏗️ Creating environment {show.sha}...")
                self._post_building_comment(show, dry_run_github)

                # Phase 1: Docker build
                print("🐳 Building Docker image...")
                show.build_docker(dry_run_docker)
                print("✅ Docker build completed")

                # Phase 2: AWS deployment
                print("☁️ Deploying to AWS ECS...")
                self.set_show_status(show, "deploying")
                show.deploy_aws(dry_run_aws)
                self.set_show_status(show, "running")
                self.set_active_show(show)
                print(f"✅ Deployment completed - environment running at {show.ip}:8080")
                self._update_show_labels(show, dry_run_github)

                # Blue-green cleanup: stop all other environments for this PR
                cleaned_count = self.stop_previous_environments(
                    show.sha, dry_run_github, dry_run_aws
                )

                # Show AWS console URLs for monitoring
                self._show_service_urls(show)

                self._post_success_comment(show, dry_run_github)
                return SyncResult(success=True, action_taken="create_environment", show=show)

            elif action_needed in ["rolling_update", "auto_sync"]:
                old_show = self.current_show
                if not old_show:
                    return SyncResult(
                        success=False,
                        action_taken="no_current_show",
                        error="No current show for rolling update",
                    )
                new_show = self._create_new_show(target_sha)
                print(f"🔄 Rolling update: {old_show.sha} → {new_show.sha}")
                self._post_rolling_start_comment(old_show, new_show, dry_run_github)

                # Phase 1: Docker build
                print("🐳 Building updated Docker image...")
                new_show.build_docker(dry_run_docker)
                print("✅ Docker build completed")

                # Phase 2: Blue-green deployment
                print("☁️ Deploying updated environment...")
                self.set_show_status(new_show, "deploying")
                new_show.deploy_aws(dry_run_aws)
                self.set_show_status(new_show, "running")
                self.set_active_show(new_show)
                print(f"✅ Rolling update completed - new environment at {new_show.ip}:8080")
                self._update_show_labels(new_show, dry_run_github)

                # Blue-green cleanup: stop all other environments for this PR
                cleaned_count = self.stop_previous_environments(
                    new_show.sha, dry_run_github, dry_run_aws
                )

                # Show AWS console URLs for monitoring
                self._show_service_urls(new_show)

                self._post_rolling_success_comment(old_show, new_show, dry_run_github)
                return SyncResult(success=True, action_taken=action_needed, show=new_show)

            elif action_needed == "destroy_environment":
                # Stop the current environment if it exists
                if self.current_show:
                    print(f"🗑️ Destroying environment {self.current_show.sha}...")
                    success = self.current_show.stop(
                        dry_run_github=dry_run_github, dry_run_aws=dry_run_aws
                    )
                    if success:
                        print("☁️ AWS resources deleted")
                    else:
                        print("⚠️ AWS resource deletion may have failed")
                    self._post_cleanup_comment(self.current_show, dry_run_github)
                else:
                    print("🗑️ No current environment to destroy")

                # ALWAYS remove all circus labels for stop trigger, regardless of current_show
                if not dry_run_github:
                    self.remove_showtime_labels()
                    print("🏷️ GitHub labels cleaned up")
                print("✅ Environment destroyed")
                return SyncResult(success=True, action_taken="destroy_environment")

            else:
                return SyncResult(success=True, action_taken="no_action")

        except Exception as e:
            # Transaction failed - set failed state and update labels
            if "show" in locals():
                show.status = "failed"
                self._update_show_labels(show, dry_run_github)
                # TODO: Post failure comment
            return SyncResult(success=False, action_taken="failed", error=str(e))

    def start_environment(self, sha: Optional[str] = None, **kwargs: Any) -> SyncResult:
        """Start a new environment (CLI start command logic)"""
        target_sha = sha or get_github().get_latest_commit_sha(self.pr_number)
        return self.sync(target_sha, **kwargs)

    def stop_environment(self, **kwargs: Any) -> SyncResult:
        """Stop current environment (CLI stop command logic)"""
        try:
            # Stop the current environment if it exists
            if self.current_show:
                success = self.current_show.stop(**kwargs)
                if success:
                    print("☁️ AWS resources deleted")
                else:
                    print("⚠️ AWS resource deletion may have failed")
                    return SyncResult(success=False, action_taken="stop_environment")
            else:
                print("🗑️ No current environment to destroy")

            # ALWAYS remove all circus labels for stop command, regardless of current_show
            if not kwargs.get("dry_run_github", False):
                self.remove_showtime_labels()
                print("🏷️ GitHub labels cleaned up")
            return SyncResult(success=True, action_taken="stopped")
        except Exception as e:
            return SyncResult(success=False, action_taken="stop_failed", error=str(e))

    def get_status(self) -> dict:
        """Get current status (CLI status command logic)"""
        if not self.current_show:
            return {"status": "no_environment", "show": None}

        # Get effective TTL: PR-level label > show default
        effective_ttl = self._get_effective_ttl_display()

        return {
            "status": "active",
            "show": {
                "sha": self.current_show.sha,
                "status": self.current_show.status,
                "ip": self.current_show.ip,
                "ttl": effective_ttl,
                "requested_by": self.current_show.requested_by,
                "created_at": self.current_show.created_at,
                "aws_service_name": self.current_show.aws_service_name,
            },
        }

    @classmethod
    def list_all_environments(cls) -> List[dict]:
        """List all environments across all PRs (CLI list command logic)"""
        # Find all PRs with circus tent labels
        pr_numbers = get_github().find_prs_with_shows()

        all_environments = []
        github_service_names = set()  # Track services we found via GitHub

        for pr_number in pr_numbers:
            pr = cls.from_id(pr_number)
            # Show ALL environments, not just current_show
            for show in pr.shows:
                # Track this service name for later
                github_service_names.add(show.ecs_service_name)

                # Determine show type based on pointer presence
                show_type = "orphaned"  # Default

                # Check for active pointer
                if any(label == f"🎪 🎯 {show.sha}" for label in pr.labels):
                    show_type = "active"
                # Check for building pointer
                elif show.status in ["building", "deploying"]:
                    show_type = "building"
                # No pointer = orphaned

                # Get effective TTL from PR-level label
                effective_ttl = pr._get_effective_ttl_display()

                environment_data = {
                    "pr_number": pr_number,
                    "status": "active",  # Keep for compatibility
                    "show": {
                        "sha": show.sha,
                        "status": show.status,
                        "ip": show.ip,
                        "ttl": effective_ttl,
                        "requested_by": show.requested_by,
                        "created_at": show.created_at,
                        "age": show.age_display(),  # Add age display
                        "aws_service_name": show.aws_service_name,
                        "show_type": show_type,  # New field for display
                        "is_legacy": False,  # Regular environment
                    },
                }
                all_environments.append(environment_data)

        # TODO: Remove after legacy cleanup - Find AWS-only services (legacy pr-XXXXX-service format)
        try:
            from .aws import get_aws
            from .service_name import ServiceName

            aws = get_aws()
            aws_services = aws.list_circus_environments()

            for aws_service in aws_services:
                service_name_str = aws_service.get("service_name", "")

                # Skip if we already have this from GitHub
                if service_name_str in github_service_names:
                    continue

                # Parse the service name to get PR number
                try:
                    svc = ServiceName.from_service_name(service_name_str)

                    # Legacy services have no SHA
                    is_legacy = svc.sha is None

                    # Add as a legacy/orphaned environment
                    environment_data = {
                        "pr_number": svc.pr_number,
                        "status": "active",  # Keep for compatibility
                        "show": {
                            "sha": svc.sha or "-",  # Show dash for missing SHA
                            "status": aws_service["status"].lower()
                            if aws_service.get("status")
                            else "running",
                            "ip": aws_service.get("ip"),
                            "ttl": None,  # Legacy environments have no TTL labels
                            "requested_by": "-",  # Unknown user for legacy
                            "created_at": None,  # Will show as "-" in display
                            "age": "-",  # Unknown age
                            "aws_service_name": svc.base_name,  # pr-XXXXX or pr-XXXXX-sha format
                            "show_type": "legacy"
                            if is_legacy
                            else "orphaned",  # Mark as legacy type
                            "is_legacy": is_legacy,  # Flag for display formatting
                        },
                    }
                    all_environments.append(environment_data)

                except ValueError:
                    # Skip services that don't match our pattern
                    continue

        except Exception:
            # If AWS lookup fails, just show GitHub-based environments
            pass

        return all_environments

    def _determine_action(self, target_sha: str) -> str:
        """Determine what sync action is needed (includes all checks and refreshes labels)"""
        # CRITICAL: Get fresh labels before any decisions
        self.refresh_labels()

        # Check for blocked state first (fast bailout)
        if "🎪 🔒 showtime-blocked" in self.labels:
            return "blocked"

        # Check authorization (security layer)
        is_authorized, _ = self._check_authorization()
        if not is_authorized:
            return "blocked"

        target_sha_short = target_sha[:7]  # Ensure we're working with short SHA

        # Get the specific show for the target SHA
        target_show = self.get_show_by_sha(target_sha_short)

        # Check for explicit trigger labels
        trigger_labels = [label for label in self.labels if "showtime-trigger-" in label]

        if trigger_labels:
            for trigger in trigger_labels:
                if "showtime-trigger-start" in trigger:
                    if not target_show or target_show.status == "failed":
                        return "create_environment"  # New SHA or failed SHA
                    elif target_show.status in ["building", "built", "deploying"]:
                        return "no_action"  # Target SHA already in progress
                    elif target_show.status == "running":
                        return "create_environment"  # Force rebuild with trigger
                    else:
                        return "create_environment"  # Default for unknown states
                elif "showtime-trigger-stop" in trigger:
                    return "destroy_environment"

        # No explicit triggers - only auto-create if there's ANY previous environment
        if not target_show:
            # Target SHA doesn't exist - only create if there's any previous environment
            if self.shows:  # Any previous environment exists
                return "create_environment"
            else:
                # No previous environments - don't auto-create without explicit trigger
                return "no_action"
        elif target_show.status == "failed":
            # Target SHA failed - rebuild it
            return "create_environment"
        elif target_show.status in ["building", "built", "deploying"]:
            # Target SHA in progress - wait
            return "no_action"
        elif target_show.status == "running":
            # Target SHA already running - no action needed
            return "no_action"

        return "no_action"

    def _atomic_claim(self, target_sha: str, action: str, dry_run: bool = False) -> bool:
        """Atomically claim this PR for the current job based on target SHA state"""
        # CRITICAL: Get fresh labels before any decisions
        self.refresh_labels()

        target_sha_short = target_sha[:7]
        target_show = self.get_show_by_sha(target_sha_short)

        # 1. Validate current state allows this action for target SHA
        if action in ["create_environment", "rolling_update", "auto_sync"]:
            if target_show and target_show.status in [
                "building",
                "built",
                "deploying",
            ]:
                return False  # Target SHA already in progress - ONLY conflict case returns

        if dry_run:
            print(f"🎪 [DRY-RUN] Would atomically claim PR for {action}")
            return True

        # 2. Remove trigger labels (atomic operation)
        trigger_labels = [label for label in self.labels if "showtime-trigger-" in label]
        if trigger_labels:
            print(f"🏷️ Removing trigger labels: {trigger_labels}")
            for trigger_label in trigger_labels:
                self.remove_label(trigger_label)
        else:
            print("🏷️ No trigger labels to remove")

        # 3. Set building state immediately (claim the PR)
        if action in ["create_environment", "rolling_update", "auto_sync"]:
            building_show = self._create_new_show(target_sha)
            building_show.status = "building"

            # Update labels to reflect building state - only remove for this SHA
            print(f"🏷️ Removing labels for SHA {target_sha[:7]}...")
            self.remove_sha_labels(target_sha)

            new_labels = building_show.to_circus_labels()
            print(f"🏷️ Creating new labels: {new_labels}")
            for label in new_labels:
                try:
                    self.add_label(label)
                except Exception as e:
                    print(f"  ❌ Failed to add {label}: {e}")
                    raise

            # Auto-create PR-level TTL label if not present
            self._ensure_ttl_label()

        return True

    def _ensure_ttl_label(self) -> None:
        """Ensure PR has a TTL label, adding the default if not present."""
        from .constants import DEFAULT_TTL

        # Check if any PR-level TTL label already exists
        has_ttl_label = any(label.startswith("🎪 ⌛ ") for label in self.labels)

        if not has_ttl_label:
            default_ttl_label = f"🎪 ⌛ {DEFAULT_TTL}"
            print(f"🏷️ Auto-creating TTL label: {default_ttl_label}")
            self.add_label(default_ttl_label)

    def _create_new_show(self, target_sha: str) -> Show:
        """Create a new Show object for the target SHA"""
        from .date_utils import format_utc_now

        return Show(
            pr_number=self.pr_number,
            sha=short_sha(target_sha),
            status="building",
            created_at=format_utc_now(),
            requested_by=GitHubInterface.get_current_actor(),
        )

    def _post_building_comment(self, show: Show, dry_run: bool = False) -> None:
        """Post building comment for new environment"""
        from .github_messages import building_comment

        if not dry_run:
            comment = building_comment(show)
            get_github().post_comment(self.pr_number, comment)

    def _post_success_comment(self, show: Show, dry_run: bool = False) -> None:
        """Post success comment for completed environment"""
        from .github_messages import success_comment

        if not dry_run:
            effective_ttl = self._get_effective_ttl_display()
            comment = success_comment(show, ttl=effective_ttl)
            get_github().post_comment(self.pr_number, comment)

    def _post_rolling_start_comment(
        self, old_show: Show, new_show: Show, dry_run: bool = False
    ) -> None:
        """Post rolling update start comment"""
        from .github_messages import rolling_start_comment

        if not dry_run:
            full_sha = new_show.sha + "0" * (40 - len(new_show.sha))
            comment = rolling_start_comment(old_show, full_sha)
            get_github().post_comment(self.pr_number, comment)

    def _post_rolling_success_comment(
        self, old_show: Show, new_show: Show, dry_run: bool = False
    ) -> None:
        """Post rolling update success comment"""
        from .github_messages import rolling_success_comment

        if not dry_run:
            comment = rolling_success_comment(old_show, new_show)
            get_github().post_comment(self.pr_number, comment)

    def _post_cleanup_comment(self, show: Show, dry_run: bool = False) -> None:
        """Post cleanup completion comment"""
        from .github_messages import cleanup_comment

        if not dry_run:
            comment = cleanup_comment(show)
            get_github().post_comment(self.pr_number, comment)

    def stop_if_expired(self, max_age_hours: int, dry_run: bool = False) -> bool:
        """Stop environment if it's expired based on age

        Args:
            max_age_hours: Maximum age in hours before expiration
            dry_run: If True, just check don't actually stop

        Returns:
            True if environment was expired (and stopped), False otherwise
        """
        if not self.current_show:
            return False

        # Use Show's expiration logic
        if self.current_show.is_expired(max_age_hours):
            if dry_run:
                print(f"🎪 [DRY-RUN] Would stop expired environment: PR #{self.pr_number}")
                return True

            print(f"🧹 Stopping expired environment: PR #{self.pr_number}")
            result = self.stop_environment(dry_run_github=False, dry_run_aws=False)
            return result.success

        return False  # Not expired

    def cleanup_orphaned_shows(self, max_age_hours: int, dry_run: bool = False) -> int:
        """Clean up orphaned shows (environments without pointer labels)

        Args:
            max_age_hours: Maximum age in hours before considering orphaned environment for cleanup
            dry_run: If True, just check don't actually stop

        Returns:
            Number of orphaned environments cleaned up
        """
        cleaned_count = 0

        # Find orphaned shows (shows without active or building pointers)
        orphaned_shows = []
        for show in self.shows:
            has_pointer = any(
                label in self.labels for label in [f"🎪 🎯 {show.sha}", f"🎪 🏗️ {show.sha}"]
            )
            if not has_pointer and show.is_expired(max_age_hours):
                orphaned_shows.append(show)

        # Clean up each orphaned show
        for show in orphaned_shows:
            if dry_run:
                print(
                    f"🎪 [DRY-RUN] Would clean orphaned environment: PR #{self.pr_number} SHA {show.sha}"
                )
                cleaned_count += 1
            else:
                print(f"🧹 Cleaning orphaned environment: PR #{self.pr_number} SHA {show.sha}")
                # Stop the specific show (AWS resources)
                success = show.stop(dry_run_github=False, dry_run_aws=False)
                if success:
                    # Also clean up GitHub labels for this specific show
                    self.remove_sha_labels(show.sha)
                    cleaned_count += 1
                    print(f"✅ Cleaned orphaned environment: {show.sha}")
                else:
                    print(f"⚠️ Failed to clean orphaned environment: {show.sha}")

        return cleaned_count

    @classmethod
    def find_all_with_environments(cls) -> List[int]:
        """Find all PR numbers that have active environments"""
        return get_github().find_prs_with_shows()

    def _update_show_labels(self, show: Show, dry_run: bool = False) -> None:
        """Update GitHub labels to reflect show state with proper status replacement"""
        if dry_run:
            return

        # Refresh labels to get current state (atomic claim may have changed them)
        self.refresh_labels()

        # First, remove any existing status labels for this SHA to ensure clean transitions
        sha_status_labels = [
            label for label in self.labels if label.startswith(f"🎪 {show.sha} 🚦 ")
        ]
        for old_status_label in sha_status_labels:
            self.remove_label(old_status_label)

        # For running environments, ensure only ONE active pointer exists
        if show.status == "running":
            # Remove ALL existing active pointers EXCEPT for this SHA's pointer
            existing_active_pointers = [
                label
                for label in self.labels
                if label.startswith("🎪 🎯 ") and label != f"🎪 🎯 {show.sha}"
            ]
            for old_pointer in existing_active_pointers:
                print(f"🎯 Removing old active pointer: {old_pointer}")
                self.remove_label(old_pointer)

            # CRITICAL: Refresh after removals before differential calculation
            if existing_active_pointers:
                print("🔄 Refreshing labels after pointer cleanup...")
                self.refresh_labels()

        # Now do normal differential updates - only for this SHA
        current_sha_labels = {
            label
            for label in self.labels
            if label.startswith("🎪")
            and (
                label.startswith(f"🎪 {show.sha} ")  # SHA-first format: 🎪 abc123f 📅 ...
                or label.startswith(f"🎪 🎯 {show.sha}")  # Pointer format: 🎪 🎯 abc123f
            )
        }
        desired_labels = set(show.to_circus_labels())

        # Remove the status labels we already cleaned up from the differential
        current_sha_labels = current_sha_labels - set(sha_status_labels)

        # Only add labels that don't exist
        labels_to_add = desired_labels - current_sha_labels
        for label in labels_to_add:
            self.add_label(label)

        # Only remove labels that shouldn't exist (excluding status labels already handled)
        labels_to_remove = current_sha_labels - desired_labels
        for label in labels_to_remove:
            self.remove_label(label)

        # Final refresh to update cache with all changes
        self.refresh_labels()

    def _show_service_urls(self, show: Show) -> None:
        """Show AWS console URLs for monitoring deployment"""
        from .github_messages import get_aws_console_urls

        urls = get_aws_console_urls(show.ecs_service_name)
        print("\n🎪 Monitor deployment progress:")
        print(f"📝 Logs: {urls['logs']}")
        print(f"📊 Service: {urls['service']}")
        print("")

    def stop_previous_environments(
        self, keep_sha: str, dry_run_github: bool = False, dry_run_aws: bool = False
    ) -> int:
        """Stop all environments except the specified SHA (blue-green cleanup)

        Args:
            keep_sha: SHA of environment to keep running
            dry_run_github: Skip GitHub label operations
            dry_run_aws: Skip AWS operations

        Returns:
            Number of environments stopped
        """
        # Note: Labels should be fresh from recent _update_show_labels() call
        stopped_count = 0

        for show in self.shows:
            if show.sha != keep_sha:
                print(f"🧹 Cleaning up old environment: {show.sha} ({show.status})")
                try:
                    show.stop(dry_run_github=dry_run_github, dry_run_aws=dry_run_aws)

                    # Remove ONLY existing labels for this old environment (not theoretical ones)
                    if not dry_run_github:
                        existing_labels = [
                            label
                            for label in self.labels
                            if label.startswith(f"🎪 {show.sha} ") or label == f"🎪 🎯 {show.sha}"
                        ]
                        print(f"🏷️ Removing existing labels for {show.sha}: {existing_labels}")
                        for label in existing_labels:
                            try:
                                self.remove_label(label)
                            except Exception as e:
                                print(f"⚠️ Failed to remove label {label}: {e}")

                    stopped_count += 1
                    print(f"✅ Stopped environment {show.sha}")

                except Exception as e:
                    print(f"❌ Failed to stop environment {show.sha}: {e}")

        if stopped_count > 0:
            print(f"🧹 Blue-green cleanup: stopped {stopped_count} old environments")
            # Refresh labels after cleanup
            if not dry_run_github:
                self.refresh_labels()
        else:
            print("ℹ️ No old environments to clean up")

        return stopped_count
