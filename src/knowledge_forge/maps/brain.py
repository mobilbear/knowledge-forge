"""Portable vault hierarchy, Canvas drill-downs, and typed parent links."""
from __future__ import annotations

from collections import defaultdict
import hashlib
import json
from pathlib import Path

from ..discovery import rel

ROOT = "LiangWiki Brain.canvas"
COLORS = ("5", "4", "6", "3", "2", "1")
NAV_START = "<!-- liangwiki:graph-navigation -->"
NAV_END = "<!-- /liangwiki:graph-navigation -->"


def title(note):
    return " ".join(str(note.meta.get("title") or note.path.stem).split())


def wiki(target, label):
    return f"[[{target}|{label.replace(']', '').replace('|', '/')}]]"


def topic_dir(registry, category):
    return "40_maps/" + "/".join(node["category_id"] for node in registry.path_of(category))


def _stable(kind, key):
    return "kf-" + kind + "-" + hashlib.sha256(key.encode()).hexdigest()[:14]


def _card(key, label, links, x, y, color=None, width=292, height=116):
    rich = "**" + label.replace("*", "") + "**"
    if links:
        rich += "\n\n" + " · ".join(links)
    result = {"id": _stable("node", key), "type": "text", "x": x, "y": y,
              "width": width, "height": height, "text": rich}
    if color:
        result["color"] = color
    return result


def _edge(one, two, color):
    return {"id": _stable("edge", one["id"] + two["id"]), "fromNode": one["id"],
            "toNode": two["id"], "fromSide": "bottom", "toSide": "top", "color": color}


def _layout(vault, path, nodes, edges):
    """Keep hand-moved cards and extra user nodes when content is regenerated."""
    target = vault / path
    if target.is_file():
        prior = json.loads(target.read_text(encoding="utf-8"))
        old = {entry["id"]: entry for entry in prior.get("nodes", [])}
        for node in nodes:
            if node["id"] in old:
                for field in ("x", "y", "width", "height"):
                    value = old[node["id"]].get(field)
                    if type(value) is int:
                        node[field] = value
        generated = {n["id"] for n in nodes}
        manual = [entry for key, entry in old.items() if key not in generated and not key.startswith("kf-")]
        nodes.extend(manual)
        present = {n["id"] for n in nodes}
        edges.extend(entry for entry in prior.get("edges", []) if not entry["id"].startswith("kf-")
                     and {entry["fromNode"], entry["toNode"]} <= present)
    return json.dumps({"nodes": nodes, "edges": edges}, ensure_ascii=False, indent=2) + "\n"


def _items(vault, vi, registry):
    ids = {node["category_id"] for node in registry.active()}
    groups = defaultdict(list)
    for note in vi.notes:
        path = rel(vault, note.path)
        is_item = ((note.note_type == "source" and path.startswith("10_sources/"))
                   or (note.note_type in ("concept", "entity") and path.startswith(("20_concepts/", "30_entities/")))
                   or (note.note_type == "map" and Path(path).parent.as_posix() == "40_maps"
                       and not note.path.name.startswith("_")))
        if is_item and not note.parse_error:
            cid = note.meta.get("primary_category_id")
            groups[cid if cid in ids else None].append(note)
    for group in groups.values():
        group.sort(key=lambda note: (note.note_type != "concept", title(note).casefold()))
    return groups


