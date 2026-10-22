"""Expand successful disc previews using the files actually checked by the provider."""

from copy import deepcopy
from functools import wraps
from pathlib import Path


def expand_items(items, plans):
    expanded = []
    for item in items:
        plan = plans.get(item.get("source"))
        rows = plan.get("episodes", []) if plan else []
        if (not item.get("success") or not rows
                or item.get("target") != str(Path(plan["output"]) / rows[0]["relative"])):
            expanded.append(item)
            continue
        disc = f"DISC{rows[0]['disc']}"
        group = f"{disc}（{len(rows)} 集，E{rows[0]['episode']:02}–E{rows[-1]['episode']:02}）"
        for row in rows:
            projected = deepcopy(item)
            marker = f"S{plan['season']:02}E{row['episode']:02}"
            projected.update(
                target=str(Path(plan["output"]) / row["relative"]),
                episode=row["episode"], episode_end=None,
                message=f"{group} · {marker} · {row['title']}",
            )
            if projected.get("source_item"):
                projected["source_item"]["name"] = f"{group} · {marker} · {row['title']}"
            expanded.append(projected)
    return expanded


def install(plugin, collector_class=None):
    if collector_class is None:
        from app.chain.transfer.request import _TransferSubmissionCollector
        collector_class = _TransferSubmissionCollector
    original = collector_class.result
    original = getattr(original, "_blurayepisodes_original", original)

    @wraps(original)
    def result(collector, success, errors, *, preview, preview_items):
        if plugin.get_state() and preview:
            preview_items = expand_items(preview_items, plugin._preview_plans)
        return original(collector, success, errors, preview=preview, preview_items=preview_items)

    result._blurayepisodes_original = original
    collector_class.result = result

    def restore():
        if collector_class.result is result:
            collector_class.result = original

    return restore
