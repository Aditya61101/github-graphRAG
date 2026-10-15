"""Stage 4.2: immutable, bounded architectural evaluation."""

from .authority import UploadAuthority
from .engine import ContextRequestBudget, DecisionIntegrityEvaluator
from .model_adapter import AzureEvaluationModel, EvaluationModel
from .models import EvaluationInputError, EvaluationResult, EvaluationSettings

__all__ = [
    "AzureEvaluationModel",
    "ContextRequestBudget",
    "DecisionIntegrityEvaluator",
    "EvaluationInputError",
    "EvaluationModel",
    "EvaluationResult",
    "EvaluationSettings",
    "UploadAuthority",
    "read_evaluation_result",
]


def read_evaluation_result(payload):
    """Read historical audit results under their original schema; never upgrade them."""
    import json

    version = json.loads(payload).get("schema_version")
    if version == "dg-evaluation-v1":
        from .historical_models import EvaluationResult as HistoricalResult

        return HistoricalResult.model_validate_json(payload)
    return EvaluationResult.model_validate_json(payload)
