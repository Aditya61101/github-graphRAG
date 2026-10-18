"""Offline configuration admission for intentionally reset existing resources."""
import hashlib
import json

import pytest

from shared.ai.provider import ProviderConfig
from ai_services.tests.test_google_migration import google_env


def fresh_env():
    env = google_env()
    env.pop("AI_ISOLATED_DATA_ID")
    env.pop("AI_DATA_ISOLATION_CONFIRMED")
    return dict(env, AI_DATA_MODE="fresh-reset", AI_INSTALLATION_ID="reset-20261018-a",
                AI_DATA_RESET_CONFIRMED="true")


def test_reset_existing_resources_without_claiming_isolation():
    config = ProviderConfig.from_env(fresh_env())
    assert config.data_mode == "fresh-reset"
    assert config.isolated_data_id == ""
    assert config.installation_id == "reset-20261018-a"
    assert config.identity != ProviderConfig.from_env(
        dict(fresh_env(), AI_INSTALLATION_ID="reset-20261018-b")).identity
    from ai_services.decision_guard.hybrid.models import google_manifest
    manifest = google_manifest(config)
    assert manifest.confirmation_source == "fresh-reset-google-config:reset-20261018-a"
    assert manifest.configuration_id == config.identity


@pytest.mark.parametrize("changes", [
    {"AI_DATA_RESET_CONFIRMED": "false"}, {"AI_INSTALLATION_ID": " "},
    {"AI_DATA_ISOLATION_CONFIRMED": "true"}, {"AI_ISOLATED_DATA_ID": "old"},
    {"AI_DATA_MODE": "reuse"}, {"AI_DATA_MODE": "isolated"},
])
def test_incomplete_or_conflicting_reset_attestation_fails_closed(changes):
    with pytest.raises(ValueError):
        ProviderConfig.from_env(dict(fresh_env(), **changes))


def test_isolated_identity_remains_compatible():
    config = ProviderConfig.from_env(google_env())
    old_fields = {k: v for k, v in config.__dict__.items()
                  if k not in {"data_mode", "installation_id"}}
    assert config.identity == hashlib.sha256(
        json.dumps(old_fields, sort_keys=True).encode()).hexdigest()


def test_reset_fields_cannot_silently_apply_to_isolated_mode():
    for addition in ({"AI_INSTALLATION_ID": "reset"}, {"AI_DATA_RESET_CONFIRMED": "true"}):
        with pytest.raises(ValueError):
            ProviderConfig.from_env(dict(google_env(), **addition))
