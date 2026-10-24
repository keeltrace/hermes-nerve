"""Quilt layout: stable nested bounds for the real observatory hierarchy.

Builds one tree from the collector snapshot (fleet -> host -> source -> agent ->
run) and assigns every node a stable id and fixed world-space bounds. Layout is
a pure function of the data: the same snapshot yields the same ids and bounds,
so a zoom anchored on a node stays anchored across refreshes.

No descendants are invented. A node that has no real children is a leaf with
``children == []``; an unknown or unavailable source is marked as such, never
filled with placeholder cards.
"""

from __future__ import annotations

import math
from typing import Any

from .camera import Frame

# A spatial atlas, not a two-column feed. All geometry is deterministic and
# grounded exclusively in real observed hierarchy nodes.
HOST_W, HOST_H = 1000.0, 700.0
CELL_W, CELL_H = 300.0, 180.0
PAD = 28.0
HEADER = 86.0
GUTTER = 22.0
TARGET_ASPECT = 1.56
MAX_GRID_COLUMNS = 96

UNKNOWN = "unknown"


def _node(node_id: str, kind: str, label: str, state: str, bounds: tuple[float, float, float, float], data: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": node_id, "kind": kind, "label": label, "state": state,
        "bounds": bounds, "data": data, "children": [],
    }


def _column_count(count: int) -> int:
    """Seed geometry only; final placement uses measured child dimensions."""
    if not count:
        return 1
    target = math.sqrt(count * (CELL_H + GUTTER) * TARGET_ASPECT / (CELL_W + GUTTER))
    return max(1, min(count, MAX_GRID_COLUMNS, round(target)))


