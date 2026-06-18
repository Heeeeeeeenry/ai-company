# Verification Module
from src.verification.verifier import Verifier, VerifyResult


def verify_aggregate(intent: str, output: str, execution_log=None) -> dict:
    """兼容 graph.py verify_aggregate_node 的 score_card 格式。

    Returns:
        dict: {"score": int, "decision": str, "next_action": str, "needs_audit": bool}
    """
    verifier = Verifier()
    return verifier.verify_aggregate(intent, output, execution_log)


__all__ = ["Verifier", "VerifyResult", "verify_aggregate"]
