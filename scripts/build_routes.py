"""
Build the map's route lines from the master segment file.

    python scripts/build_routes.py

Reads   data/sheaf.geojson   (the master you edit in QGIS — one line per segment)
Writes  data/routes.geojson  (what the map draws — one line per route per section)

What it does
------------
1. Sorts every segment into the routes it belongs to, using the rules in
   ROUTES below. A segment can belong to several routes.
2. Within each route and section, joins segments that share an endpoint into
   single continuous lines.
3. Points every line the same way: downstream, from Dore towards the Don.
   The map offsets lines to the left or right of their drawing direction, so
   consistent direction means the offset lines never swap sides.
   Exception: one-way cycle segments keep the direction they were drawn in,
   because that direction is what the map's arrows show.

No extra packages needed — standard Python 3 only.
"""

import argparse
import json
import math
from collections import defaultdict

# ── Display rules ───────────────────────────────────────────────────────────
# Which segments make up each route. Change these, re-run, and the map follows.
ROUTES = {
    "walk":      lambda p: not p["proposed"] and p["walking_default"],
    "step_free": lambda p: not p["proposed"] and not p["on_road"] and p["step_free"],
    "cycle":     lambda p: not p["proposed"] and p["cycleway"],
    "vision":    lambda p: p["proposed"],
}

# Ends of the valley, used to decide which way is "downstream".
UPSTREAM   = (-1.5155, 53.3276)   # Dore & Totley Station
DOWNSTREAM = (-1.4623, 53.3853)   # Don confluence

PRECISION = 6  # decimal places (~10 cm); endpoints are matched at this precision


def node(coord):
    """An endpoint, rounded so tiny floating-point differences still match."""
    return (round(coord[0], PRECISION), round(coord[1], PRECISION))


def progress(coord):
    """How far down the valley a point is (bigger = further downstream)."""
    vx, vy = DOWNSTREAM[0] - UPSTREAM[0], DOWNSTREAM[1] - UPSTREAM[1]
    return (coord[0] - UPSTREAM[0]) * vx + (coord[1] - UPSTREAM[1]) * vy


def length_m(coords):
    """Length of a line in metres (haversine)."""
    total = 0.0
    for (x1, y1), (x2, y2) in zip(coords, coords[1:]):
        p1, p2 = math.radians(y1), math.radians(y2)
        dp, dl = p2 - p1, math.radians(x2 - x1)
        a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
        total += 2 * 6371000 * math.asin(math.sqrt(a))
    return total


def join_segments(segments):
    """
    Join segments that share endpoints into continuous chains.

    A chain continues through any endpoint shared by exactly two segments,
    and stops at dead ends and at junctions (three or more segments meeting).
    Returns a list of chains, each a list of (segment, reversed?) pairs in order.
    """
    ends = defaultdict(list)  # endpoint -> segments that start or end there
    for s in segments:
        c = s["geometry"]["coordinates"]
        ends[node(c[0])].append(s)
        ends[node(c[-1])].append(s)

    def far_end(seg, from_node):
        c = seg["geometry"]["coordinates"]
        return node(c[-1]) if node(c[0]) == from_node else node(c[0])

    used, chains = set(), []

    def walk_from(start_node, first):
        chain, here, seg = [], start_node, first
        while seg is not None and id(seg) not in used:
            used.add(id(seg))
            c = seg["geometry"]["coordinates"]
            chain.append((seg, node(c[0]) != here))  # reversed if it starts at the far end
            here = far_end(seg, here)
            nxt = [x for x in ends[here] if id(x) not in used]
            seg = nxt[0] if len(ends[here]) == 2 and nxt else None
        return chain

    # Start from dead ends and junctions first, so chains run end to end…
    for n, segs in ends.items():
        if len(segs) != 2:
            for s in segs:
                if id(s) not in used:
                    chains.append(walk_from(n, s))
    # …then pick up anything left (closed loops).
    for s in segments:
        if id(s) not in used:
            chains.append(walk_from(node(s["geometry"]["coordinates"][0]), s))
    return chains


