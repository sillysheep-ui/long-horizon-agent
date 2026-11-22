"""声明式行程约束层：规格（spec）与求值（engine）分离。"""

from product.constraints.engine import build_view, evaluate, failed_ids, passed, summary
from product.constraints.model import CATEGORIES, SEVERITIES, Constraint, ConstraintSet, Finding, ItineraryView
from product.constraints.spec import DEFAULT_SET, load_set, validate

__all__ = [
    "CATEGORIES",
    "DEFAULT_SET",
    "SEVERITIES",
    "Constraint",
    "ConstraintSet",
    "Finding",
    "ItineraryView",
    "build_view",
    "evaluate",
    "failed_ids",
    "load_set",
    "passed",
    "summary",
    "validate",
]
