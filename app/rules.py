"""Rule engine: matching channels to groups and auto-grouping.

Extracted from main.py. Depends only on the standard library and ``app.db`` —
never imports ``app.main``, so there is no circular-import risk.
"""

import re

from app.db import (
    create_group, ensure_unmatched_group, find_group_by_name,
    get_active_source_ids, get_all_channels_raw, get_all_rules_by_group,
    get_group_max_sort_order, list_groups, update_channel_group_bulk,
)


def matches_rule(ch, rule):
    field = rule.get("field","source_group")
    pattern = (rule.get("pattern") or "").strip().lower()
    mt = rule.get("match_type","contains")
    if not pattern: return False
    if field == "source_group": val = (ch.get("source_group") or "").lower()
    elif field == "channel_name": val = (ch.get("source_name") or ch.get("name") or "").lower()
    elif field == "any": val = f"{ch.get('source_group','')} {ch.get('source_name') or ch.get('name','')}".lower()
    else: return False
    if mt == "contains": return pattern in val
    elif mt == "starts_with": return val.startswith(pattern)
    elif mt == "regex":
        try: return bool(re.search(pattern, val, re.IGNORECASE))
        except re.error: return False
    elif mt == "exact": return val == pattern
    return False

def _rule_plan(apply=False, new_only=False):
    """Plan or apply non-destructive rule assignments.

    Locked channels and every channel already in a custom group are immutable.
    A channel that matches no rule remains exactly where it is. Provider-group
    fallback is used only while assigning genuinely new, ungrouped channels.
    """
    asid = get_active_source_ids()
    channels = get_all_channels_raw(source_id=asid)
    if not channels:
        return {"considered": 0, "moves": 0, "unchanged": 0, "protected": 0, "sample": []}
    groups = list_groups()
    rules_map = get_all_rules_by_group()
    has_rules = any(rules_map.get(g["id"]) for g in groups)
    if not has_rules:
        return {"considered": 0, "moves": 0, "unchanged": len(channels), "protected": 0,
                "sample": [], "message": "No rules configured"}

    custom_gids = {g["id"] for g in groups if g.get("is_custom")}
    assignments = []
    sample = []
    counters = {}
    protected = unchanged = considered = 0

    for ch in channels:
        cur_gid = ch.get("group_id")
        if new_only and cur_gid:
            continue
        if ch.get("placement_locked") or cur_gid in custom_gids:
            protected += 1
            continue
        considered += 1
        target = None
        matched_rule = None
        for group in sorted(groups, key=lambda g: g.get("sort_order", 0)):
            for rule in rules_map.get(group["id"], []):
                if matches_rule(ch, rule):
                    target = group
                    matched_rule = rule
                    break
            if target:
                break
        if target is None:
            unchanged += 1
            continue
        target_gid = target["id"]
        if target_gid == cur_gid:
            unchanged += 1
            continue
        if target_gid not in counters:
            counters[target_gid] = get_group_max_sort_order(target_gid) + 1
        order = counters[target_gid]
        counters[target_gid] += 1
        assignments.append((ch["id"], target_gid, order))
        if len(sample) < 100:
            sample.append({
                "id": ch["id"], "name": ch.get("name") or ch.get("source_name"),
                "from_group_id": cur_gid, "to_group_id": target_gid,
                "to_group": target.get("name") if target else ch.get("source_group"),
                "matched": bool(matched_rule),
            })
    if apply and assignments:
        update_channel_group_bulk(assignments)
    return {
        "considered": considered, "moves": len(assignments), "unchanged": unchanged,
        "protected": protected, "sample": sample,
    }


def preview_rules():
    return _rule_plan(apply=False)


def apply_rules():
    return _rule_plan(apply=True)

def _auto_group_by_source(channels):
    seen = {}
    assignments = []
    counters = {}
    for ch in channels:
        sg = ch.get("source_group") or "Ungrouped"
        if sg not in seen:
            existing = find_group_by_name(sg)
            if existing:
                seen[sg] = existing["id"]
            else:
                g = create_group(sg, is_custom=False)
                seen[sg] = g["id"]
            counters[seen[sg]] = 0
        gid = seen[sg]
        assignments.append((ch["id"], gid, counters[gid]))
        counters[gid] += 1
    update_channel_group_bulk(assignments)


def _assign_new_channels():
    """Assign groups only to channels that don't have a group yet (newly added).
    Uses rules if available, otherwise auto-groups by source_group.
    Existing channel group assignments are untouched."""
    asid = get_active_source_ids()
    all_channels = get_all_channels_raw(source_id=asid)
    ungrouped = [ch for ch in all_channels if not ch.get("group_id")]
    if not ungrouped:
        return 0

    groups = list_groups()
    rules_map = get_all_rules_by_group()
    has_rules = any(rules_map.get(g["id"]) for g in groups)

    if has_rules:
        assignments = []
        assigned = set()
        counters = {}
        for group in sorted(groups, key=lambda g: g.get("sort_order", 0)):
            rules = rules_map.get(group["id"], [])
            if not rules: continue
            for ch in ungrouped:
                if ch["id"] in assigned: continue
                for rule in rules:
                    if matches_rule(ch, rule):
                        gid = group["id"]
                        if gid not in counters:
                            counters[gid] = get_group_max_sort_order(gid) + 1
                        assignments.append((ch["id"], gid, counters[gid]))
                        counters[gid] += 1
                        assigned.add(ch["id"])
                        break
        group_by_name = {g.get("name"): g for g in groups}
        for ch in ungrouped:
            if ch["id"] not in assigned:
                source_name = ch.get("source_group") or "Ungrouped"
                target = group_by_name.get(source_name)
                if target is None:
                    target = create_group(source_name, is_custom=False)
                    group_by_name[source_name] = target
                gid = target["id"]
                if gid not in counters:
                    counters[gid] = get_group_max_sort_order(gid) + 1
                assignments.append((ch["id"], gid, counters[gid]))
                counters[gid] += 1
        update_channel_group_bulk(assignments)
    else:
        # Auto-group by source_group, only for ungrouped channels
        seen = {}
        assignments = []
        counters = {}
        for ch in ungrouped:
            sg = ch.get("source_group") or "Ungrouped"
            if sg not in seen:
                existing = find_group_by_name(sg)
                if existing:
                    seen[sg] = existing["id"]
                else:
                    g = create_group(sg, is_custom=False)
                    seen[sg] = g["id"]
                counters[seen[sg]] = get_group_max_sort_order(seen[sg]) + 1
            gid = seen[sg]
            assignments.append((ch["id"], gid, counters[gid]))
            counters[gid] += 1
        update_channel_group_bulk(assignments)
    return len(ungrouped)
