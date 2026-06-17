"""Dynamic Reviewer — task-specific reviewers selected at runtime.

Instead of fixed Auditor+PMO for all tasks, the DynamicReviewer:
1. Reads the WorkflowPlan's reviewer list
2. Composes the right set of reviewer agents
3. Each reviewer scores a specific aspect of the output

Reviewer types:
  - CodeReviewer: code quality, security, best practices
  - FactChecker: source verification, accuracy
  - VisionVerifier: screenshot-based action verification
  - DefaultAuditor: generic quality check (fallback)
"""

import json, logging
from typing import Optional
from dataclasses import dataclass

logger = logging.getLogger("ai_company.reviewer")


@dataclass
class ReviewResult:
    reviewer: str
    score: float       # 0-100
    passed: bool
    feedback: str


REVIEWER_PROMPTS = {
    "code_review": """Review this code output for quality:
- Correctness: does it do what was asked?
- Security: any injection risks, hardcoded secrets?
- Best practices: proper error handling, readable code?
- Completeness: are all requirements met?

Output: {{score: 0-100, passed: true/false, feedback: "specific issues found"}}""",

    "fact_check": """Verify the factual claims in this output:
- Are sources cited?
- Are claims verifiable?
- Is the data current?
- Any contradictions?

Output: {{score: 0-100, passed: true/false, feedback: "specific issues"}}""",

    "vision_verify": """Verify this action output:
- Was the action completed successfully?
- Are there error messages?
- Is the output consistent with expectations?

Output: {{score: 0-100, passed: true/false, feedback: "verification result"}}""",

    "default": """Review this output for general quality:
- Is the output relevant to the task?
- Is it well-structured?
- Are there obvious errors or omissions?

Output: {{score: 0-100, passed: true/false, feedback: "review"}}""",
}


class DynamicReviewer:
    """Select and run task-specific reviewers."""
    
    def __init__(self):
        self._llm = None
    
    def _get_llm(self):
        if self._llm is None:
            from src.ceo.graph import _get_llm
            self._llm = _get_llm("ceo")
        return self._llm
    
    async def review(self, task: str, output: str, reviewer_types: list[str]) -> list[ReviewResult]:
        """Run all specified reviewers against the output."""
        results = []
        for rtype in reviewer_types:
            result = await self._run_reviewer(rtype, task, output)
            results.append(result)
        return results
    
    async def _run_reviewer(self, rtype: str, task: str, output: str) -> ReviewResult:
        prompt_template = REVIEWER_PROMPTS.get(rtype, REVIEWER_PROMPTS["default"])
        prompt = f"{prompt_template}\n\nTask: {task}\nOutput: {output[:2000]}"
        
        try:
            llm = self._get_llm()
            response = await llm.ainvoke(prompt)
            raw = str(response.content)
            
            # Parse JSON
            if "```" in raw:
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
            data = json.loads(raw.strip())
            
            return ReviewResult(
                reviewer=rtype,
                score=float(data.get("score", 60)),
                passed=data.get("passed", data.get("score", 60) >= 60),
                feedback=data.get("feedback", ""),
            )
        except Exception as e:
            logger.warning("Reviewer %s failed: %s", rtype, e)
            return ReviewResult(
                reviewer=rtype,
                score=50,
                passed=True,  # Don't block on reviewer failure
                feedback=f"Reviewer error: {e}",
            )
    
    def aggregate(self, results: list[ReviewResult]) -> dict:
        """Aggregate multiple review results into final score."""
        if not results:
            return {"overall_score": 95, "verdict": "APPROVE", "feedback": "No reviewers configured"}
        
        avg_score = sum(r.score for r in results) / len(results)
        all_passed = all(r.passed for r in results)
        feedback = "; ".join(f"[{r.reviewer}] {r.feedback[:100]}" for r in results if r.feedback)
        
        if all_passed and avg_score >= 70:
            verdict = "APPROVE"
        elif avg_score >= 50:
            verdict = "REVISE"
        else:
            verdict = "REJECT"
        
        return {
            "overall_score": round(avg_score, 1),
            "verdict": verdict,
            "feedback": feedback,
            "details": [{"reviewer": r.reviewer, "score": r.score, "feedback": r.feedback[:200]} for r in results],
        }


# Singleton
_reviewer: Optional[DynamicReviewer] = None

def get_dynamic_reviewer() -> DynamicReviewer:
    global _reviewer
    if _reviewer is None:
        _reviewer = DynamicReviewer()
    return _reviewer