def build(vault, vi, registry):
    groups = _items(vault, vi, registry)
    roots = registry.active(level=1)
    home = _card("home", "LiangWiki · 知识图谱", [wiki("HOME", "HOME"),
                 wiki("Knowledge Map", "Expand outline"), wiki("Library Index", "Search by topic")],
                 0, 0, "6", width=330)
    nodes, edges, outputs = [home], [], {}
    for i, domain in enumerate(roots):
        cid = domain["category_id"]
        dest = topic_dir(registry, cid)
        color = COLORS[i % len(COLORS)]
        node = _card(cid, domain["title"], [wiki(dest + "/_brain.canvas", "Explore branch"),
                     wiki(dest + "/_index", "Domain home")],
                     (i % 4 - 1) * 326 - 162, 212 + i // 4 * 154, color)
        nodes.append(node)
        edges.append(_edge(home, node, color))
    if groups[None]:
        waiting = _card("unclassified", "未分类 / Unclassified",
                        [wiki("Library Index#To classify", "Open unclassified notes")],
                        0, 555, "3")
        nodes.append(waiting)
        edges.append(_edge(home, waiting, "3"))
    outputs[ROOT] = _layout(vault, ROOT, nodes, edges)

    active = {node["category_id"] for node in registry.active()}
    for i, topic in enumerate(registry.active()):
        cid = topic["category_id"]
        folder = topic_dir(registry, cid)
        parents = registry.path_of(cid)
        closest = parents[-2] if len(parents) > 1 else None
        color = COLORS[i % len(COLORS)]
        links = [wiki(ROOT, "Brain overview"), wiki("HOME", "HOME"), wiki(folder + "/_index", "Topic home")]
        if closest:
            links.insert(1, wiki(topic_dir(registry, closest["category_id"]) + "/_brain.canvas", "Parent branch"))
        center = _card(cid, topic["title"], links, 0, 0, color, width=348)
        entries = [[child["category_id"], child["title"],
                    [wiki(topic_dir(registry, child["category_id"]) + "/_brain.canvas", "Explore branch"),
                     wiki(topic_dir(registry, child["category_id"]) + "/_index", "Topic home")]]
                   for child in registry.children(cid) if child["category_id"] in active]
        labels = {"concept": "Open synthesis", "entity": "Open entity", "map": "Open topic map"}
        entries += [[rel(vault, note.path), title(note), [wiki(rel(vault, note.path),
                     labels.get(note.note_type, "Open source"))]]
                    for note in groups[cid]]
        nodes, edges = [center], []
        if not entries:
            nodes.append(_card(cid + "-empty", "No saved sources in this topic yet", [], 0, 212))
        for j, (key, label, destinations) in enumerate(entries):
            node = _card(key, label, destinations, (j % 3 - 1) * 344, 215 + j // 3 * 162,
                         None, width=316, height=126)
            nodes.append(node)
            edges.append(_edge(center, node, color))
        path = folder + "/_brain.canvas"
        outputs[path] = _layout(vault, path, nodes, edges)
    return outputs


def outline(vault, vi, registry):
    groups = _items(vault, vi, registry)
    active = {node["category_id"] for node in registry.active()}

    def branch(node, level):
        cid = node["category_id"]
        dest = topic_dir(registry, cid)
        q = "> " * level
        lines = [q + "[!info]- " + node["title"],
                 q, q + wiki(dest + "/_index", "Topic home") + " · " + wiki(dest + "/_brain.canvas", "Open branch Canvas")]
        for item in groups[cid]:
            lines.append(q + "- " + wiki(rel(vault, item.path), title(item)))
        for child in registry.children(cid):
            if child["category_id"] in active:
                lines += [q, *branch(child, level + 1)]
        return lines

    lines = []
    for root in registry.active(level=1):
        lines.extend(branch(root, 1))
        lines.append("")
    if groups[None]:
        lines += ["> [!question]- Unclassified", ">"]
        lines += ["> - " + wiki(rel(vault, note.path), title(note)) for note in groups[None]]
    return lines


def navigation(registry, category, include_topic=True):
    path = registry.path_of(category) if category and registry.get(category) else []
    ancestors = path if include_topic else path[:-1]
    links = [wiki("HOME", "HOME"), wiki(ROOT, "Brain graph")]
    for node in ancestors:
        links.append(wiki(topic_dir(registry, node["category_id"]) + "/_index", node["title"]))
    return " / ".join(links)