def _grid(parent_bounds: tuple[float, float, float, float], count: int) -> list[tuple[float, float, float, float]]:
    """Provisional cells; the bottom-up layout supplies authoritative bounds."""
    px, py, _, _ = parent_bounds
    cols = _column_count(count)
    return [
        (px + PAD + (i % cols) * (CELL_W + GUTTER),
         py + PAD + HEADER + (i // cols) * (CELL_H + GUTTER),
         CELL_W, CELL_H)
        for i in range(count)
    ]


def _container_size(child_count: int) -> tuple[float, float]:
    """Minimum width/height for homogeneous cells at the chosen grid count."""
    cols = _column_count(child_count)
    rows = max(1, math.ceil(child_count / cols))
    return (2 * PAD + cols * CELL_W + (cols - 1) * GUTTER,
            2 * PAD + HEADER + rows * CELL_H + (rows - 1) * GUTTER)


def _state_of(freshness: dict[str, Any] | None) -> str:
    if not freshness or not freshness.get("state"):
        return UNKNOWN
    return str(freshness["state"])


def build_tree(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Return the root node (fleet) with real children only."""
    fleet_hosts = (snapshot.get("fleet") or {}).get("hosts") or []
    root = _node("fleet", "fleet", "FLEET", UNKNOWN, (0.0, 0.0, 0.0, 0.0), {"note": (snapshot.get("fleet") or {}).get("note")})
    host_cells = _grid((0.0, 0.0, 0.0, 0.0), len(fleet_hosts))
    for host, cell in zip(fleet_hosts, host_cells):
        host_id = "host:" + str(host.get("id", "unrecorded"))
        host_node = _node(host_id, "host", str(host.get("hostname") or host.get("id") or "UNRECORDED"), "ok", (cell[0], cell[1], HOST_W, HOST_H), {"host": host})
        root["children"].append(host_node)
        # Harness sources belong to the local host; they are real collector records.
        sources = snapshot.get("harnesses") or []
        src_cells = _grid(host_node["bounds"], len(sources))
        for src, sc in zip(sources, src_cells):
            name = str(src.get("source") or "UNRECORDED")
            src_node = _node(
                "source:" + str(host.get("id", "unrecorded")) + ":" + name, "source", name,
                _state_of(src.get("freshness")), sc,
                {"source": src},
            )
            host_node["children"].append(src_node)
            agents = [a for a in (snapshot.get("agents") or []) if name in (a.get("sources") or [])]
            agent_cells = _grid(sc, len(agents))
            for a, ac in zip(agents, agent_cells):
                wid = str(a.get("worker_id") or "unrecorded")
                agent_node = _node(
                    "agent:" + str(host.get("id", "unrecorded")) + ":" + name + ":" + wid, "agent", wid,
                    _state_of(a.get("freshness")), ac,
                    {"agent": a},
                )
                src_node["children"].append(agent_node)
                runs = [r for r in (snapshot.get("runs") or [])
                        if (r.get("worker_id") or "unrecorded") == wid and name in (r.get("sources") or [])]
                # Group real runs by their actual task identity, then numeric run
                # order. This creates coherent visual neighborhoods without adding
                # synthetic task or worker records.
                def stable_run_order(run):
                    raw = str(run.get("run_id") if run.get("run_id") is not None else "")
                    return (str(run.get("task_id") or ""), (0, int(raw)) if raw.isdecimal() else (1, raw))
                runs.sort(key=stable_run_order)
                run_cells = _grid(ac, len(runs))
                for r, rc in zip(runs, run_cells):
                    run_node = _node(
                        "run:" + str(host.get("id", "unrecorded")) + ":" + name + ":" + wid + ":" + str(r.get("task_id")) + "#" + str(r.get("run_id")), "run",
                        str(r.get("task_id")) + " #" + str(r.get("run_id")),
                        _state_of(r.get("freshness")), rc,
                        {"run": {"task_id": r.get("task_id"), "run_id": r.get("run_id")}},
                    )
                    agent_node["children"].append(run_node)
    _fit(root)
    return root


def _grid_measure(children: list[tuple[float, float]], columns: int) -> tuple[float, float, list[float], list[float]]:
    """Measure row and column extents without overlap, including header space."""
    rows = math.ceil(len(children) / columns)
    col_widths = [0.0] * columns
    row_heights = [0.0] * rows
    for i, (width, height) in enumerate(children):
        col = i % columns
        row = i // columns
        col_widths[col] = max(col_widths[col], width)
        row_heights[row] = max(row_heights[row], height)
    width = 2 * PAD + sum(col_widths) + (columns - 1) * GUTTER
    height = 2 * PAD + HEADER + sum(row_heights) + (rows - 1) * GUTTER
    return width, height, col_widths, row_heights


def _best_columns(children: list[tuple[float, float]]) -> int:
    """Choose the actual spatial aspect, not a fixed two-column feed.

    Penalize long strips and wasted holes; layout work is bounded by MAX_GRID_COLUMNS.
    Children retain their observed order. We never fabricate spacer nodes.
    """
    count = len(children)
    if count < 2:
        return 1
    best_cols = 1
    best_score = float("inf")
    for columns in range(1, min(count, MAX_GRID_COLUMNS) + 1):
        w, h, _, _ = _grid_measure(children, columns)
        aspect_cost = abs(math.log((w / h) / TARGET_ASPECT))
        slots = columns * math.ceil(count / columns)
        empty_cost = ((slots - count) / slots) * 0.2
        score = aspect_cost + empty_cost
        if score < best_score - 1e-12:
            best_score, best_cols = score, columns
    return best_cols


def _size(node: dict[str, Any]) -> tuple[float, float]:
    """Compute the measured bounds of any subtree."""
    dimensions: dict[int, tuple[float, float]] = {}
    plans: dict[int, int] = {}
    _measure(node, dimensions, plans)
    return dimensions[id(node)]


def _measure(node: dict[str, Any], dimensions: dict[int, tuple[float, float]],
             plans: dict[int, int]) -> None:
    """Cache one bottom-up measurement for the entire tree (no repeated recursion)."""
    if not node["children"]:
        dimensions[id(node)] = (CELL_W, CELL_H)
        return
    for child in node["children"]:
        _measure(child, dimensions, plans)
    child_sizes = [dimensions[id(child)] for child in node["children"]]
    cols = _best_columns(child_sizes)
    plans[id(node)] = cols
    dimensions[id(node)] = _grid_measure(child_sizes, cols)[:2]


def _place(node: dict[str, Any], x: float, y: float,
           dimensions: dict[int, tuple[float, float]] | None = None,
           plans: dict[int, int] | None = None) -> None:
    """Assign world-space rectangles to the same real children, no overlap."""
    if dimensions is None or plans is None:
        dimensions, plans = {}, {}
        _measure(node, dimensions, plans)
    width, height = dimensions[id(node)]
    node["bounds"] = (x, y, width, height)
    children = node["children"]
    if not children:
        return
    cols = plans[id(node)]
    child_sizes = [dimensions[id(child)] for child in children]
    _, _, col_widths, row_heights = _grid_measure(child_sizes, cols)
    cx, cy = [], []
    next_x = x + PAD
    for w in col_widths:
        cx.append(next_x)
        next_x += w + GUTTER
    next_y = y + PAD + HEADER
    for h in row_heights:
        cy.append(next_y)
        next_y += h + GUTTER
    for i, child in enumerate(children):
        _place(child, cx[i % cols], cy[i // cols], dimensions, plans)


def _fit(node: dict[str, Any]) -> None:
    """Single bottom-up layout pass, followed by one top-down placement pass."""
    dimensions: dict[int, tuple[float, float]] = {}
    plans: dict[int, int] = {}
    _measure(node, dimensions, plans)
    _place(node, node["bounds"][0], node["bounds"][1], dimensions, plans)


def index_by_id(root: dict[str, Any]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    stack = [root]
    while stack:
        node = stack.pop()
        if node["id"] in out:
            raise ValueError("duplicate node id: " + node["id"])
        out[node["id"]] = node
        stack.extend(node["children"])
    return out


def visible_nodes(root: dict[str, Any], rect: tuple[float, float, float, float], intersects) -> list[dict[str, Any]]:
    """Nodes whose bounds intersect ``rect``, parents before children.

    A zero-size container (the fleet root) is not itself drawn, but its children
    are still tested, so the walk descends through it.
    """
    out: list[dict[str, Any]] = []
    stack = [root]
    while stack:
        node = stack.pop()
        b = node["bounds"]
        drawable = b[2] > 0 and b[3] > 0
        if drawable and intersects(b, rect):
            out.append(node)
            stack.extend(reversed(node["children"]))
        elif not drawable:
            stack.extend(reversed(node["children"]))
    return out



def to_wire(node: dict[str, Any]) -> dict[str, Any]:
    """JSON-safe view of a node for the browser: ids, labels, states, bounds, children.

    Carries no event bodies, prompts or secrets: only the identity, label, state and
    geometry the camera needs, plus the child list. Bounds are plain floats.
    """
    x, y, w, h = node["bounds"]
    return {
        "id": node["id"],
        "kind": node["kind"],
        "label": node["label"],
        "state": node["state"],
        "bounds": [float(x), float(y), float(w), float(h)],
        "children": [to_wire(child) for child in node["children"]],
    }


# ---------------------------------------------------------------------------
# Frame navigation, culling, search and paging over a wire-form tree.
#
# All functions take the wire form (dicts with id/kind/label/state/bounds/children)
# and never invent nodes. Ids are the only navigation key; a stored id that no
# longer exists resolves to its nearest surviving ancestor.
# ---------------------------------------------------------------------------

def index_wire(root: dict[str, Any]) -> dict[str, tuple[dict[str, Any], list[str]]]:
    """Map id -> (node, ancestor_ids), root first. Duplicate ids are refused."""
    out: dict[str, tuple[dict[str, Any], list[str]]] = {}
    stack: list[tuple[dict[str, Any], list[str]]] = [(root, [])]
    while stack:
        node, anc = stack.pop()
        nid = node["id"]
        if nid in out:
            raise ValueError("duplicate node id: " + nid)
        out[nid] = (node, anc)
        child_anc = anc + [nid]
        for child in node["children"]:
            stack.append((child, child_anc))
    return out


def resolve_stack(root: dict[str, Any], stack_ids: list[str]) -> list[str]:
    """Longest valid prefix of a remembered frame stack, each id a child of the previous.

    Ids that vanished (or are no longer children of their predecessor) are dropped.
    The result always starts with the root id, so the caller can always fall back
    to the fleet view. Returns a new list; the input is not mutated.
    """
    if not isinstance(root, dict) or not isinstance(root.get("id"), str):
        return []
    index = index_wire(root)
    kept = [root["id"]]
    for nid in stack_ids or []:
        if not isinstance(nid, str):
            break
        parent_id = kept[-1]
        parent = index[parent_id][0]
        child = next((c for c in parent["children"] if c["id"] == nid), None)
        if child is None or frame_for_bounds(child["bounds"]) is None:
            break
        kept.append(nid)
    return kept


def frame_for_bounds(bounds) -> "Frame | None":
    """The district frame for a node: origin at its top-left, one local unit per 1/1000 of its width.

    Mirrors ``frameOfNode`` in the browser. Returns None for a node with no measurable size.
    """
    try:
        x, y, w, h = (float(v) for v in bounds[:4])
    except (TypeError, ValueError, IndexError):
        return None
    if not (math.isfinite(x) and math.isfinite(y) and math.isfinite(w) and math.isfinite(h)):
        return None
    if w <= 0 or h <= 0:
        return None
    return Frame(x, y, w / 1000.0)


def find_node(root: dict[str, Any], node_id: str) -> dict[str, Any] | None:
    """The node with ``node_id`` or None. Never raises for a missing id."""
    hit = index_wire(root).get(node_id)
    return hit[0] if hit else None


def flatten_wire(root: dict[str, Any]) -> list[tuple[dict[str, Any], int]]:
    """Preorder (node, depth) list. Used for the accessible list and search."""
    out: list[tuple[dict[str, Any], int]] = []
    stack: list[tuple[dict[str, Any], int]] = [(root, 0)]
    while stack:
        node, depth = stack.pop()
        out.append((node, depth))
        for child in reversed(node["children"]):
            stack.append((child, depth + 1))
    return out


def cull_wire(top: dict[str, Any], rect: tuple[float, float, float, float], *,
              traverse_budget: int, render_budget: int) -> dict[str, Any]:
    """Visible children of ``top`` inside ``rect`` with separate budgets.

    ``traverse_budget`` bounds how many nodes are examined; ``render_budget`` bounds
    how many are returned. When either is hit the result says so (``truncated``)
    instead of silently dropping nodes. Parents come before their children.
    """
    if traverse_budget < 0 or render_budget < 0:
        raise ValueError("budgets must be non-negative")
    shown: list[dict[str, Any]] = []
    examined = 0
    truncated = False
    stack: list[dict[str, Any]] = list(reversed(top["children"]))
    while stack:
        if examined >= traverse_budget:
            truncated = True
            break
        node = stack.pop()
        examined += 1
        b = node["bounds"]
        drawable = b[2] > 0 and b[3] > 0
        if drawable and _rect_hits(b, rect):
            if len(shown) >= render_budget:
                truncated = True
                break
            shown.append(node)
            stack.extend(reversed(node["children"]))
        elif not drawable:
            stack.extend(reversed(node["children"]))
    return {"nodes": shown, "examined": examined, "truncated": truncated}


def _rect_hits(a: list[float] | tuple[float, ...], b: tuple[float, float, float, float]) -> bool:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    return ax < bx + bw and bx < ax + aw and ay < by + bh and by < ay + ah


def search_wire(root: dict[str, Any], query: str) -> list[dict[str, Any]]:
    """Case-insensitive substring match on id, kind, label and state. Empty query -> []."""
    needle = str(query or "").strip().lower()
    if not needle:
        return []
    out = []
    for node, _depth in flatten_wire(root):
        hay = " ".join(str(node.get(k, "")) for k in ("id", "kind", "label", "state")).lower()
        if needle in hay:
            out.append(node)
    return out


def page_items(items: list[Any], page: int, size: int) -> dict[str, Any]:
    """One page of ``items``. Page numbers are clamped to range; size must be >= 1."""
    if size < 1:
        raise ValueError("size must be >= 1")
    total_pages = max(1, -(-len(items) // size))
    p = min(max(0, int(page)), total_pages - 1)
    return {"items": items[p * size:(p + 1) * size], "page": p, "pages": total_pages, "total": len(items)}
