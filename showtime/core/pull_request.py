"""
🎪 PullRequest class - PR-level orchestration and state management

Handles atomic transactions, trigger processing, and environment orchestration.
"""

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

from .aws import AWSInterface
from .constants import SHOWTIME_COMMENT_MARKER
from .github import GitHubInterface
from .readiness import DEFAULT_STARTUP_TIMEOUT_SECONDS, validate_startup_timeout_seconds
from .runner_smoke import (
    DEFAULT_BUILD_TIMEOUT_SECONDS,
    DEFAULT_DIAGNOSTICS_DIR,
    DEFAULT_SMOKE_TIMEOUT_SECONDS,
    validate_diagnostics_directory,
    validate_positive_seconds,
)
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


VALID_FLAG_VALUES = {"true", "false", "1", "0", "yes", "no"}


def parse_feature_flags(description: Optional[str]) -> List[Dict[str, str]]:
    """Extract feature flags from PR description.

    Parses lines matching FEATURE_(name)=(value) and transforms them to
    ECS environment variable format: [{"name": "SUPERSET_FEATURE_X", "value": "True"}]

    Only accepts boolean-like values (true/false/1/0/yes/no). Invalid values are skipped.
    Values are normalized to "True"/"False" to match the format used by reconcile_feature_flags.
    """
    if not description:
        return []

    flags: List[Dict[str, str]] = []
    for match in re.finditer(r"\bFEATURE_(\w+)=(\w+)", description):
        name = f"SUPERSET_FEATURE_{match.group(1)}"
        value = match.group(2).lower()
        if value not in VALID_FLAG_VALUES:
            print(
                f"⚠️ Skipping feature flag {name}: invalid value "
                f"'{match.group(2)}' (expected true/false/1/0/yes/no)"
            )
            continue
        canonical = "True" if value in ("true", "1", "yes") else "False"
        flags.append({"name": name, "value": canonical})
    return flags


@dataclass
class CleanupResult:
    """Aggregate outcome for cleanup across one or more tracked shows."""

    success: bool
    attempted_shas: List[str] = field(default_factory=list)
    deleted_shas: List[str] = field(default_factory=list)
    pending_shas: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)


@dataclass
class PromotionResult:
    """Outcome of attaching a candidate pointer and retiring stale pointers."""

    success: bool
    pointer_attached: bool
    errors: List[str] = field(default_factory=list)


