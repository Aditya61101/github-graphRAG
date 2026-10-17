"""Image-only Cloud Run rollout. Only main() performs external operations."""

import copy
import json
import os
from pathlib import Path
import re
import subprocess
import time
from urllib.request import urlopen

PROJECT = "decisionguard-ai-builder-cup"
REGION = "asia-south1"
IMAGE = f"{REGION}-docker.pkg.dev/{PROJECT}/decisionguard/backend"
API = "decisionguard-api"
WORKER = "decisionguard-pr-evaluation-worker"


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def gcloud(*args):
    # Capture output: describe responses can contain plaintext environment values.
    result = subprocess.run(
        ["gcloud", *args, "--project", PROJECT, "--region", REGION,
         "--quiet", "--format=json"],
        capture_output=True, text=True, timeout=900,
    )
    require(result.returncode == 0, f"gcloud {' '.join(args[:4])} failed (details withheld)")
    return json.loads(result.stdout or "{}")


def template(resource, kind):
    value = resource["spec"]["template"]
    return value if kind == "services" else value["spec"]["template"]


def single_container(resource, kind):
    containers = template(resource, kind)["spec"]["containers"]
    require(len(containers) == 1, "Expected exactly one container; refusing ambiguous image update")
    return containers[0]


def runtime_config(resource, kind):
    """Compare all spec and annotations except image and server/client bookkeeping."""
    result = copy.deepcopy(resource["spec"])
    if kind == "services":
        result.pop("traffic", None)  # Traffic intentionally moves to the ready revision.
    target = result["template"]
    if kind == "jobs":
        target = target["spec"]["template"]
    target["spec"]["containers"][0].pop("image", None)

    def clean(value):
        if isinstance(value, dict):
            metadata = value.get("metadata", {})
            metadata.pop("name", None)  # Server-assigned revision name.
            for key in ("run.googleapis.com/client-name", "run.googleapis.com/client-version",
                        "run.googleapis.com/operation-id", "client.knative.dev/user-image"):
                metadata.get("annotations", {}).pop(key, None)
            for child in value.values():
                clean(child)
        elif isinstance(value, list):
            for child in value:
                clean(child)

    clean(result)
    # Service-level annotations include ingress, scaling and IAM check settings.
    annotations = copy.deepcopy(resource.get("metadata", {}).get("annotations", {}))
    for key in list(annotations):
        if key.startswith(("serving.knative.dev/", "client.knative.dev/")) or key in {
            "run.googleapis.com/client-name", "run.googleapis.com/client-version",
            "run.googleapis.com/operation-id", "run.googleapis.com/lastModifier",
            "run.googleapis.com/creator", "run.googleapis.com/urls",
        }:
            del annotations[key]
    return result, annotations


def ready(resource):
    return any(c.get("type") == "Ready" and str(c.get("status")).lower() == "true"
               for c in resource.get("status", {}).get("conditions", []))


def serving_revision(resource):
    traffic = resource.get("status", {}).get("traffic", [])
    require(len(traffic) == 1 and traffic[0].get("percent") == 100
            and not traffic[0].get("tag"),
            "Expected one untagged revision serving 100% traffic; reconcile traffic manually")
    return traffic[0]["revisionName"]


def revision_digest(revision):
    digest = gcloud("run", "revisions", "describe", revision)["status"]["imageDigest"]
    return digest if "@" in digest else f"{IMAGE}@{digest}"


def check_health(url):
    require(url.startswith("https://"), "API URL must use HTTPS")
    for attempt in range(6):
        try:
            with urlopen(f"{url.rstrip('/')}/health", timeout=20) as response:
                if response.status == 200 and json.load(response).get("status") == "ok":
                    return
        except (OSError, ValueError):
            pass
        if attempt < 5:
            time.sleep(5)
    raise RuntimeError("API /health did not return HTTP 200 with status=ok")


def rollout(image, targets, manifest_path):
    require(re.fullmatch(re.escape(IMAGE) + r"@sha256:[a-f0-9]{64}", image),
            "Deployment requires an immutable backend image digest")
    baseline = {}
    manifest = {"commit": os.environ.get("GITHUB_SHA"), "image": image, "targets": []}

    def save():
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    # Preflight EVERY selected target before the first mutation; no create operations.
    for kind, name in targets:
        resource = gcloud("run", kind, "describe", name)
        container = single_container(resource, kind)
        require(ready(resource), f"{name} is not ready before rollout")
        record = {"kind": kind, "name": name, "state": "pending"}
        if kind == "services":
            revision = serving_revision(resource)
            record.update(previous_revision=revision, previous_image=revision_digest(revision))
            require(resource["status"].get("latestReadyRevisionName") == revision
                    and resource["status"].get("latestCreatedRevisionName") == revision,
                    f"{name} has an unserved or unready latest revision; reconcile it first")
        else:
            # Resolve historical tags now so rollback references are immutable too.
            old_image = container["image"]
            if "@sha256:" not in old_image:
                result = subprocess.run(
                    ["gcloud", "artifacts", "docker", "images", "describe", old_image,
                     "--project", PROJECT, "--format=value(image_summary.digest)"],
                    capture_output=True, text=True, timeout=60, check=True,
                )
                digest = result.stdout.strip()
                require(re.fullmatch(r"sha256:[a-f0-9]{64}", digest), "Invalid previous job digest")
                old_image = f"{old_image.rsplit(':', 1)[0]}@{digest}"
            record["previous_image"] = old_image
        baseline[name] = resource
        manifest["targets"].append(record)
        save()

    for record in manifest["targets"]:
        kind, name = record["kind"], record["name"]
        # Detect out-of-band configuration changes since preflight.
        require(gcloud("run", kind, "describe", name) == baseline[name],
                f"{name} changed during preflight; rerun after reconciling changes")
        record["state"] = "updating"
        save()
        gcloud("run", kind, "update", name, "--image", image)
        current = gcloud("run", kind, "describe", name)
        require(ready(current), f"{name} is not ready after update")
        require(runtime_config(current, kind) == runtime_config(baseline[name], kind),
                f"{name} configuration changed unexpectedly; stop and inspect")
        require(single_container(current, kind)["image"] == image, f"{name} image mismatch")
        if kind == "services":
            revision = current["status"]["latestCreatedRevisionName"]
            require(current["status"].get("latestReadyRevisionName") == revision,
                    f"{name} latest revision is not ready")
            require(revision_digest(revision) == image, f"{name} resolved digest mismatch")
            gcloud("run", "services", "update-traffic", name, "--to-latest")
            current = gcloud("run", kind, "describe", name)
            require(serving_revision(current) == revision, f"{name} traffic mismatch")
            record["revision"] = revision
        record["state"] = "image-verified"
        save()
        if name == API:
            check_health(current["status"]["url"])
            record["health"] = "ok"
        record["state"] = "verified"
        save()
        print(f"Verified {name}: {image}")


def main():
    require(os.environ.get("GITHUB_REF") == "refs/heads/integration/production",
            "Only integration/production may deploy")
    targets = [("services", WORKER), ("services", API)]
    for flag, kind, name in (
        ("DEPLOY_RECOVERY", "services", "decisionguard-processing-recovery"),
        ("DEPLOY_REPOSITORY_INGESTION", "jobs", "decisionguard-repository-ingestion"),
        ("DEPLOY_ADR_INGESTION", "jobs", "decisionguard-adr-ingestion"),
    ):
        if os.environ.get(flag) == "true":
            targets.append((kind, name))
    rollout(os.environ["DEPLOY_IMAGE"], targets, Path("backend-release.json"))


if __name__ == "__main__":
    main()
