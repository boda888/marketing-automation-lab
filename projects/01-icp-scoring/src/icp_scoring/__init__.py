"""Explainable ICP fit + engagement scoring."""

from .config import ICPConfig, load_config
from .models import Company, EngagementEvent, ScoreRequest, ScoreResult
from .scoring import score

__all__ = [
    "Company",
    "EngagementEvent",
    "ICPConfig",
    "ScoreRequest",
    "ScoreResult",
    "load_config",
    "score",
]
__version__ = "0.1.0"
