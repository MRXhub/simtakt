#!/usr/bin/env python3
"""Read-only status view assembly shared by the CLI and the web status server.

These functions only assemble dictionaries from the objects passed in.  They
never import the CLI, open connections, or perform any I/O, so both the
composition root and the HTTP layer can reuse them without drift.
"""

from __future__ import annotations

from typing import Any


def capacity_status(
    middleware: Any,
    topology: Any,
    policy: Any,
    *,
    stale_seconds: int = 3600,
) -> dict[str, Any]:
    """Assemble the capacity view from one middleware/topology/policy triple."""

    envelope = policy.as_mapping()["capacity_envelope"]
    allocations = middleware.active_allocations()
    by_target: dict[str, int] = {}
    for item in allocations:
        by_target[item["target_id"]] = by_target.get(item["target_id"], 0) + 1
    targets_view = [{
        "target_id": target["target_id"], "host_id": target["host_id"],
        "active": by_target.get(target["target_id"], 0),
        "active_count": by_target.get(target["target_id"], 0),
        # The governed envelope exposes pool-wide license_sessions, not
        # a per-target concurrency limit. Do not present the pool limit
        # as a target limit.
        "max_active_sessions": None,
        "role": "formal" if target["formal_execution"] else "trial",
    } for target in topology["targets"]]
    pools = []
    for pool_id, target_ids in topology["license_pool_groups"].items():
        pools.append({"license_pool_id": pool_id,
            "license_sessions": envelope["license_sessions"],
            "active": sum(by_target.get(target_id, 0) for target_id in target_ids),
            "active_count": sum(by_target.get(target_id, 0) for target_id in target_ids),
            "license_sessions_in_use": None,
            "license_reserve": envelope.get("license_reserve", 0)})
    counts = middleware.capacity_counts()
    stale = middleware.stale_reconciling_attempts(stale_seconds)
    return {"license_pools": pools, "targets": targets_view,
            "global": {**counts, "stale_reconciling": len(stale)},
            "snapshot": "unavailable"}


def shape_stats(middleware: Any) -> dict[str, Any]:
    """Assemble the task-shape statistics view with the placeholder budget."""

    shapes = middleware.task_shape_statistics()
    return {"shapes": [{**shape, "budget": {
        "max_wall_seconds": None, "command_timeout_seconds": None}}
        for shape in shapes]}