def split_loop(chain):
    """
    Where two alternative paths join the same two points, the chain comes back
    to where it started. Split it at its most upstream and most downstream
    points into two separate paths, so each can be pointed downstream.
    """
    def start(seg, rev):
        c = seg["geometry"]["coordinates"]
        return node(c[-1] if rev else c[0])

    nodes = [start(s, r) for s, r in chain]          # node at the start of each segment
    c_last = chain[-1][0]["geometry"]["coordinates"]
    if node(c_last[0] if chain[-1][1] else c_last[-1]) != nodes[0] or len(chain) < 2:
        return [chain]                                 # not a loop

    lo = min(range(len(nodes)), key=lambda i: progress(nodes[i]))
    chain, nodes = chain[lo:] + chain[:lo], nodes[lo:] + nodes[:lo]
    hi = max(range(len(nodes)), key=lambda i: progress(nodes[i]))
    return [part for part in (chain[:hi], chain[hi:]) if part]


def chain_coords(chain):
    """Flatten a chain into one coordinate list, dropping repeated joins."""
    out = []
    for seg, rev in chain:
        c = [list(map(float, p[:2])) for p in seg["geometry"]["coordinates"]]
        if rev:
            c.reverse()
        for p in c:
            p = [round(p[0], PRECISION), round(p[1], PRECISION)]
            if not out or out[-1] != p:
                out.append(p)
    return out


FLAGS = ["proposed", "walking_default", "step_free", "cycleway", "on_road", "one_way", "steep"]


def check(master):
    """Stop with a clear message if the master file has a mistake the map would trip over."""
    problems, seen = [], set()
    for i, f in enumerate(master["features"]):
        p = f["properties"]
        name = p.get("segment_id") or f"feature {i + 1}"
        if not p.get("segment_id"):
            problems.append(f"{name}: no segment_id")
        elif p["segment_id"] in seen:
            problems.append(f"{name}: segment_id used more than once")
        seen.add(p.get("segment_id"))
        if not p.get("section"):
            problems.append(f"{name}: no section")
        problems += [f"{name}: '{k}' must be true or false" for k in FLAGS if not isinstance(p.get(k), bool)]
        g = f.get("geometry") or {}
        if g.get("type") != "LineString" or len(g.get("coordinates", [])) < 2:
            problems.append(f"{name}: geometry must be a single line (LineString), got {g.get('type')}")
    if problems:
        raise SystemExit("Problems in the master file:\n  " + "\n  ".join(problems))


def build(master):
    check(master)
    features = []
    for route, rule in ROUTES.items():
        by_section = defaultdict(list)
        for f in master["features"]:
            if rule(f["properties"]):
                by_section[f["properties"]["section"]].append(f)

        for section, segs in by_section.items():
            # One-way cycle segments keep their drawn direction, so handle them alone.
            fixed = [s for s in segs if route == "cycle" and s["properties"].get("one_way")]
            free  = [s for s in segs if s not in fixed]
            chains = [part for c in join_segments(free) for part in split_loop(c)]
            chains += [[(s, False)] for s in fixed]

            for chain in chains:
                coords = chain_coords(chain)
                one_way = any(s in fixed for s, _ in chain)
                if not one_way and progress(coords[0]) > progress(coords[-1]):
                    coords.reverse()  # point downstream
                props = [s["properties"] for s, _ in chain]
                features.append({
                    "type": "Feature",
                    "properties": {
                        "route":       route,
                        "section":     section,
                        "length_m":    round(length_m(coords)),
                        "step_free":   all(p["step_free"] for p in props),
                        "steep":       any(p["steep"] for p in props),
                        "on_road":     any(p["on_road"] for p in props),
                        "one_way":     one_way,
                        "segment_ids": ",".join(p["segment_id"] for p in props),
                    },
                    "geometry": {"type": "LineString", "coordinates": coords},
                })
    return {"type": "FeatureCollection", "features": features}


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--input",  default="data/sheaf.geojson")
    ap.add_argument("--output", default="data/routes.geojson")
    args = ap.parse_args()

    with open(args.input) as fh:
        master = json.load(fh)
    routes = build(master)

    with open(args.output, "w") as fh:  # one feature per line: readable, clean diffs
        fh.write('{"type":"FeatureCollection","features":[\n')
        fh.write(",\n".join(json.dumps(f, separators=(",", ":")) for f in routes["features"]))
        fh.write("\n]}\n")

    # Summary, so problems are visible in the GitHub Actions log
    print(f"Read {len(master['features'])} segments -> wrote {len(routes['features'])} lines to {args.output}")
    for route in ROUTES:
        lines = [f for f in routes["features"] if f["properties"]["route"] == route]
        km = sum(f["properties"]["length_m"] for f in lines) / 1000
        print(f"  {route:<10} {len(lines):>3} lines  {km:5.1f} km")


if __name__ == "__main__":
    main()
