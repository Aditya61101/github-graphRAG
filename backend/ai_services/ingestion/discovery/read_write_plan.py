import json
from pathlib import Path

from ai_services.models.ingestion_plan import IngestionPlan

PLAN_FILE = Path("data/ingestion_plan.json")
CHUNK_REPORT_FILE = Path("data/chunk_report.txt")

# --- Write ---
def save_plan(plan: IngestionPlan, path: Path = PLAN_FILE) -> None:
    path.write_text(plan.model_dump_json(indent=2), encoding="utf-8")
    
def save_manifest(manifest: dict, path: Path = Path("data/repository_manifest.json")) -> None:
    path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

# --- Read ---
def load_plan(path: Path = PLAN_FILE) -> IngestionPlan:
    return IngestionPlan.model_validate_json(path.read_text(encoding="utf-8"))
