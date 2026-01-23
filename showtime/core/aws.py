"""
🎪 AWS interface for circus tent environment management

Replicates the AWS logic from current GitHub Actions workflows.
"""

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

import boto3  # type: ignore[import-untyped]


@dataclass
class AWSError(Exception):
    """AWS operation error"""

    message: str
    operation: str
    resource: Optional[str] = None


@dataclass
class EnvironmentResult:
    """Result of AWS environment operation"""

    success: bool
    ip: Optional[str] = None
    service_name: Optional[str] = None
    error: Optional[str] = None


class AWSInterface:
    """AWS ECS/ECR client replicating current GHA logic"""

    def __init__(
        self,
        region: Optional[str] = None,
        cluster: Optional[str] = None,
        repository: Optional[str] = None,
    ):
        self.region = region or os.getenv("AWS_REGION", "us-west-2")
        self.cluster = cluster or os.getenv("ECS_CLUSTER", "superset-ci")
        self.repository = repository or os.getenv("ECR_REPOSITORY", "superset-ci")

        # AWS clients
        self.ecs_client = boto3.client("ecs", region_name=self.region)
        self.ecr_client = boto3.client("ecr", region_name=self.region)
        self.ec2_client = boto3.client("ec2", region_name=self.region)

        # Network configuration (from current GHA)
        self.subnets = ["subnet-0e15a5034b4121710", "subnet-0e8efef4a72224974"]
        self.security_group = "sg-092ff3a6ae0574d91"

    def create_environment(
        self,
        pr_number: int,
        sha: str,
        github_user: str = "unknown",
        feature_flags: Optional[List[Dict[str, str]]] = None,
        image_tag_override: Optional[str] = None,
        force: bool = False,
    ) -> EnvironmentResult:
        """
        Create ephemeral environment (replaces any existing service with same name)

        Steps:
        1. Create task definition with feature flags
        2. Delete any existing service with same name (clean slate)
        3. Create fresh ECS service
        4. Deploy and wait for stability
        5. Health check and return IP
        """

        # Create Show object for consistent AWS naming
        from .date_utils import format_utc_now
        from .show import Show

        show = Show(
            pr_number=pr_number,
            sha=sha[:7],  # Truncate to 7 chars like GitHub
            status="building",
            created_at=format_utc_now(),
            requested_by=github_user,
        )

        service_name = show.ecs_service_name  # pr-{pr_number}-{sha}-service

        try:
            # Handle force flag - delete existing service for this SHA first
            if force:
                print(f"🗑️ Force flag: Checking for existing service {service_name}")
                if self._service_exists(service_name):
                    print(f"🗑️ Deleting existing service: {service_name}")
                    success = self._delete_ecs_service(service_name)
                    if success:
                        print("✅ Service deletion initiated, waiting for completion...")
                        # Wait for service to be fully deleted before proceeding
                        if self._wait_for_service_deletion(service_name):
                            print("✅ Service deletion completed, proceeding with fresh deployment")
                        else:
                            print("⚠️ Service deletion timeout, proceeding anyway")
                    else:
                        print("⚠️ Failed to delete existing service, proceeding anyway")
                else:
                    print("ℹ️ No existing service found, proceeding with new deployment")
            # Step 1: Determine which Docker image to use (DockerHub direct)
            if image_tag_override:
                # Use explicit override (can be any format)
                docker_image = f"apache/superset:{image_tag_override}"
                print(f"✅ Using override image: {docker_image}")
            else:
                # Use supersetbot PR-SHA format (what supersetbot creates)
                supersetbot_tag = show.aws_image_tag  # pr-{pr_number}-{sha}-ci
                docker_image = f"apache/superset:{supersetbot_tag}"
                print(f"✅ Using DockerHub image: {docker_image} (supersetbot PR-SHA format)")
                print(
                    "💡 To test with different image: --image-tag latest or --image-tag pr-34639-9a82c20-ci"
                )

            # Note: No ECR image check needed - ECS will pull from DockerHub directly

            # Step 2: Create/update ECS task definition with feature flags
            task_def_arn = self._create_task_definition_with_image_and_flags(
                docker_image, feature_flags or []
            )
            if not task_def_arn:
                return EnvironmentResult(success=False, error="Failed to create task definition")

            # Step 3: Clean slate - Ensure no service with this exact name exists
            # CRITICAL: Use _service_exists_any_state() which calls describe_services directly,
            # NOT _find_pr_services() which uses list_services(). This is because:
            # - list_services() only returns ACTIVE services
            # - A service in DRAINING state won't appear in list_services()
            # - But DRAINING services still block creation with "not idempotent" error
            # - describe_services() returns services in ANY state (ACTIVE, DRAINING, INACTIVE)
            print(f"🔍 Checking for existing service: {service_name}")
            if self._service_exists_any_state(service_name):
                print(f"🗑️ Found existing service (may be DRAINING): {service_name}")
                self._delete_ecs_service(service_name)
                print("✅ Service deletion initiated, waiting for completion...")
                self._wait_for_service_deletion(service_name)

            # Step 4: Create fresh service
            print(f"🆕 Creating service: {service_name}")
            success = self._create_ecs_service(service_name, pr_number, github_user, task_def_arn)
            if not success:
                return EnvironmentResult(success=False, error="Service creation failed")

            # Step 5: Deploy task definition to green service
            success = self._deploy_task_definition(service_name, task_def_arn)
            if not success:
                return EnvironmentResult(
                    success=False, error="Green task definition deployment failed"
                )

            # Step 6: Wait for service stability (replicate GHA wait-for-service-stability)
            print(f"⏳ Waiting for service {service_name} to become stable...")
            if not self._wait_for_service_stability(service_name):
                return EnvironmentResult(success=False, error="Service failed to become stable")

            # Step 7: Health check the new service (longer timeout for Superset + examples)
            print(f"🏥 Health checking service {service_name}...")
            if not self._health_check_service(service_name, max_attempts=20):  # 10 minutes total
                return EnvironmentResult(success=False, error="Service failed health checks")

            # Step 8: Get IP after health checks pass
            ip = self.get_environment_ip(service_name)
            if not ip:
                return EnvironmentResult(success=False, error="Failed to get environment IP")

            return EnvironmentResult(success=True, ip=ip, service_name=service_name)

        except Exception as e:
            return EnvironmentResult(success=False, error=str(e))

    def delete_environment(self, base_name: str, pr_number: int) -> bool:
        """
        Delete ephemeral environment

        Args:
            base_name: Base service name WITHOUT -service suffix (e.g., "pr-34868-abc123f")
            pr_number: PR number for this environment
        """
        try:
            # Simple: always add -service suffix
            ecs_service_name = f"{base_name}-service"
            print(f"🗑️ Deleting ECS service: {ecs_service_name}")

            # Delete ECS service with force flag (AWS will handle cleanup)
            try:
                self.ecs_client.delete_service(
                    cluster=self.cluster, service=ecs_service_name, force=True
                )
                print(f"✅ ECS service deletion initiated: {ecs_service_name}")
            except self.ecs_client.exceptions.ServiceNotFoundException:
                print(f"ℹ️ Service {ecs_service_name} already deleted")
            except Exception as e:
                print(f"❌ AWS deletion failed: {e}")
                return False

            # Try to clean up ECR image - for showtime services, tag is base_name + "-ci"
            try:
                image_tag = f"{base_name}-ci"
                self.ecr_client.batch_delete_image(
                    repositoryName=self.repository, imageIds=[{"imageTag": image_tag}]
                )
                print(f"✅ Deleted ECR image: {image_tag}")
            except Exception:
                pass  # Image cleanup is optional

            return True

        except Exception as e:
            print(f"❌ Unexpected error: {e}")
            return False

    def get_environment_ip(self, service_name: str) -> Optional[str]:
        """
        Get public IP for environment - replicates GHA IP discovery logic

        Steps:
        1. List tasks for service
        2. Describe task to get network interface
        3. Get public IP from network interface
        """
        try:
            # Step 1: List tasks
            tasks_response = self.ecs_client.list_tasks(
                cluster=self.cluster, serviceName=service_name
            )

            if not tasks_response["taskArns"]:
                return None

            task_arn = tasks_response["taskArns"][0]

            # Step 2: Describe task to get network interface
            task_response = self.ecs_client.describe_tasks(cluster=self.cluster, tasks=[task_arn])

            if not task_response["tasks"]:
                return None

            task = task_response["tasks"][0]

            # Find network interface ID
            eni_id = None
            for attachment in task.get("attachments", []):
                for detail in attachment.get("details", []):
                    if detail["name"] == "networkInterfaceId":
                        eni_id = detail["value"]
                        break
                if eni_id:
                    break

            if not eni_id:
                return None

            # Step 3: Get public IP from network interface
            eni_response = self.ec2_client.describe_network_interfaces(NetworkInterfaceIds=[eni_id])

            if not eni_response["NetworkInterfaces"]:
                return None

            eni = eni_response["NetworkInterfaces"][0]
            public_ip = eni.get("Association", {}).get("PublicIp")
            return str(public_ip) if public_ip else None

        except Exception:
            return None

    def get_environment_status(self, service_name: str) -> str:
        """Get environment status from AWS"""
        try:
            response = self.ecs_client.describe_services(
                cluster=self.cluster, services=[service_name]
            )

            if not response["services"]:
                return "not_found"

            service = response["services"][0]
            status = service["status"]

            if status == "ACTIVE":
                # Check if tasks are running
                running_count = service["runningCount"]
                desired_count = service["desiredCount"]

                if running_count == desired_count and running_count > 0:
                    return "running"
                else:
                    return "building"
            else:
                return "failed"

        except Exception:
            return "unknown"

    def _check_ecr_image_exists(self, image_tag: str) -> bool:
        """Check if ECR image exists (replicate GHA check-image step)"""
        try:
            # Get registry ID from ECR login
            ecr_response = self.ecr_client.get_authorization_token()
            registry_id = ecr_response["authorizationData"][0]["proxyEndpoint"]
            registry_id = registry_id.split(".")[0].replace("https://", "")

            # Replicate exact GHA describe-images command
            self.ecr_client.describe_images(
                registryId=registry_id,
                repositoryName=self.repository,
                imageIds=[{"imageTag": image_tag}],
            )

            print(f"✅ Found ECR image: {image_tag}")
            return True

        except self.ecr_client.exceptions.ImageNotFoundException:
            print(f"❌ ECR image not found: {image_tag}")
            return False
        except Exception as e:
            print(f"❌ ECR image check failed: {e}")
            return False

    def _create_task_definition_with_image_and_flags(
        self, docker_image: str, feature_flags: List[Dict[str, str]]
    ) -> Optional[str]:
        """Create ECS task definition with DockerHub image and feature flags"""
        try:
            # Load base task definition template
            task_def_path = Path(__file__).parent.parent / "data" / "ecs-task-definition.json"
            with open(task_def_path) as f:
                task_def = json.load(f)

            # Use DockerHub image directly (no ECR needed)
            # docker_image is already in format: apache/superset:abc123f-ci
            task_def["containerDefinitions"][0]["image"] = docker_image

            # Add feature flags to environment (replicate GHA jq environment update)
            container_env = task_def["containerDefinitions"][0]["environment"]
            for flag in feature_flags:
                container_env.append(flag)

            # Register task definition
            response = self.ecs_client.register_task_definition(**task_def)
            task_def_arn = response["taskDefinition"]["taskDefinitionArn"]

            print(f"✅ Created task definition: {task_def_arn}")
            return str(task_def_arn)

        except Exception as e:
            print(f"❌ Task definition creation failed: {e}")
            return None

    def _deploy_task_definition(self, service_name: str, task_def_arn: str) -> bool:
        """Deploy task definition to service (replicate GHA deploy-task step)"""
        try:
            # Replicate exact GHA deploy-task-definition parameters
            self.ecs_client.update_service(
                cluster=self.cluster, service=service_name, taskDefinition=task_def_arn
            )

            print(f"✅ Updated service {service_name} with task definition")
            return True

        except Exception as e:
            print(f"❌ Task definition deployment failed: {e}")
            return False

    def _service_exists(self, service_name: str) -> bool:
        """Check if ECS service exists and is active"""
        try:
            response = self.ecs_client.describe_services(
                cluster=self.cluster, services=[service_name]
            )

            for service in response["services"]:
                if service["status"] == "ACTIVE":
                    return True

            return False

        except Exception:
            return False

    def _service_exists_any_state(self, service_name: str) -> bool:
        """Check if ECS service exists in a state that blocks CreateService.

        Unlike _service_exists() which only checks for ACTIVE services, this method
        also detects DRAINING services. This is critical because:
        - list_services() only returns ACTIVE services
        - A service in DRAINING state still blocks CreateService with "not idempotent" error
        - describe_services() returns services in any state

        Note: INACTIVE services do NOT block CreateService, so we don't check for them.

        Use this method before creating a service to detect DRAINING services
        from previous failed/cancelled deployments.
        """
        try:
            response = self.ecs_client.describe_services(
                cluster=self.cluster, services=[service_name]
            )

            for service in response.get("services", []):
                status = service.get("status", "")
                # ACTIVE = running, DRAINING = being deleted
                # INACTIVE services don't block CreateService, so we ignore them
                if status in ["ACTIVE", "DRAINING"]:
                    return True

            return False

        except Exception:
            return False

    def _create_ecs_service(
        self, service_name: str, pr_number: int, github_user: str, task_def_arn: str
    ) -> bool:
        """Create ECS service (replicate exact GHA create-service step)"""
        try:
            # Replicate exact GHA create-service command parameters
            self.ecs_client.create_service(
                cluster=self.cluster,
                serviceName=service_name,  # pr-{pr_number}-service
                taskDefinition=task_def_arn,  # Use our custom task definition with env vars
                launchType="FARGATE",
                desiredCount=1,
                platformVersion="LATEST",
                networkConfiguration={
                    "awsvpcConfiguration": {
                        "subnets": self.subnets,  # Same subnets as GHA
                        "securityGroups": [self.security_group],  # Same SG as GHA
                        "assignPublicIp": "ENABLED",
                    }
                },
                tags=[
                    {"key": "pr", "value": str(pr_number)},
                    {"key": "github_user", "value": github_user},
                    {"key": "showtime_created", "value": str(int(time.time()))},
                    {
                        "key": "showtime_expires",
                        "value": str(int(time.time()) + 48 * 3600),
                    },  # 48 hours
                    {"key": "showtime_managed", "value": "true"},
                ],
            )

            print(f"✅ Created ECS service: {service_name}")
            return True

        except Exception as e:
            print(f"❌ ECS service creation failed: {e}")
            return False

    def _wait_for_deployment_and_get_ip(
        self, service_name: str, timeout_minutes: int = 10
    ) -> Optional[str]:
        """Wait for ECS deployment to complete and get IP"""
        try:
            # Wait for service stability (replicate GHA wait-for-service-stability)
            waiter = self.ecs_client.get_waiter("services_stable")
            waiter.wait(
                cluster=self.cluster,
                services=[service_name],
                WaiterConfig={"maxAttempts": timeout_minutes * 2},  # 30s intervals
            )

            # Get IP after deployment is stable
            return self.get_environment_ip(service_name)

        except Exception:
            return None

    def list_circus_environments(self) -> List[Dict[str, Any]]:
        """List all environments with circus tags"""
        try:
            # List all services in cluster
            services_response = self.ecs_client.list_services(cluster=self.cluster)

            circus_services = []
            for service_arn in services_response["serviceArns"]:
                service_name = service_arn.split("/")[-1]

                # Check if it's a circus service (pr-{number}-{sha} pattern)
                if service_name.startswith("pr-") and len(service_name.split("-")) >= 3:
                    # Get service details and tags
                    service_response = self.ecs_client.describe_services(
                        cluster=self.cluster, services=[service_name]
                    )

                    if service_response["services"]:
                        service = service_response["services"][0]
                        circus_services.append(
                            {
                                "service_name": service_name,
                                "status": service["status"],
                                "running_count": service["runningCount"],
                                "desired_count": service["desiredCount"],
                                "created_at": service["createdAt"],
                                "ip": self.get_environment_ip(service_name),
                            }
                        )

            return circus_services

        except Exception:
            return []

    def cleanup_orphaned_environments(self, max_age_hours: int = 48) -> List[str]:
        """Clean up environments older than max_age_hours"""
        import time

        try:
            orphaned = []
            circus_services = self.list_circus_environments()

            current_time = time.time()
            max_age_seconds = max_age_hours * 3600

            for service in circus_services:
                # Calculate age
                created_timestamp = service["created_at"].timestamp()
                age_seconds = current_time - created_timestamp

                if age_seconds > max_age_seconds:
                    service_name = service["service_name"]

                    # Extract PR number for cleanup
                    pr_number = int(service_name.split("-")[1])

                    # Delete the service
                    if self.delete_environment(service_name, pr_number):
                        orphaned.append(service_name)

            return orphaned

        except Exception as e:
            raise AWSError(message=str(e), operation="cleanup_orphaned_environments") from e

    def update_feature_flags(self, service_name: str, feature_flags: Dict[str, bool]) -> bool:
        """Update feature flags in running environment"""
        try:
            # Get current task definition
            service_response = self.ecs_client.describe_services(
                cluster=self.cluster, services=[service_name]
            )

            if not service_response["services"]:
                return False

            task_def_arn = service_response["services"][0]["taskDefinition"]

            # Get task definition details
            task_def_response = self.ecs_client.describe_task_definition(
                taskDefinition=task_def_arn
            )

            task_def = task_def_response["taskDefinition"]

            # Update environment variables
            container_def = task_def["containerDefinitions"][0]
            env_vars = container_def.get("environment", [])

            # Update feature flags
            for flag_name, enabled in feature_flags.items():
                # Remove existing flag
                env_vars = [e for e in env_vars if e["name"] != flag_name]
                # Add updated flag
                env_vars.append({"name": flag_name, "value": "True" if enabled else "False"})

            container_def["environment"] = env_vars

            # Register new task definition
            new_task_def = self.ecs_client.register_task_definition(
                family=task_def["family"],
                containerDefinitions=task_def["containerDefinitions"],
                requiresCompatibilities=task_def["requiresCompatibilities"],
                networkMode=task_def["networkMode"],
                cpu=task_def["cpu"],
                memory=task_def["memory"],
                executionRoleArn=task_def["executionRoleArn"],
                taskRoleArn=task_def.get("taskRoleArn"),
            )

            # Update service to use new task definition
            self.ecs_client.update_service(
                cluster=self.cluster,
                service=service_name,
                taskDefinition=new_task_def["taskDefinition"]["taskDefinitionArn"],
            )

            return True

        except Exception as e:
            print(f"Feature flag update failed: {e}")
            return False

    def _delete_ecs_service(self, service_name: str) -> bool:
        """Delete ECS service (replicate GHA delete-service step)"""
        try:
            # Replicate exact GHA delete-service command with --force
            self.ecs_client.delete_service(cluster=self.cluster, service=service_name, force=True)

            print(f"✅ Deleted ECS service: {service_name}")
            return True

        except Exception as e:
            print(f"❌ ECS service deletion failed: {e}")
            return False

    def _delete_ecr_image(self, image_tag: str) -> bool:
        """Delete ECR image tag (replicate GHA batch-delete-image step)"""
        try:
            # Get registry ID for ECR operations
            ecr_response = self.ecr_client.get_authorization_token()
            registry_id = ecr_response["authorizationData"][0]["proxyEndpoint"]
            registry_id = registry_id.split(".")[0].replace("https://", "")

            # Replicate exact GHA batch-delete-image command
            self.ecr_client.batch_delete_image(
                registryId=registry_id,
                repositoryName=self.repository,
                imageIds=[{"imageTag": image_tag}],
            )

            print(f"✅ Deleted ECR image: {image_tag}")
            return True

        except self.ecr_client.exceptions.ImageNotFoundException:
            print(f"⚠️ ECR image not found: {image_tag} (already deleted)")
            return True  # Consider this success since it's already gone
        except Exception as e:
            print(f"❌ ECR image deletion failed: {e}")
            return False

    def find_expired_services(self, older_than: str) -> List[Dict[str, Any]]:
        """Find ECS services managed by showtime that are expired"""
        import re
        import time

        try:
            # Parse older_than (e.g., "48h", "7d")
            time_match = re.match(r"(\d+)([hd])", older_than)
            if not time_match:
                return []

            hours = int(time_match.group(1))
            if time_match.group(2) == "d":
                hours *= 24

            # cutoff_timestamp = time.time() - (hours * 3600)  # Not used in current implementation
            expired_services = []

            # List all services in cluster
            response = self.ecs_client.list_services(cluster=self.cluster)

            for service_arn in response.get("serviceArns", []):
                service_name = service_arn.split("/")[-1]

                # Only check services that match showtime pattern: pr-{number}-service
                if not service_name.startswith("pr-") or not service_name.endswith("-service"):
                    continue

                try:
                    # Get service tags to check expiration
                    tags_response = self.ecs_client.list_tags_for_resource(resourceArn=service_arn)
                    tags = {tag["key"]: tag["value"] for tag in tags_response.get("tags", [])}

                    # Only process services managed by showtime
                    if tags.get("showtime_managed") != "true":
                        continue

                    # Check if expired
                    expires_timestamp = tags.get("showtime_expires")
                    created_timestamp = tags.get("showtime_created")

                    if expires_timestamp and float(expires_timestamp) < time.time():
                        # Extract PR number from service name: pr-1234-service -> 1234
                        pr_match = re.match(r"pr-(\d+)-service", service_name)
                        pr_number = int(pr_match.group(1)) if pr_match else None

                        age_hours = (
                            (time.time() - float(created_timestamp)) / 3600
                            if created_timestamp
                            else 0
                        )

                        expired_services.append(
                            {
                                "service_name": service_name,
                                "service_arn": service_arn,
                                "pr_number": pr_number,
                                "age_hours": age_hours,
                                "expires_timestamp": expires_timestamp,
                                "tags": tags,
                            }
                        )

                except Exception as e:
                    print(f"⚠️ Could not check service {service_name}: {e}")
                    continue

            return expired_services

        except Exception as e:
            print(f"❌ Failed to find expired services: {e}")
            return []

    def find_showtime_services(self) -> List[str]:
        """Find all ECS services managed by showtime (pr-* pattern)"""
        try:
            # List all services in cluster
            response = self.ecs_client.list_services(cluster=self.cluster)

            if not response.get("serviceArns"):
                return []

            # Extract service names and filter for showtime pattern
            showtime_services = []
            for service_arn in response["serviceArns"]:
                service_name = service_arn.split("/")[-1]  # Extract name from ARN
                if service_name.startswith("pr-") and "-service" in service_name:
                    showtime_services.append(service_name)

            return sorted(showtime_services)

        except Exception as e:
            print(f"❌ Failed to find showtime services: {e}")
            return []

    def _find_pr_services(self, pr_number: int) -> List[Dict[str, Any]]:
        """Find all ECS services for a specific PR"""
        try:
            pr_services = []

            # List all services in cluster
            response = self.ecs_client.list_services(cluster=self.cluster)

            for service_arn in response.get("serviceArns", []):
                service_name = service_arn.split("/")[-1]

                # Check if service matches PR pattern: pr-{number}-{sha}-service
                if service_name.startswith(f"pr-{pr_number}-") and service_name.endswith(
                    "-service"
                ):
                    try:
                        # Get service details
                        service_response = self.ecs_client.describe_services(
                            cluster=self.cluster, services=[service_name]
                        )

                        if service_response["services"]:
                            service = service_response["services"][0]

                            # Extract SHA from service name: pr-1234-abc123f-service -> abc123f
                            sha_match = service_name.replace(f"pr-{pr_number}-", "").replace(
                                "-service", ""
                            )

                            pr_services.append(
                                {
                                    "service_name": service_name,
                                    "service_arn": service_arn,
                                    "sha": sha_match,
                                    "status": service["status"],
                                    "running_count": service["runningCount"],
                                    "desired_count": service["desiredCount"],
                                    "created_at": service["createdAt"],
                                }
                            )

                    except Exception as e:
                        print(f"⚠️ Could not check service {service_name}: {e}")
                        continue

            return pr_services

        except Exception as e:
            print(f"❌ Failed to find PR services: {e}")
            return []

    def _wait_for_service_stability(self, service_name: str, timeout_minutes: int = 10) -> bool:
        """Wait for ECS service to become stable (replicate GHA wait-for-service-stability)"""
        try:
            # Use ECS waiter - same as GHA wait-for-service-stability
            waiter = self.ecs_client.get_waiter("services_stable")
            waiter.wait(
                cluster=self.cluster,
                services=[service_name],
                WaiterConfig={"maxAttempts": timeout_minutes * 2},  # 30s intervals
            )

            print(f"✅ Service {service_name} is stable")
            return True

        except Exception as e:
            print(f"❌ Service stability check failed: {e}")
            return False

    def _health_check_service(self, service_name: str, max_attempts: int = 6) -> bool:
        """Health check service by testing HTTP response"""
        import time

        import httpx

        try:
            # Get service IP
            ip = self.get_environment_ip(service_name)
            if not ip:
                print("❌ Could not get service IP for health check")
                return False

            health_url = f"http://{ip}:8080/health"  # Superset health endpoint
            fallback_url = f"http://{ip}:8080/"  # Fallback to main page

            for attempt in range(max_attempts):
                try:
                    with httpx.Client(timeout=10.0) as client:
                        # Try health endpoint first
                        try:
                            response = client.get(health_url)
                            if response.status_code == 200:
                                print(f"✅ Health check passed on attempt {attempt + 1}")
                                return True
                        except httpx.RequestError:
                            pass

                        # Fallback to main page
                        response = client.get(fallback_url)
                        if response.status_code == 200:
                            print(f"✅ Health check passed (main page) on attempt {attempt + 1}")
                            return True

                except Exception as e:
                    print(f"⚠️ Health check attempt {attempt + 1} failed: {e}")

                if attempt < max_attempts - 1:
                    print("⏳ Waiting 30s before next health check attempt...")
                    time.sleep(30)

            print(f"❌ Health check failed after {max_attempts} attempts")
            return False

        except Exception as e:
            print(f"❌ Health check error: {e}")
            return False

    def _wait_for_service_deletion(self, service_name: str, timeout_minutes: int = 5) -> bool:
        """Wait for ECS service to be fully deleted (not just DRAINING)"""
        import time

        try:
            max_attempts = timeout_minutes * 12  # 5s intervals

            for attempt in range(max_attempts):
                # Check if service still exists in any blocking state (ACTIVE or DRAINING)
                # CRITICAL: Use _service_exists_any_state, not _service_exists, because
                # _service_exists only checks ACTIVE. A DRAINING service still blocks
                # CreateService with "not idempotent" error.
                if not self._service_exists_any_state(service_name):
                    if attempt == 0:
                        print(
                            f"✅ Service {service_name} deletion confirmed (was already draining)"
                        )
                    else:
                        print(f"✅ Service {service_name} fully deleted after {attempt * 5}s")
                    return True

                if attempt % 6 == 0 and attempt > 0:  # Every 30s after first check
                    print(f"⏳ Waiting for service deletion... ({attempt * 5}s elapsed)")

                time.sleep(5)  # Check every 5 seconds

            print(f"⚠️ Service deletion timeout after {timeout_minutes} minutes")
            return False

        except Exception as e:
            print(f"❌ Error waiting for service deletion: {e}")
            return False
