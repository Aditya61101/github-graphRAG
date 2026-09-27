"""Claim decision pipeline."""

AUTO_APPROVE_LIMIT = 50_000


def check_documents(claim: dict) -> bool:
    """All mandatory documents (bill, prescription, report) are attached."""
    required = {"bill", "prescription", "report"}
    return required.issubset(set(claim.get("documents", [])))


def fraud_score(claim: dict) -> float:
    """Heuristic fraud risk between 0 and 1."""
    score = 0.0
    if claim.get("amount", 0) > 5 * AUTO_APPROVE_LIMIT:
        score += 0.5
    if claim.get("hospital_blacklisted"):
        score += 0.5
    return min(score, 1.0)


def decide(claim: dict) -> str:
    """Approve automatically, send to manual review, or reject."""
    if not check_documents(claim):
        return "rejected"
    if fraud_score(claim) >= 0.5:
        return "manual_review"
    if claim.get("amount", 0) <= AUTO_APPROVE_LIMIT:
        return "approved"
    return "manual_review"
