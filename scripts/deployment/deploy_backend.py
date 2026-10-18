"""Image-only Cloud Run rollout. Only main() performs external operations."""

import copy
import json
import os
from pathlib import Path
import re
import subprocess
import time
from urllib.request import urlopen

from validate_api_profile import profile_from_container, validate_profile

PROJECT = "decisionguard-ai-builder-cup"
REGION = "asia-south1"
IMAGE = f"{REGION}-docker.pkg.dev/{PROJECT}/decisionguard/backend"
API = "decisionguard-api"
WORKER = "decisionguard-pr-evaluation-worker"
REVISION_WAIT_SECONDS = 600
TRAFFIC_WAIT_SECONDS = 120
POLL_SECONDS = 5


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
    # gcloud may rotate its client nonce when producing a fresh Job execution
    # template or a Service revision. This is not user runtime configuration.
    # Only normalize this exact label; keep all other labels strict.
    metadata = result["template"].get("metadata", {})
    labels = metadata.get("labels", {})
    labels.pop("client.knative.dev/nonce", None)
    if not labels:
        metadata.pop("labels", None)
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


def differing_paths(before, after, prefix=""):
    """Return changed JSON paths only. Never print values (env may contain secrets)."""
    if isinstance(before, dict) and isinstance(after, dict):
        result = []
        for key in sorted(before.keys() | after.keys()):
            path = f"{prefix}.{key}" if prefix else str(key)
            if key not in before or key not in after:
                result.append(path)
            else:
                result.extend(differing_paths(before[key], after[key], path))
        return result
    if isinstance(before, list) and isinstance(after, list):
        result = []
        for index in range(max(len(before), len(after))):
            path = f"{prefix}[{index}]"
            if index >= len(before) or index >= len(after):
                result.append(path)
            else:
                result.extend(differing_paths(before[index], after[index], path))
        return result
    return [] if before == after else [prefix]


def assert_runtime_unchanged(before, after, kind, name):
    old_config = runtime_config(before, kind)
    new_config = runtime_config(after, kind)
    if old_config != new_config:
        paths = []
        for section, old_section, new_section in zip(
            ("spec", "annotations"), old_config, new_config
        ):
            paths.extend(differing_paths(old_section, new_section, section))
        # A path may contain environment NAME keys only as list indices; never
        # log values or serialized resource details.
        raise RuntimeError(
            f"{name} configuration changed unexpectedly; changed paths: "
            + ", ".join(paths[:25])
            + (f" (+{len(paths)-25} more)" if len(paths) > 25 else "")
        )


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


def wait_for_new_revision(service_name, old_revision, image, baseline,
                          timeout=REVISION_WAIT_SECONDS):
    """Wait for Cloud Run Service status to expose the just-created staged revision."""
    deadline = time.monotonic() + timeout
    while True:
        current = gcloud("run", "services", "describe", service_name)
        revision = current.get("status", {}).get("latestCreatedRevisionName")
        if revision and revision != old_revision:
            assert_runtime_unchanged(baseline, current, "services", service_name)
            require(single_container(current, "services")["image"] == image,
                    f"{service_name} staged image mismatch")
            return revision, current
        if time.monotonic() >= deadline:
            raise RuntimeError(
                f"{service_name} no new revision observed within {timeout}s; "
                "inspect Cloud Run revisions before retrying")
        time.sleep(POLL_SECONDS)


def wait_for_job_ready(job_name, image, baseline, timeout=REVISION_WAIT_SECONDS):
    """Wait for job template propagation without executing the Cloud Run Job."""
    deadline = time.monotonic() + timeout
    while True:
        current = gcloud("run", "jobs", "describe", job_name)
        assert_runtime_unchanged(baseline, current, "jobs", job_name)
        require(single_container(current, "jobs")["image"] == image,
                f"{job_name} job template image mismatch")
        if ready(current):
            return current
        if time.monotonic() >= deadline:
            raise RuntimeError(f"{job_name} template not Ready within {timeout}s")
        time.sleep(POLL_SECONDS)


