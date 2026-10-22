"""Agent evals: golden tickets, per-skill cases and the prompt-injection red-team set.

``python -m squadhq.skills.evals run [--skill ID] [--provider-ref P/M] [--dry-run]``
"""
from squadhq.skills.evals.cases import CaseError, EvalCase, Expectation, load_case_file, parse_cases
from squadhq.skills.evals.scoring import CaseScore, score

__all__ = ["CaseError", "EvalCase", "Expectation", "load_case_file", "parse_cases", "CaseScore", "score"]
