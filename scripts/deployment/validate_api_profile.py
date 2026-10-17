"""Validate production settings with the release image's own admission contract.

No backend imports, provider calls, secret resolution or Cloud Run mutations.
The CLI supports an approved local profile or a staged API revision.
"""

import argparse
import json
from pathlib import Path
import re
import subprocess

IMAGE = "asia-south1-docker.pkg.dev/decisionguard-ai-builder-cup/decisionguard/backend"
VALIDATOR = """
import sys
from ai_services.decision_guard.orchestration.profiles import automatic_evaluation_settings
automatic_evaluation_settings({"K_SERVICE": "decisionguard-api", "PR_EVALUATION_SETTINGS": sys.stdin.read()})
"""


def validate_profile(raw, image):
    if not re.fullmatch(re.escape(IMAGE) + r"@sha256:[a-f0-9]{64}", image):
        raise RuntimeError("Profile validation requires an immutable backend digest")
    if not isinstance(raw, str) or not raw.strip():
        raise RuntimeError("API PR_EVALUATION_SETTINGS is missing; complete the one-time profile rollout")
    result = subprocess.run(
        ["docker", "run", "--rm", "--interactive", "--network=none",
         "--entrypoint=python", image, "-c", VALIDATOR],
        input=raw, capture_output=True, text=True, timeout=120,
    )
    # Never echo input or container diagnostics: profiles may contain private metadata.
    if result.returncode:
        raise RuntimeError("Release image rejected the production API profile; see the regression runbook")


def profile_from_container(container):
    entries = [item for item in container.get("env", []) if item.get("name") == "PR_EVALUATION_SETTINGS"]
    if len(entries) != 1 or "valueFrom" in entries[0] or "value" not in entries[0]:
        raise RuntimeError("API needs one literal PR_EVALUATION_SETTINGS value; secret references are not resolved")
    return entries[0]["value"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--profile-file", type=Path)
    source.add_argument("--revision", help="Validate an existing staged API revision, without changing traffic")
    parser.add_argument("--expected-profile-file", type=Path,
                        help="Require the staged revision to contain exactly this approved JSON object")
    args = parser.parse_args()
    if args.expected_profile_file and not args.revision:
        parser.error("--expected-profile-file requires --revision")
    if args.profile_file:
        raw = args.profile_file.read_text(encoding="utf-8-sig")
    else:
        result = subprocess.run(
            ["gcloud", "run", "revisions", "describe", args.revision,
             "--project=decisionguard-ai-builder-cup", "--region=asia-south1", "--format=json"],
            capture_output=True, text=True, timeout=60,
        )
        if result.returncode:
            raise RuntimeError("Cannot inspect staged API revision (details withheld)")
        revision = json.loads(result.stdout)
        containers = revision["spec"]["containers"]
        status = revision["status"]
        if (revision.get("metadata", {}).get("labels", {}).get("serving.knative.dev/service") != "decisionguard-api"
                or len(containers) != 1 or status.get("imageDigest") != args.image
                or not any(c.get("type") == "Ready" and c.get("status") == "True"
                           for c in status.get("conditions", []))):
            raise RuntimeError("Staged API revision is not ready with the approved digest")
        raw = profile_from_container(containers[0])
        if args.expected_profile_file:
            expected = args.expected_profile_file.read_text(encoding="utf-8-sig")
            if json.loads(raw) != json.loads(expected):
                raise RuntimeError("Staged API profile differs from the approved profile file")
    validate_profile(raw, args.image)
    print("Production API profile validated by the release image")


if __name__ == "__main__":
    main()