def wait_for_staged_revision(service_name, revision, image, baseline, timeout=REVISION_WAIT_SECONDS):
    """Verify the revision itself; latestReadyRevisionName may still be the serving revision.

    --no-traffic deliberately keeps the old revision serving. A Service Ready
    condition or latestReadyRevisionName alone is not evidence the new revision
    is ready, and using --to-latest before inspecting the revision is unsafe.
    """
    deadline = time.monotonic() + timeout
    while True:
        current = gcloud("run", "services", "describe", service_name)
        require(current.get("status", {}).get("latestCreatedRevisionName") == revision,
                f"{service_name} latest created revision changed during rollout")
        assert_runtime_unchanged(baseline, current, "services", service_name)
        require(single_container(current, "services")["image"] == image,
                f"{service_name} staged image mismatch")
        rev = gcloud("run", "revisions", "describe", revision)
        if ready(rev):
            require(rev.get("status", {}).get("imageDigest") in (image, image.split("@")[-1]),
                    f"{service_name} staged revision digest mismatch")
            return current
        if time.monotonic() >= deadline:
            raise RuntimeError(
                f"{service_name} staged revision {revision} not Ready within {timeout}s; "
                "inspect revision conditions; no traffic was promoted")
        time.sleep(POLL_SECONDS)


def wait_for_traffic(service_name, revision, timeout=TRAFFIC_WAIT_SECONDS):
    """Wait for Route reconciliation; never count a staged revision as deployed."""
    deadline = time.monotonic() + timeout
    while True:
        current = gcloud("run", "services", "describe", service_name)
        require(current.get("status", {}).get("latestCreatedRevisionName") == revision,
                f"{service_name} latest created revision changed during promotion")
        traffic = current.get("status", {}).get("traffic", [])
        if (ready(current) and len(traffic) == 1
                and traffic[0].get("revisionName") == revision
                and traffic[0].get("percent") == 100
                and not traffic[0].get("tag")):
            return current
        if time.monotonic() >= deadline:
            raise RuntimeError(
                f"{service_name} traffic did not reach {revision}=100 within {timeout}s; "
                "inspect traffic and reconcile manually")
        time.sleep(POLL_SECONDS)


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
        if name == API:
            validate_profile(profile_from_container(container), image)
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
        # Do not churn Cloud Run templates/revisions that already use this digest.
        # Preflight has established single ready, fully-served service revisions.
        already_deployed = record["previous_image"] == image
        if already_deployed:
            record["state"] = "image-verified"
            record["skipped_unchanged_image"] = True
            if kind == "services":
                record["revision"] = record["previous_revision"]
            save()
            if name == API:
                check_health(baseline[name]["status"]["url"])
                record["health"] = "ok"
            record["state"] = "verified"
            save()
            print(f"Verified (unchanged image) {name}: {image}")
            continue
        record["state"] = "updating"
        save()
        if kind == "services":
            gcloud("run", "services", "update", name, "--image", image, "--no-traffic")
        else:
            gcloud("run", "jobs", "update", name, "--image", image)
        if kind == "services":
            revision, current = wait_for_new_revision(
                name, record["previous_revision"], image, baseline[name])
        else:
            current = gcloud("run", kind, "describe", name)
            assert_runtime_unchanged(baseline[name], current, kind, name)
            require(single_container(current, kind)["image"] == image,
                    f"{name} image mismatch")
        if kind == "services":
            record["revision"] = revision
            record["state"] = "staged"
            save()
            current = wait_for_staged_revision(name, revision, image, baseline[name])
            if name == API:
                validate_profile(profile_from_container(single_container(current, kind)), image)
            record["state"] = "image-verified"
            save()
            # Pin promotion to the exact revision validated above, not an alias
            # that could select a newer revision after another deployment.
            gcloud("run", "services", "update-traffic", name,
                   "--to-revisions", f"{revision}=100")
            current = wait_for_traffic(name, revision)
            assert_runtime_unchanged(baseline[name], current, kind, name)
            require(revision_digest(revision) == image, f"{name} promoted digest mismatch")
        else:
            current = wait_for_job_ready(name, image, baseline[name])
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
    targets = []
    for flag, kind, name in (
        ("DEPLOY_ADR_INGESTION", "jobs", "decisionguard-adr-ingestion"),
        ("DEPLOY_REPOSITORY_INGESTION", "jobs", "decisionguard-repository-ingestion"),
        ("DEPLOY_RECOVERY", "services", "decisionguard-processing-recovery"),
    ):
        if os.environ.get(flag) == "true":
            targets.append((kind, name))
    targets.extend([("services", WORKER), ("services", API)])
    rollout(os.environ["DEPLOY_IMAGE"], targets, Path("backend-release.json"))


if __name__ == "__main__":
    main()
