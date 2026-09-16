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

"""Shared rendering for the packaged ECS and runner-smoke configuration."""

import json
import re
from pathlib import Path
from typing import Any, Dict, Optional, Sequence

IMAGE_REFERENCE_PATTERN = re.compile(r"^apache/superset@sha256:[0-9a-f]{64}$")


def validate_image_reference(image_reference: str) -> str:
    """Require the immutable manifest reference produced by the managed build."""
    if not IMAGE_REFERENCE_PATTERN.fullmatch(image_reference):
        raise ValueError("image reference must be an apache/superset sha256 manifest digest")
    return image_reference


def load_packaged_task_definition() -> Dict[str, Any]:
    """Load a fresh copy of the packaged ECS task definition."""
    path = Path(__file__).parent.parent / "data" / "ecs-task-definition.json"
    with path.open() as stream:
        task_definition = json.load(stream)
    if not isinstance(task_definition, dict):
        raise ValueError("packaged task definition must be an object")
    return task_definition


def render_task_definition(
    image_reference: str,
    feature_flags: Optional[Sequence[Dict[str, str]]] = None,
) -> Dict[str, Any]:
    """Render one deterministic definition for local smoke and ECS registration."""
    task_definition = load_packaged_task_definition()
    containers = task_definition.get("containerDefinitions")
    if not isinstance(containers, list) or len(containers) != 1:
        raise ValueError("packaged task definition must contain exactly one container")
    container = containers[0]
    if not isinstance(container, dict):
        raise ValueError("packaged container definition must be an object")

    container["image"] = image_reference
    entries = list(container.get("environment", [])) + list(feature_flags or [])
    environment: Dict[str, str] = {}
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("name"), str):
            raise ValueError("environment entries must contain a string name")
        environment[entry["name"]] = str(entry.get("value", ""))
    container["environment"] = [
        {"name": name, "value": environment[name]} for name in sorted(environment)
    ]
    return task_definition


def rendered_container(task_definition: Dict[str, Any]) -> Dict[str, Any]:
    """Return the single validated rendered container."""
    containers = task_definition.get("containerDefinitions")
    if not isinstance(containers, list) or len(containers) != 1:
        raise ValueError("rendered task definition must contain exactly one container")
    container = containers[0]
    if not isinstance(container, dict):
        raise ValueError("rendered container definition must be an object")
    return container
