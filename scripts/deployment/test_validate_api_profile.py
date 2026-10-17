import copy
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import validate_api_profile as profile

IMAGE = profile.IMAGE + "@sha256:" + "b" * 64


class ProfileValidationTests(unittest.TestCase):
    def test_release_image_owns_validation_without_network_or_diagnostic_leaks(self):
        raw = '{"private_metadata":"do-not-log"}'
        with patch.object(profile.subprocess, "run", return_value=SimpleNamespace(returncode=0)) as run:
            profile.validate_profile(raw, IMAGE)
        args, kwargs = run.call_args
        self.assertIn("--network=none", args[0])
        self.assertIn(IMAGE, args[0])
        self.assertIn("automatic_evaluation_settings", args[0][-1])
        self.assertNotIn(raw, args[0])
        self.assertEqual(kwargs["input"], raw)
        self.assertTrue(kwargs["capture_output"])

    def test_rejected_or_unavailable_canonical_validator_blocks_rollout(self):
        with patch.object(profile.subprocess, "run", return_value=SimpleNamespace(
                returncode=1, stderr="private raw profile")):
            with self.assertRaisesRegex(RuntimeError, "Release image rejected") as error:
                profile.validate_profile("{}", IMAGE)
        self.assertNotIn("private", str(error.exception))

    def test_missing_value_and_mutable_image_never_start_container(self):
        for raw, image in [(None, IMAGE), ("", IMAGE), ("{}", profile.IMAGE + ":latest")]:
            with self.subTest(raw=raw), patch.object(profile.subprocess, "run") as run:
                with self.assertRaises(RuntimeError):
                    profile.validate_profile(raw, image)
                run.assert_not_called()

    def test_ambiguous_or_secret_backed_profile_rejected_without_secret_access(self):
        for env in [[], [{"name": "PR_EVALUATION_SETTINGS", "valueFrom": {}}],
                    [{"name": "PR_EVALUATION_SETTINGS", "value": "{}"}] * 2]:
            with self.subTest(env=env), self.assertRaises(RuntimeError):
                profile.profile_from_container({"env": env})

    def test_staged_revision_uses_actual_profile_and_checks_digest_before_validation(self):
        revision = {"metadata": {"labels": {"serving.knative.dev/service": "decisionguard-api"}},
                    "spec": {"containers": [{"env": [{"name": "PR_EVALUATION_SETTINGS", "value": "actual"}]}]},
                    "status": {"imageDigest": IMAGE, "conditions": [{"type": "Ready", "status": "True"}]}}
        for fault in (None, "digest", "service", "ready", "containers"):
            with self.subTest(fault=fault):
                candidate = copy.deepcopy(revision)
                if fault == "digest":
                    candidate["status"]["imageDigest"] = "different"
                elif fault == "service":
                    candidate["metadata"]["labels"]["serving.knative.dev/service"] = "different-service"
                elif fault == "ready":
                    candidate["status"]["conditions"] = []
                elif fault == "containers":
                    candidate["spec"]["containers"] *= 2
                with patch("sys.argv", ["validate_api_profile.py", "--image", IMAGE, "--revision", "api-staged"]), \
                     patch.object(profile.subprocess, "run", return_value=SimpleNamespace(
                         returncode=0, stdout=json.dumps(candidate))), \
                     patch.object(profile, "validate_profile") as validate:
                    if fault:
                        with self.assertRaises(RuntimeError):
                            profile.main()
                        validate.assert_not_called()
                    else:
                        profile.main()
                        validate.assert_called_once_with("actual", IMAGE)

    def test_staged_profile_must_match_approved_unrelated_settings_too(self):
        revision = {"metadata": {"labels": {"serving.knative.dev/service": "decisionguard-api"}},
                    "spec": {"containers": [{"env": [{"name": "PR_EVALUATION_SETTINGS",
                                                       "value": '{"max_reads":96}'}]}]},
                    "status": {"imageDigest": IMAGE, "conditions": [{"type": "Ready", "status": "True"}]}}
        for expected in ('{"max_reads":123}', '{ "max_reads" : 96 }'):
            with self.subTest(expected=expected), \
                 patch("sys.argv", ["validate_api_profile.py", "--image", IMAGE, "--revision", "api-staged",
                                    "--expected-profile-file", "approved.json"]), \
                 patch.object(profile.subprocess, "run", return_value=SimpleNamespace(
                     returncode=0, stdout=json.dumps(revision))), \
                 patch.object(profile.Path, "read_text", return_value=expected), \
                 patch.object(profile, "validate_profile") as validate:
                if json.loads(expected)["max_reads"] != 96:
                    with self.assertRaisesRegex(RuntimeError, "differs from the approved"):
                        profile.main()
                    validate.assert_not_called()
                else:
                    profile.main()
                    validate.assert_called_once_with('{"max_reads":96}', IMAGE)


if __name__ == "__main__":
    unittest.main()
