"""Provider-specific settings resolved only when the API creates evaluations.

Workers and recovery use the job's persisted settings, never process defaults.
Keep development defaults in EvaluationSettings independent of Azure policy.
"""

import os
import json

from pydantic import ValidationError

from ai_services.decision_guard.evaluation.models import EvaluationSettings


class EvaluationConfigurationError(RuntimeError):
    """Automatic evaluations cannot be admitted with this configuration."""


AZURE_PRODUCTION_PROFILE = {
    "token_encoding": "o200k_base",
    "profile": "DEFAULT",
    "max_attempts": 43,
    "max_tokens": 1075000,
    "deadline_seconds": 1500.0,
    "concurrency": 2,
    "max_input_tokens": 20000,
    "max_output_tokens": 5000,
    "model_window": 32768,
    "provider_reserve": 1024,
}


GEMINI_PRODUCTION_PROFILE = {
    "provider": "google", "evaluation_profile": "gemini-v1",
    "token_encoding": "gemini-utf8-bound-v1", "profile": "DEFAULT",
    "max_attempts": 43, "max_tokens": 2150000, "deadline_seconds": 1500.0,
    "concurrency": 2, "max_input_tokens": 80000, "max_output_tokens": 16384,
    "model_window": 131072, "provider_reserve": 4096, "call_timeout_seconds": 120.0,
}


def azure_production_settings():
    """Validated release profile; unrelated fields retain their existing defaults."""
    return EvaluationSettings(**AZURE_PRODUCTION_PROFILE)


def automatic_evaluation_settings(environ=None):
    """Require explicit provider configuration in production, without import side effects.

    Called at job admission, not shared-image startup. PR_EVALUATION_SETTINGS is
    the sole runtime settings source; production must explicitly supply every
    field in the release profile. Additional read/cost settings are preserved.
    """
    environ = os.environ if environ is None else environ
    production = bool(environ.get("K_SERVICE")) or (
        environ.get("APP_ENV", "").lower() == "production"
    )
    raw = environ.get("PR_EVALUATION_SETTINGS")
    from shared.ai.provider import ProviderConfig
    config = ProviderConfig.from_env(environ)
    expected_profile = GEMINI_PRODUCTION_PROFILE if config.provider == "google" else AZURE_PRODUCTION_PROFILE
    if production and not raw:
        raise EvaluationConfigurationError(
            "PR_EVALUATION_SETTINGS is required for automatic production evaluations"
        )
    try:
        settings = EvaluationSettings.model_validate_json(
            raw if raw is not None else (
                json.dumps(dict(GEMINI_PRODUCTION_PROFILE, provider_identity=config.identity))
                if config.provider == "google" else "{}")
        )
    except ValidationError:
        # Settings may include private deployment metadata; do not echo the input.
        raise EvaluationConfigurationError("PR_EVALUATION_SETTINGS is invalid") from None
    if settings.provider != config.provider or (settings.provider == "google" and settings.provider_identity != config.identity):
        raise EvaluationConfigurationError("Evaluation provider identity differs from runtime configuration")
    if production:
        missing = expected_profile.keys() - settings.model_fields_set
        mismatched = {
            name
            for name, expected in expected_profile.items()
            if getattr(settings, name) != expected
        }
        if missing or mismatched:
            raise EvaluationConfigurationError(
                f"PR_EVALUATION_SETTINGS must explicitly match the {config.provider} production profile: "
                + ", ".join(sorted(missing | mismatched))
            )
    return settings
