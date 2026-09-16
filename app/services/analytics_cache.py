"""
app/services/analytics_cache.py
─────────────────────────────────
Phase 8 — Cache for period analytics results.

STRATEGY:
  Cache key = run_id from the latest DhanSyncLog / FIFO run.
  When a new Sync or FIFO run happens, the run_id changes and the
  cache is automatically stale — next read recomputes and re-caches.
  This avoids re-running period aggregation on every dashboard load.

STORAGE:
  Single JSON file: data/trading_analytics/analytics_cache.json
  Format: { "run_id": "...", "monthly": [...], "yearly": [...] }

THREAD SAFETY:
  Single-process Flask dev server — file writes are atomic enough.
  For multi-worker production, wrap writes in a file lock.
"""

import json
import logging
import os
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger(__name__)

_PROJECT_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), '..', '..')
)
_CACHE_PATH = os.path.join(
    _PROJECT_ROOT, 'data', 'trading_analytics', 'analytics_cache.json'
)


class AnalyticsCache:
    """
    JSON file cache for period analytics.
    Keyed by run_id — automatically stale when run_id changes.

    Usage:
        cache = AnalyticsCache()
        hit   = cache.get(current_run_id)
        if hit is None:
            data = compute_everything()
            cache.set(current_run_id, data)
    """

    def __init__(self, path: str = _CACHE_PATH):
        self._path = path
        os.makedirs(os.path.dirname(path), exist_ok=True)

    def get(self, run_id: Optional[str]) -> Optional[dict]:
        """
        Return cached data if run_id matches, else None.
        Returns None on any read error — caller should recompute.
        """
        if not run_id:
            return None
        try:
            if not os.path.exists(self._path):
                return None
            with open(self._path, 'r') as f:
                cached = json.load(f)
            if cached.get('run_id') == run_id:
                log.debug("[AnalyticsCache] HIT run_id=%s", run_id)
                return cached
            log.debug(
                "[AnalyticsCache] MISS — cached=%s current=%s",
                cached.get('run_id'), run_id
            )
            return None
        except (json.JSONDecodeError, OSError, KeyError) as e:
            log.warning("[AnalyticsCache] Read error: %s", e)
            return None

    def set(self, run_id: str, data: dict) -> None:
        """
        Write data to cache tagged with run_id.
        data should be JSON-serializable (use to_dict() on period objects).
        """
        try:
            payload = {
                'run_id':    run_id,
                'cached_at': datetime.now(timezone.utc).isoformat(),
                **data,
            }
            with open(self._path, 'w') as f:
                json.dump(payload, f, default=str)
            log.debug("[AnalyticsCache] Wrote run_id=%s", run_id)
        except (OSError, TypeError) as e:
            log.warning("[AnalyticsCache] Write error: %s", e)

    def invalidate(self) -> None:
        """Delete the cache file, forcing recomputation on next access."""
        try:
            if os.path.exists(self._path):
                os.remove(self._path)
                log.info("[AnalyticsCache] Invalidated")
        except OSError as e:
            log.warning("[AnalyticsCache] Invalidate error: %s", e)

    def current_run_id(self) -> Optional[str]:
        """
        Fetch the run_id of the most recent FIFO run from the DB.
        Returns None if no runs exist or DB unavailable.
        This is compared against the cached run_id to detect staleness.
        """
        try:
            from app.models_analytics import MatchedTrade
            from sqlalchemy import desc
            latest = MatchedTrade.query.order_by(
                desc(MatchedTrade.matched_at)
            ).with_entities(MatchedTrade.run_id).first()
            return latest[0] if latest else None
        except Exception as e:
            log.debug("[AnalyticsCache] current_run_id error: %s", e)
            return None

    def get_or_compute(self, compute_fn) -> dict:
        """
        Convenience: return cached result if fresh, else call compute_fn()
        and cache the result.

        compute_fn: callable() → dict (JSON-serializable)
        """
        run_id = self.current_run_id()
        cached = self.get(run_id)
        if cached is not None:
            return cached

        log.info("[AnalyticsCache] Computing fresh analytics (run_id=%s)", run_id)
        data = compute_fn()
        if run_id:
            self.set(run_id, data)
        return data