@dataclass
class SyncResult:
    """Result of a PullRequest.sync() operation"""

    success: bool
    action_taken: str  # create_environment, rolling_update, cleanup, no_action
    show: Optional[Show] = None
    error: Optional[str] = None
    cleanup_result: Optional[CleanupResult] = None


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
        """Return the deterministic best active show during pointer recovery."""
        active_shas = {
            label.split(" ")[2]
            for label in self.labels
            if label.startswith("🎪 🎯 ") and len(label.split(" ")) >= 3
        }
        candidates = [show for show in self.shows if show.sha in active_shas]
        if not candidates:
            return None
        return max(
            candidates,
            key=lambda show: (
                show.status == "running",
                show.created_datetime or datetime.min,
                show.sha,
            ),
        )

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

    def remove_sha_labels(self, sha: str, delete_definitions: bool = False) -> None:
        """Detach SHA-owned labels, preserving status until every detail is gone."""
        sha_short = sha[:7]
        labels_to_remove = [label for label in self.labels if label.startswith(f"🎪 {sha_short} ")]
        if labels_to_remove:
            print(f"🗑️ Removing SHA {sha_short} labels: {labels_to_remove}")
            labels_to_remove.sort(key=lambda label: " 🚦 " in label)
            for label in labels_to_remove:
                self.remove_label(label)

    def remove_showtime_labels(self, delete_definitions: bool = False) -> None:
        """Remove all PR label attachments without pruning shared definitions.

        Args:
            delete_definitions: Retained for call compatibility. Repository definitions
                are pruned only by the attachment-count-qualified global cleanup.
        """
        circus_labels = [label for label in self.labels if label.startswith("🎪 ")]
        if circus_labels:
            print(f"🎪 Removing all showtime labels: {circus_labels}")
            for label in circus_labels:
                self.remove_label(label)

    def set_show_status(self, show: Show, new_status: str, dry_run: bool = False) -> None:
        """Attach a replacement status before retiring stale status labels."""
        show.status = new_status

        if dry_run:
            return

        # 1. Refresh labels to get current GitHub state
        self.refresh_labels()

        new_status_label = f"🎪 {show.sha} 🚦 {new_status}"
        if new_status_label not in self.labels:
            self.add_label(new_status_label)

        # 2. Remove stale status labels only after the replacement is usable.
        status_labels_to_remove = [
            label
            for label in self.labels
            if label.startswith(f"🎪 {show.sha} 🚦 ") and label != new_status_label
        ]

        for label in status_labels_to_remove:
            self.remove_label(label)

    def set_active_show(
        self, show: Show, *, active: bool = True, dry_run: bool = False
    ) -> PromotionResult:
        """Attach a candidate pointer first, or clear only the specified pointer."""
        from .emojis import CIRCUS_PREFIX, MEANING_TO_EMOJI

        if dry_run:
            return PromotionResult(success=True, pointer_attached=active)

        self.refresh_labels()

        # 2. Remove ALL existing active pointers (ensure only one)
        active_emoji = MEANING_TO_EMOJI["active"]  # Gets 🎯
        active_prefix = f"{CIRCUS_PREFIX} {active_emoji} "  # "🎪 🎯 "
        active_pointer = f"{active_prefix}{show.sha}"  # "🎪 🎯 abc123f"
        if not active:
            if active_pointer in self.labels:
                self.remove_label(active_pointer)
            return PromotionResult(success=True, pointer_attached=False)

        if active_pointer not in self.labels:
            self.add_label(active_pointer)

        errors = []
        active_pointers = [
            label
            for label in self.labels
            if label.startswith(active_prefix) and label != active_pointer
        ]
        for pointer in active_pointers:
            try:
                self.remove_label(pointer)
            except Exception as exc:
                errors.append(f"{pointer}: {exc}")

        return PromotionResult(
            success=not errors,
            pointer_attached=True,
            errors=errors,
        )

    def _check_authorization(self, dry_run_github: bool = False) -> tuple[bool, dict]:
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
                    if not dry_run_github:
                        self.add_label("🎪 🔒 showtime-blocked")
                else:
                    debug_info["auth_status"] = "authorized"

                return authorized, debug_info

        except Exception as e:
            debug_info["auth_status"] = f"error_{type(e).__name__}"
            debug_info["error"] = str(e)
            print(f"⚠️ Authorization check failed: {e}")
            return True, debug_info  # Fail open for non-security operations

    def analyze(
        self,
        target_sha: str,
        pr_state: str = "open",
        dry_run_github: bool = False,
    ) -> SyncState:
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
        is_authorized, auth_debug = self._check_authorization(dry_run_github)
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

    def _best_effort_comment(self, callback: Any, *args: Any) -> None:
        """Run a comment callback without changing lifecycle truth on failure."""
        try:
            callback(*args)
        except Exception as exc:
            print(f"⚠️ GitHub comment failed: {exc}")

    def _candidate_cleanup(
        self, candidate: Show, dry_run_github: bool, dry_run_aws: bool
    ) -> CleanupResult:
        """Compensate only a failed candidate with verifiable ownership."""
        if candidate.service_created is False:
            if candidate.cleanup_pending:
                return CleanupResult(
                    success=False,
                    pending_shas=[candidate.sha],
                    errors=[f"{candidate.sha}: previous allocation still requires cleanup"],
                )
            return CleanupResult(success=True)

        errors: List[str] = []
        deleted: List[str] = []
        try:
            stopped = candidate.stop(
                dry_run_github=dry_run_github,
                dry_run_aws=dry_run_aws,
                require_ownership=True,
                delete_image=False,
            )
            if stopped:
                deleted.append(candidate.sha)
            else:
                errors.append(f"{candidate.sha}: candidate cleanup was not confirmed")
        except Exception as exc:
            errors.append(f"{candidate.sha}: {exc}")

        candidate.cleanup_pending = not deleted
        return CleanupResult(
            success=bool(deleted),
            attempted_shas=[candidate.sha],
            deleted_shas=deleted,
            pending_shas=[] if deleted else [candidate.sha],
            errors=errors,
        )

    def _record_failed_candidate(
        self,
        candidate: Show,
        error: Exception,
        dry_run_github: bool,
        dry_run_aws: bool,
    ) -> SyncResult:
        """Persist failed candidate truth and any remaining allocation state."""
        cleanup_result = self._candidate_cleanup(candidate, dry_run_github, dry_run_aws)
        candidate.status = "failed"
        try:
            self._update_show_labels(candidate, dry_run_github)
        except Exception as label_error:
            cleanup_result.success = False
            cleanup_result.errors.append(f"{candidate.sha}: labels: {label_error}")
            if candidate.sha not in cleanup_result.pending_shas:
                cleanup_result.pending_shas.append(candidate.sha)
        return SyncResult(
            success=False,
            action_taken="failed",
            show=candidate,
            error=str(error),
            cleanup_result=cleanup_result,
        )

    def _reconcile_running_show(self, show: Show, dry_run_github: bool) -> SyncResult:
        """Converge resource labels and pointer state without rebuilding."""
        self._update_show_labels(show, dry_run_github)
        promotion = self.set_active_show(show, dry_run=dry_run_github)
        if promotion.success:
            return SyncResult(True, "no_action", show=show)
        cleanup = CleanupResult(
            success=False,
            pending_shas=[show.sha],
            errors=promotion.errors,
        )
        return SyncResult(
            False,
            "promotion_pending",
            show=show,
            error="Active-pointer reconciliation is incomplete",
            cleanup_result=cleanup,
        )

    def sync(
        self,
        target_sha: str,
        dry_run_github: bool = False,
        dry_run_aws: bool = False,
        dry_run_docker: bool = False,
        startup_timeout_seconds: int = DEFAULT_STARTUP_TIMEOUT_SECONDS,
        smoke_test: bool = False,
        smoke_timeout_seconds: int = DEFAULT_SMOKE_TIMEOUT_SECONDS,
        build_timeout_seconds: int = DEFAULT_BUILD_TIMEOUT_SECONDS,
        smoke_diagnostics_dir: str = DEFAULT_DIAGNOSTICS_DIR,
    ) -> SyncResult:
        """Sync PR to desired state while preserving truthful lifecycle state.

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
        startup_timeout_seconds = validate_startup_timeout_seconds(startup_timeout_seconds)
        smoke_timeout_seconds = validate_positive_seconds(smoke_timeout_seconds, "smoke timeout")
        build_timeout_seconds = validate_positive_seconds(build_timeout_seconds, "build timeout")
        smoke_diagnostics_dir = str(validate_diagnostics_directory(smoke_diagnostics_dir))
        if dry_run_docker and not dry_run_aws:
            raise ValueError("--dry-run-docker requires --dry-run-aws")
        if dry_run_docker and smoke_test:
            raise ValueError("runner smoke cannot be enabled when Docker is skipped")

        action_needed = self._determine_action(target_sha, dry_run_github)
        target_sha_short = short_sha(target_sha)
        target_before_claim = self.get_show_by_sha(target_sha_short)
        previous_running = [
            show for show in self.shows if show.status == "running" and show.sha != target_sha_short
        ]

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

        feature_flags: List[Dict[str, str]] = []
        if action_needed != "destroy_environment":
            try:
                pr_data = get_github().get_pr_data(self.pr_number)
                feature_flags = parse_feature_flags(pr_data.get("body"))
                if feature_flags:
                    flag_names = [f["name"] for f in feature_flags]
                    print(f"🏁 Feature flags from PR description: {', '.join(flag_names)}")
            except Exception as e:
                print(f"⚠️ Failed to fetch PR description for feature flags: {e}")

        if action_needed == "destroy_environment":
            result = self.stop_environment(dry_run_github=dry_run_github, dry_run_aws=dry_run_aws)
            if result.success and result.show:
                self._best_effort_comment(self._post_cleanup_comment, result.show, dry_run_github)
            return SyncResult(
                result.success,
                "destroy_environment",
                show=result.show,
                error=result.error,
                cleanup_result=result.cleanup_result,
            )

        if action_needed not in ["create_environment", "rolling_update", "auto_sync"]:
            running_target = self.get_show_by_sha(target_sha_short)
            if running_target and running_target.status == "running":
                result = self._reconcile_running_show(running_target, dry_run_github)
                if not result.success:
                    return result
                if feature_flags:
                    self._update_feature_flags_if_changed(
                        running_target, feature_flags, dry_run_aws
                    )
            return SyncResult(success=True, action_taken="no_action")

        candidate = self._create_new_show(target_sha)
        same_sha_rebuild = bool(target_before_claim and target_before_claim.status == "running")
        if same_sha_rebuild:
            print(
                "⚠️ Destructive same-SHA rebuild: deterministic service reuse "
                "provides no fallback if deployment fails."
            )

        promotion_committed = False
        try:
            print(f"🏗️ Creating environment {candidate.sha}...")
            self._best_effort_comment(self._post_building_comment, candidate, dry_run_github)
            if build_timeout_seconds == DEFAULT_BUILD_TIMEOUT_SECONDS:
                built_reference = candidate.build_docker(dry_run_docker)
            else:
                built_reference = candidate.build_docker(
                    dry_run_docker, build_timeout_seconds=build_timeout_seconds
                )
            image_reference = None if dry_run_docker else built_reference
            if not dry_run_docker and image_reference is None:
                raise RuntimeError("Docker build did not produce an immutable image reference")
            if smoke_test:
                if image_reference is None:
                    raise RuntimeError("runner smoke requires an immutable image reference")
                smoke = candidate.run_smoke(
                    image_reference,
                    feature_flags=feature_flags,
                    timeout_seconds=smoke_timeout_seconds,
                    diagnostics_dir=smoke_diagnostics_dir,
                )
                if not smoke.success:
                    evidence = ", ".join(smoke.artifact_paths) or "no artifact was written"
                    raise RuntimeError(f"Runner smoke failed: {smoke.error}; evidence: {evidence}")
            self.set_show_status(candidate, "deploying", dry_run_github)
            deploy_options: Dict[str, Any] = {
                "feature_flags": feature_flags,
                "startup_timeout_seconds": startup_timeout_seconds,
            }
            if image_reference is not None:
                deploy_options["image_reference"] = image_reference
            candidate.deploy_aws(dry_run_aws, **deploy_options)
            self.set_show_status(candidate, "running", dry_run_github)
            self._update_show_labels(candidate, dry_run_github)

            promotion = self.set_active_show(candidate, dry_run=dry_run_github)
            promotion_committed = promotion.pointer_attached
            if not promotion.success:
                return SyncResult(
                    False,
                    "promotion_pending",
                    show=candidate,
                    error="Candidate pointer attached, but stale pointers remain",
                    cleanup_result=CleanupResult(
                        False,
                        pending_shas=[candidate.sha],
                        errors=promotion.errors,
                    ),
                )

            cleanup = self._cleanup_shows(
                previous_running,
                dry_run_github=dry_run_github,
                dry_run_aws=dry_run_aws,
                clear_controls=False,
            )
            self._show_service_urls(candidate)
            self._best_effort_comment(self._post_success_comment, candidate, dry_run_github)
            if not cleanup.success:
                return SyncResult(
                    False,
                    action_needed,
                    show=candidate,
                    error="Previous environment cleanup is incomplete",
                    cleanup_result=cleanup,
                )
            return SyncResult(
                True,
                action_needed,
                show=candidate,
                cleanup_result=cleanup,
            )
        except Exception as exc:
            if promotion_committed:
                return SyncResult(
                    False,
                    "post_promotion_failed",
                    show=candidate,
                    error=f"Environment remains promoted; a later operation failed: {exc}",
                )
            result = self._record_failed_candidate(candidate, exc, dry_run_github, dry_run_aws)

            # Post a failure comment so the last comment on the PR reflects
            # reality (the prior deployed/updating comment may already be
            # gone - _post_showtime_comment cleans up on every post).
            from .github_messages import failure_comment, rolling_failure_comment

            try:
                if previous_running:
                    self._post_showtime_comment(
                        rolling_failure_comment(previous_running[0], candidate.sha, str(exc)),
                        dry_run_github,
                    )
                else:
                    self._post_showtime_comment(
                        failure_comment(candidate, str(exc)), dry_run_github
                    )
            except Exception as post_error:
                print(f"⚠️ Failed to post failure comment: {post_error}")
            return result

    def start_environment(self, sha: Optional[str] = None, **kwargs: Any) -> SyncResult:
        """Start a new environment (CLI start command logic)"""
        target_sha = sha or get_github().get_latest_commit_sha(self.pr_number)
        return self.sync(target_sha, **kwargs)

    def stop_environment(self, **kwargs: Any) -> SyncResult:
        """Stop every tracked environment and return aggregate cleanup truth."""
        dry_run_github = bool(kwargs.get("dry_run_github", False))
        dry_run_aws = bool(kwargs.get("dry_run_aws", False))
        snapshot = list(self.shows)
        cleanup = self._cleanup_shows(
            snapshot,
            dry_run_github=dry_run_github,
            dry_run_aws=dry_run_aws,
            clear_controls=True,
        )
        action = "stopped" if cleanup.success else "stop_failed"
        error = None if cleanup.success else "; ".join(cleanup.errors)
        return SyncResult(
            cleanup.success,
            action,
            show=self.current_show,
            error=error,
            cleanup_result=cleanup,
        )

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

    def _determine_action(self, target_sha: str, dry_run_github: bool = False) -> str:
        """Determine what sync action is needed (includes all checks and refreshes labels)"""
        # CRITICAL: Get fresh labels before any decisions
        self.refresh_labels()

        # Check for blocked state first (fast bailout)
        if "🎪 🔒 showtime-blocked" in self.labels:
            return "blocked"

        # Check authorization (security layer)
        is_authorized, _ = self._check_authorization(dry_run_github)
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

            # Reconcile add-first so a failed replacement cannot erase the
            # prior status sentinel or active pointer.
            self._update_show_labels(building_show)

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

    def _update_feature_flags_if_changed(
        self,
        show: Show,
        feature_flags: List[Dict[str, str]],
        dry_run: bool = False,
    ) -> None:
        """Reconcile feature flags on a running environment to match PR description.

        Compares desired flags (from PR description) against current ECS state
        and updates only if they differ. Uses reconcile_feature_flags which
        replaces the entire SUPERSET_FEATURE_* set, handling partial removals.

        Note: only called when feature_flags is non-empty. Full flag removal
        (PR description cleared of all flags) is handled on the next deploy.
        """
        # Build desired state: {name: "True"/"False"} matching ECS format
        desired_flags: Dict[str, str] = {f["name"]: f["value"] for f in feature_flags}

        if dry_run:
            print(f"🏁 [dry-run] Would reconcile feature flags on {show.sha}")
            return

        aws = get_aws()

        # Fetch current SUPERSET_FEATURE_* flags from ECS to compare
        current_flags = aws.get_current_feature_flags(show.ecs_service_name)

        if current_flags == desired_flags:
            print(f"🏁 Feature flags on {show.sha} already match desired state, skipping update")
            return

        print(f"🏁 Reconciling feature flags on running environment {show.sha}...")
        success = aws.reconcile_feature_flags(show.ecs_service_name, desired_flags)
        if success:
            print("✅ Feature flags reconciled successfully")
        else:
            print("⚠️ Feature flag reconciliation failed")

    def _create_new_show(self, target_sha: str) -> Show:
        """Create a new Show object for the target SHA"""
        from .date_utils import format_utc_now

        previous = self.get_show_by_sha(short_sha(target_sha))
        pending = previous if previous and previous.cleanup_pending else None
        return Show(
            pr_number=self.pr_number,
            sha=short_sha(target_sha),
            status="building",
            created_at=format_utc_now(),
            requested_by=GitHubInterface.get_current_actor(),
            task_definition_arn=pending.task_definition_arn if pending else None,
            task_definition_fingerprint=(pending.task_definition_fingerprint if pending else None),
            cleanup_pending=pending is not None,
        )

    def _post_showtime_comment(self, comment: str, dry_run: bool = False) -> None:
        """Post a Showtime comment, then delete superseded Showtime comments

        Each new lifecycle comment replaces the previous ones so PR threads
        don't accumulate stale building/deployed/updating messages. Posting
        first (and excluding the new comment from the sweep) means a failed
        cleanup never costs the new comment, and a failed post never costs
        the old ones - worst case the old comments stick around.
        """
        if dry_run:
            return

        github = get_github()
        posted = github.post_comment(self.pr_number, f"{comment}\n\n{SHOWTIME_COMMENT_MARKER}")

        try:
            deleted = github.delete_showtime_comments(self.pr_number, except_id=posted["id"])
            if deleted:
                print(f"🧹 Removed {deleted} superseded Showtime comment(s)")
        except Exception as e:
            print(f"⚠️ Failed to clean up old Showtime comments: {e}")

    def _post_building_comment(self, show: Show, dry_run: bool = False) -> None:
        """Post building comment for new environment"""
        from .github_messages import building_comment

        self._post_showtime_comment(building_comment(show), dry_run)

    def _post_success_comment(self, show: Show, dry_run: bool = False) -> None:
        """Post success comment for completed environment"""
        from .github_messages import success_comment

        effective_ttl = self._get_effective_ttl_display()
        self._post_showtime_comment(success_comment(show, ttl=effective_ttl), dry_run)

    def _post_cleanup_comment(self, show: Show, dry_run: bool = False) -> None:
        """Post cleanup completion comment"""
        from .github_messages import cleanup_comment

        self._post_showtime_comment(cleanup_comment(show), dry_run)

    def stop_if_expired(self, max_age_hours: int, dry_run: bool = False) -> bool:
        """Stop environment if it's expired based on age

        Args:
            max_age_hours: Maximum age in hours before expiration
            dry_run: If True, just check don't actually stop

        Returns:
            True if environment was expired (and stopped), False otherwise
        """
        result = self.stop_if_expired_result(max_age_hours, dry_run)
        return bool(result and result.success)

    def stop_if_expired_result(
        self, max_age_hours: int, dry_run: bool = False
    ) -> Optional[CleanupResult]:
        """Return cleanup detail for an expired active environment, if any."""
        current = self.current_show
        if not current or not current.is_expired(max_age_hours):
            return None
        print(f"🧹 Stopping expired environment: PR #{self.pr_number}")
        sync_result = self.stop_environment(dry_run_github=dry_run, dry_run_aws=dry_run)
        return sync_result.cleanup_result or CleanupResult(
            success=sync_result.success,
            attempted_shas=[current.sha],
            deleted_shas=[current.sha] if sync_result.success else [],
            pending_shas=[] if sync_result.success else [current.sha],
            errors=[] if sync_result.success else [sync_result.error or "cleanup failed"],
        )

    def cleanup_orphaned_shows(self, max_age_hours: int, dry_run: bool = False) -> int:
        """Clean up orphaned shows (environments without pointer labels)

        Args:
            max_age_hours: Maximum age in hours before considering orphaned environment for cleanup
            dry_run: If True, just check don't actually stop

        Returns:
            Number of orphaned environments cleaned up
        """
        result = self.cleanup_orphaned_shows_result(max_age_hours, dry_run)
        return len(result.deleted_shas)

    def cleanup_orphaned_shows_result(
        self, max_age_hours: int, dry_run: bool = False
    ) -> CleanupResult:
        """Return aggregate cleanup detail for expired shows without pointers."""
        orphaned = [
            show
            for show in self.shows
            if not any(
                pointer in self.labels for pointer in [f"🎪 🎯 {show.sha}", f"🎪 🏗️ {show.sha}"]
            )
            and show.is_expired(max_age_hours)
        ]
        return self._cleanup_shows(
            orphaned,
            dry_run_github=dry_run,
            dry_run_aws=dry_run,
            clear_controls=False,
        )

    @classmethod
    def find_all_with_environments(cls, include_closed: bool = False) -> List[int]:
        """Find all PR numbers that have active environments.

        Args:
            include_closed: If True, also include closed/merged PRs. Useful for
                orphan detection where closed PRs may still have label definitions.
        """
        return get_github().find_prs_with_shows(include_closed=include_closed)

    def _update_show_labels(self, show: Show, dry_run: bool = False) -> None:
        """Reconcile only SHA-owned resource labels, adding before removing."""
        if dry_run:
            return

        self.refresh_labels()
        current_sha_labels = {label for label in self.labels if label.startswith(f"🎪 {show.sha} ")}
        desired_labels = set(show.to_circus_labels())

        labels_to_add = sorted(desired_labels - current_sha_labels)
        for label in labels_to_add:
            self.add_label(label)

        labels_to_remove = sorted(
            current_sha_labels - desired_labels,
            key=lambda label: " 🚦 " in label,
        )
        for label in labels_to_remove:
            self.remove_label(label)

        self.refresh_labels()

    def _mark_cleanup_pending(self, show: Show, error: str) -> List[str]:
        """Retain a failed show's discoverability and cleanup-pending marker."""
        errors = [error]
        show.cleanup_pending = True
        try:
            self._update_show_labels(show)
        except Exception as exc:
            errors.append(f"{show.sha}: failed to record cleanup-pending: {exc}")
        return errors

    def _cleanup_shows(
        self,
        shows: List[Show],
        *,
        dry_run_github: bool,
        dry_run_aws: bool,
        clear_controls: bool,
    ) -> CleanupResult:
        """Attempt a snapshot of tracked shows and preserve every failed item."""
        snapshot = list(shows)
        deleted: List[str] = []
        pending: List[str] = []
        errors: List[str] = []
        for show in snapshot:
            try:
                if not show.stop(dry_run_github=dry_run_github, dry_run_aws=dry_run_aws):
                    raise RuntimeError("AWS deletion was not confirmed")
                if not dry_run_github:
                    self.set_active_show(show, active=False)
                    self.remove_sha_labels(show.sha, delete_definitions=False)
                deleted.append(show.sha)
            except Exception as exc:
                pending.append(show.sha)
                errors.extend(
                    self._mark_cleanup_pending(show, f"{show.sha}: {exc}")
                    if not dry_run_github
                    else [f"{show.sha}: {exc}"]
                )

        if not dry_run_github:
            try:
                self.refresh_labels()
                if clear_controls and not self.shows and not pending:
                    self.remove_showtime_labels(delete_definitions=False)
            except Exception as exc:
                errors.append(f"PR #{self.pr_number}: label cleanup: {exc}")
        return CleanupResult(
            success=not errors,
            attempted_shas=[show.sha for show in snapshot],
            deleted_shas=deleted,
            pending_shas=pending,
            errors=errors,
        )

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
        cleanup = self._cleanup_shows(
            [show for show in self.shows if show.sha != keep_sha],
            dry_run_github=dry_run_github,
            dry_run_aws=dry_run_aws,
            clear_controls=False,
        )
        return len(cleanup.deleted_shas)
