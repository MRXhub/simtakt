"""Compatibility import for the batch queue worker skeleton."""
from __future__ import annotations
try:
    from .adapter import BatchQueueWorker
except ImportError:
    from adapter import BatchQueueWorker

__all__ = ["BatchQueueWorker"]
