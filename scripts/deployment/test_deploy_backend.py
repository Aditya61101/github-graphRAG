"""Offline rollout contract tests: no credentials, network or gcloud required."""

import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import deploy_backend as deploy

OLD = deploy.IMAGE + "@sha256:" + "a" * 64
NEW = deploy.IMAGE + "@sha256:" + "b" * 64
RECOVERY = "decisionguard-processing-recovery"
JOB = "decisionguard-repository-ingestion"
ADR_JOB = "decisionguard-adr-ingestion"
PROFILE = '{"token_encoding":"o200k_base"}'  # Opaque input; image owns schema validation.


def resource(name, kind="services"):
    task = {"metadata": {"annotations": {"run.googleapis.com/cloudsql-instances": "project:region:db"}},
            "spec": {"serviceAccountName": "runtime@example.iam.gserviceaccount.com",
                     "timeoutSeconds": 3600, "containerConcurrency": 1,
                     "containers": [{"image": OLD, "command": ["python"], "args": ["worker.py"],
                                     "env": [{"name": "BUDGET", "value": "123"},
                                             {"name": "SECRET", "valueFrom": {"secretKeyRef": {
                                                 "name": "existing-secret", "key": "latest"}}}],
                                     "resources": {"limits": {"cpu": "2", "memory": "2Gi"}}}]}}
    result = {"metadata": {"annotations": {"run.googleapis.com/ingress": "all"}},
              "spec": {"template": task},
              "status": {"conditions": [{"type": "Ready", "status": "True"}]}}
    if kind == "jobs":
        result["spec"]["template"] = {"spec": {"taskCount": 1, "template": task}}
    else:
        if name == deploy.API:
            task["spec"]["containers"][0]["env"].append({"name": "PR_EVALUATION_SETTINGS", "value": PROFILE})
        result["spec"]["traffic"] = [{"latestRevision": True, "percent": 100}]
        result["status"].update(latestCreatedRevisionName=name + "-old",
                                latestReadyRevisionName=name + "-old",
                                traffic=[{"revisionName": name + "-old", "percent": 100}],
                                url="https://example.run.app")
    return result


def realistic_job():
    """Sanitized v1 Job shape matching the audited production resource."""
    value = resource(ADR_JOB, "jobs")
    value.update(apiVersion="run.googleapis.com/v1", kind="Job")
    value["metadata"].update(generation=4, resourceVersion="old-version")
    execution = value["spec"]["template"]
    execution["metadata"] = {
        "annotations": {"run.googleapis.com/client-name": "gcloud",
                        "run.googleapis.com/client-version": "589.0.0",
                        "run.googleapis.com/cloudsql-instances": "project:region:db",
                        "run.googleapis.com/execution-environment": "gen2"},
        "labels": {"client.knative.dev/nonce": "kaz_fes_rog"}}
    execution["spec"].update(parallelism=1, taskCount=1)
    task = execution["spec"]["template"]["spec"]
    task.pop("containerConcurrency")  # Jobs use task count and parallelism.
    task.update(maxRetries=0, timeoutSeconds="3600", volumes=[{
        "name": "github-key", "secret": {"secretName": "github-private-key",
        "items": [{"key": "2", "path": "github-app.pem"}]}}])
    container = task["containers"][0]
    container.update(command=["python"], args=["-m", "ai_services.runs.run_adr_ingestion"],
        volumeMounts=[{"name": "github-key", "mountPath": "/run/secrets"}])
    container["resources"]["limits"] = {"cpu": "2", "memory": "4Gi"}
    return value


class RolloutTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.manifest = Path(self.temp.name) / "release.json"
        self.targets = [("services", deploy.WORKER), ("services", deploy.API),
                        ("services", RECOVERY), ("jobs", JOB), ("jobs", ADR_JOB)]
        self.resources = {name: resource(name, kind) for kind, name in self.targets}
        self.calls = []
        self.failure = None
        self.drift = False
        self.bad_digest = False

    def fake_gcloud(self, *args):
        self.calls.append(args)
        _, kind, action, name, *flags = args
        if kind == "revisions":
            digest = OLD if name.endswith("-old") or self.bad_digest else NEW
            return {"status": {"imageDigest": digest}}
        value = self.resources[name]
        if action == "update":
            if name == self.failure:
                raise RuntimeError("deployment failed")
            self.assertEqual(flags, ["--image", NEW])
            deploy.single_container(value, kind)["image"] = NEW
            if self.drift:
                deploy.single_container(value, kind)["env"] = []
            if kind == "services":
                value["status"].update(latestCreatedRevisionName=name + "-new",
                                        latestReadyRevisionName=name + "-new")
        elif action == "update-traffic":
            self.assertEqual(flags, ["--to-latest"])
            value["status"]["traffic"] = [{"revisionName": name + "-new", "percent": 100}]
        return copy.deepcopy(value)

    def run_rollout(self, health=None, profile_error=None):
        with patch.object(deploy, "gcloud", side_effect=self.fake_gcloud), \
             patch.object(deploy, "validate_profile", side_effect=profile_error) as validate, \
             patch.object(deploy, "check_health", side_effect=health) as check:
            deploy.rollout(NEW, self.targets, self.manifest)
        self.assertEqual(validate.call_count, 2)
        validate.assert_called_with(PROFILE, NEW)
        return check

    def mutations(self):
        return [call for call in self.calls if call[2] in ("update", "update-traffic")]

    def test_all_targets_share_digest_and_preserve_runtime_settings(self):
        before = copy.deepcopy(self.resources)
        health = self.run_rollout()
        self.assertEqual([call[3] for call in self.calls if call[2] == "update"],
                         [name for _, name in self.targets])
        health.assert_called_once_with("https://example.run.app")
        for kind, name in self.targets:
            self.assertEqual(deploy.runtime_config(before[name], kind),
                             deploy.runtime_config(self.resources[name], kind))
        manifest = json.loads(self.manifest.read_text())
        self.assertTrue(all(t["state"] == "verified" and t["previous_image"] == OLD
                            for t in manifest["targets"]))
        self.assertFalse(any("execute" in call for call in self.calls))

    def test_worker_failure_stops_api(self):
        self.failure = deploy.WORKER
        with self.assertRaisesRegex(RuntimeError, "deployment failed"):
            self.run_rollout()
        self.assertEqual(len(self.mutations()), 1)
        self.assertEqual(deploy.single_container(self.resources[deploy.API], "services")["image"], OLD)

    def test_missing_api_profile_stops_before_worker_update(self):
        container = deploy.single_container(self.resources[deploy.API], "services")
        container["env"] = [e for e in container["env"] if e["name"] != "PR_EVALUATION_SETTINGS"]
        with self.assertRaisesRegex(RuntimeError, "PR_EVALUATION_SETTINGS"):
            self.run_rollout()
        self.assertEqual(self.mutations(), [])

    def test_invalid_api_profile_stops_before_worker_update(self):
        with self.assertRaisesRegex(RuntimeError, "profile rejected"):
            self.run_rollout(profile_error=RuntimeError("profile rejected"))
        self.assertEqual(self.mutations(), [])

    def test_preflight_checks_optional_targets_before_any_update(self):
        self.resources[JOB]["status"]["conditions"] = []
        with self.assertRaisesRegex(RuntimeError, "not ready before"):
            self.run_rollout()
        self.assertEqual(self.mutations(), [])

    def test_rejects_split_traffic_and_sidecars(self):
        for sidecar in (False, True):
            with self.subTest(sidecar=sidecar):
                self.setUp()
                value = self.resources[deploy.API]
                if sidecar:
                    value["spec"]["template"]["spec"]["containers"].append({"image": "sidecar"})
                else:
                    value["status"]["traffic"][0]["percent"] = 50
                with self.assertRaises(RuntimeError):
                    self.run_rollout()
                self.assertEqual(self.mutations(), [])

    def test_config_drift_and_digest_mismatch_stop_rollout(self):
        for flag in ("drift", "bad_digest"):
            with self.subTest(flag=flag):
                self.setUp()
                setattr(self, flag, True)
                with self.assertRaises(RuntimeError):
                    self.run_rollout()
                self.assertEqual(len(self.mutations()), 1)

    def test_health_failure_stops_optional_updates(self):
        with self.assertRaisesRegex(RuntimeError, "health failure"):
            self.run_rollout(RuntimeError("health failure"))
        self.assertFalse(any(call[3] in (RECOVERY, JOB, ADR_JOB) for call in self.mutations()))
        api_record = json.loads(self.manifest.read_text())["targets"][1]
        self.assertEqual(api_record["state"], "image-verified")
        self.assertNotIn("health", api_record)

    def test_optional_targets_default_off(self):
        with patch.dict(deploy.os.environ, {
            "GITHUB_REF": "refs/heads/integration/production", "DEPLOY_IMAGE": NEW,
            "DEPLOY_RECOVERY": "false", "DEPLOY_REPOSITORY_INGESTION": "false",
            "DEPLOY_ADR_INGESTION": "false",
        }), patch.object(deploy, "rollout") as rollout:
            deploy.main()
        self.assertEqual(rollout.call_args.args[1], self.targets[:2])

    def test_all_optional_flags_select_same_release(self):
        with patch.dict(deploy.os.environ, {
            "GITHUB_REF": "refs/heads/integration/production", "DEPLOY_IMAGE": NEW,
            "DEPLOY_RECOVERY": "true", "DEPLOY_REPOSITORY_INGESTION": "true",
            "DEPLOY_ADR_INGESTION": "true",
        }), patch.object(deploy, "rollout") as rollout:
            deploy.main()
        self.assertEqual(rollout.call_args.args[:2], (NEW, [
            ("jobs", ADR_JOB), ("jobs", JOB), ("services", RECOVERY),
            ("services", deploy.WORKER), ("services", deploy.API)]))

    def test_mutable_image_rejected_before_gcloud(self):
        with patch.object(deploy, "gcloud") as cloud, self.assertRaises(RuntimeError):
            deploy.rollout(deploy.IMAGE + ":latest", self.targets, self.manifest)
        cloud.assert_not_called()

    def test_selected_adr_consumer_failure_prevents_api_update(self):
        self.targets = [("jobs", ADR_JOB), ("jobs", JOB), ("services", RECOVERY),
                        ("services", deploy.WORKER), ("services", deploy.API)]
        self.failure = ADR_JOB
        with self.assertRaisesRegex(RuntimeError, "deployment failed"):
            self.run_rollout()
        self.assertEqual([call[3] for call in self.mutations()], [ADR_JOB])
        self.assertEqual(deploy.single_container(self.resources[deploy.API], "services")["image"], OLD)

    def test_audited_job_nonce_rotation_preserves_runtime_config(self):
        before = realistic_job()
        after = copy.deepcopy(before)
        deploy.single_container(after, "jobs")["image"] = NEW
        after["spec"]["template"]["metadata"]["labels"]["client.knative.dev/nonce"] = "fov_rig_quk"
        after["spec"]["template"]["metadata"]["annotations"]["run.googleapis.com/client-version"] = "568.0.0"
        after["metadata"].update(generation=5, resourceVersion="new-version")
        after["metadata"]["annotations"].update({
            "run.googleapis.com/lastModifier": "deployer@example.iam.gserviceaccount.com",
            "run.googleapis.com/operation-id": "new-operation"})
        saved = copy.deepcopy((before, after))
        self.assertEqual(deploy.runtime_config(before, "jobs"), deploy.runtime_config(after, "jobs"))
        self.assertEqual((before, after), saved)  # Comparator must not mutate inputs.
        del before["spec"]["template"]["metadata"]["labels"]
        self.assertEqual(deploy.runtime_config(before, "jobs"), deploy.runtime_config(after, "jobs"))

    def test_job_runtime_changes_remain_strict(self):
        before = realistic_job()
        paths = [
            ("spec", "template", "spec", "parallelism"),
            ("spec", "template", "spec", "taskCount"),
            ("spec", "template", "metadata", "labels", "owner"),
            ("spec", "template", "metadata", "annotations", "run.googleapis.com/cloudsql-instances"),
            ("spec", "template", "metadata", "annotations", "run.googleapis.com/vpc-access-connector"),
            ("spec", "template", "metadata", "annotations", "run.googleapis.com/vpc-access-egress"),
        ]
        task = ("spec", "template", "spec", "template", "spec")
        paths += [task + (key,) for key in ("serviceAccountName", "maxRetries", "timeoutSeconds", "volumes")]
        container = task + ("containers", 0)
        paths += [container + (key,) for key in ("command", "args", "env", "resources", "volumeMounts")]
        paths += [container + ("env", 1, "valueFrom", "secretKeyRef", "key")]
        for path in paths:
            with self.subTest(path=path):
                after = copy.deepcopy(before)
                cursor = after
                for key in path[:-1]:
                    cursor = cursor[key]
                cursor[path[-1]] = "changed"
                self.assertNotEqual(deploy.runtime_config(before, "jobs"), deploy.runtime_config(after, "jobs"))

    def test_already_updated_job_can_be_redeployed_with_new_nonce(self):
        self.targets = [("jobs", ADR_JOB)]
        self.resources[ADR_JOB] = realistic_job()
        deploy.single_container(self.resources[ADR_JOB], "jobs")["image"] = NEW
        original = self.fake_gcloud
        def cloud(*args):
            if args[2] == "update":
                self.resources[ADR_JOB]["spec"]["template"]["metadata"]["labels"]["client.knative.dev/nonce"] = "another_nonce"
            return original(*args)
        with patch.object(deploy, "gcloud", side_effect=cloud):
            deploy.rollout(NEW, self.targets, self.manifest)
        record = json.loads(self.manifest.read_text())["targets"][0]
        self.assertEqual(record["previous_image"], NEW)
        self.assertEqual(record["state"], "verified")
        self.assertFalse(any("execute" in call for call in self.calls))

    def test_nonce_exception_does_not_hide_service_concurrency_or_labels(self):
        before = resource(deploy.WORKER)
        for field in ("concurrency", "nonce"):
            with self.subTest(field=field):
                after = copy.deepcopy(before)
                if field == "concurrency":
                    after["spec"]["template"]["spec"]["containerConcurrency"] = 20
                else:
                    after["spec"]["template"]["metadata"]["labels"] = {
                        "client.knative.dev/nonce": "service-label"}
                self.assertNotEqual(deploy.runtime_config(before, "services"),
                                    deploy.runtime_config(after, "services"))

    def test_wrong_branch_rejected_before_external_operations(self):
        with patch.dict(deploy.os.environ, {"GITHUB_REF": "refs/heads/feature/backend-cicd"}), \
             patch.object(deploy, "rollout") as rollout, self.assertRaises(RuntimeError):
            deploy.main()
        rollout.assert_not_called()


if __name__ == "__main__":
    unittest.main()
