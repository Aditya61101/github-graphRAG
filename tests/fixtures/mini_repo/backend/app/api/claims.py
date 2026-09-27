from fastapi import APIRouter

from ..claims.processing import decide

router = APIRouter()


@router.post("/claims")
def submit_claim(claim: dict):
    """Submit a claim and get the automatic decision."""
    return {"decision": decide(claim)}
