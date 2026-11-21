"""OCI 镜像仓库清理预演核心逻辑。

包含：盘点 JSON 解析与校验、可达性计算、退役预演（删除计划）、
下载清理清单前的复核（冲突检测）。本模块不依赖 FastAPI / SQLite，便于单测。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

# OCI digest 格式：algorithm ":" hex，例如 sha256:<64 hex>
DIGEST_RE = re.compile(r"^[a-z0-9]+([+._-][a-z0-9]+)*:[0-9a-fA-F]{32,128}$")

# 已知对象类型；未知类型也接受，原样保留
KNOWN_TYPES = {"index", "manifest", "config", "layer", "blob"}

MAX_ERRORS = 30


class InventoryError(ValueError):
    """盘点 JSON 校验失败，errors 为全部错误描述。"""

    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("; ".join(errors[:5]))


@dataclass
class ParsedInventory:
    captured_at: str | None
    # repo -> {tag -> digest}
    tags: dict[str, dict[str, str]] = field(default_factory=dict)
    # 按摘要保留的版本（GC 额外根），全局 + 各仓库合并
    retained: set[str] = field(default_factory=set)
    # digest -> {"type": str, "size": int}
    objects: dict[str, dict] = field(default_factory=dict)
    # src_digest -> [dst_digest]
    edges: dict[str, list[str]] = field(default_factory=dict)
    # 被引用但 objects 中缺失的摘要（非空则盘点不完整）
    missing: list[str] = field(default_factory=list)


def _is_digest(value: object) -> bool:
    return isinstance(value, str) and bool(DIGEST_RE.match(value))


def parse_inventory(text: str) -> ParsedInventory:
    """解析并校验盘点 JSON。格式错误抛 InventoryError。

    期望格式：
    {
      "captured_at": "...",                # 可选
      "retained": ["sha256:..."],          # 可选，全局按摘要保留
      "repositories": [
        {"name": "repo", "tags": {"t": "sha256:..."}, "retained": ["sha256:..."]}
      ],
      "objects": {
        "sha256:...": {"type": "manifest", "size": 123, "refs": ["sha256:..."]}
      }
    }
    被引用但不在 objects 中的摘要不算导入错误，而是记入 missing（盘点不完整）。
    """
    errors: list[str] = []

    def err(msg: str) -> None:
        if len(errors) < MAX_ERRORS:
            errors.append(msg)

    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise InventoryError([f"JSON 解析失败: {exc}"]) from exc

    if not isinstance(raw, dict):
        raise InventoryError(["顶层必须是 JSON 对象"])

    parsed = ParsedInventory(captured_at=None)
    captured_at = raw.get("captured_at")
    if captured_at is not None and not isinstance(captured_at, str):
        err("captured_at 必须是字符串")
    else:
        parsed.captured_at = captured_at

    def add_retained(value: object, where: str) -> None:
        if value is None:
            return
        if not isinstance(value, list):
            err(f"{where}.retained 必须是数组")
            return
        for i, d in enumerate(value):
            if not _is_digest(d):
                err(f"{where}.retained[{i}] 不是合法摘要: {d!r}")
            else:
                parsed.retained.add(d)

    add_retained(raw.get("retained"), "顶层")

    repos = raw.get("repositories")
    if not isinstance(repos, list):
        err("repositories 必须是数组")
        repos = []
    seen_repos: set[str] = set()
    for i, repo in enumerate(repos):
        where = f"repositories[{i}]"
        if not isinstance(repo, dict):
            err(f"{where} 必须是对象")
            continue
        name = repo.get("name")
        if not isinstance(name, str) or not name.strip():
            err(f"{where}.name 缺失或为空")
            continue
        name = name.strip()
        if name in seen_repos:
            err(f"仓库名重复: {name}")
            continue
        seen_repos.add(name)
        tag_map: dict[str, str] = {}
        tags = repo.get("tags", {})
        if not isinstance(tags, dict):
            err(f"{where}.tags 必须是对象")
            tags = {}
        for tag, digest in tags.items():
            if not isinstance(tag, str) or not tag:
                err(f"{where}.tags 含空标签名")
                continue
            if not _is_digest(digest):
                err(f"标签 {name}:{tag} 的指向不是合法摘要: {digest!r}")
                continue
            tag_map[tag] = digest
        parsed.tags[name] = tag_map
        add_retained(repo.get("retained"), f"仓库 {name}")

    objects = raw.get("objects")
    if not isinstance(objects, dict):
        err("objects 必须是对象")
        objects = {}
    for digest, meta in objects.items():
        if not _is_digest(digest):
            err(f"objects 的键不是合法摘要: {digest!r}")
            continue
        if not isinstance(meta, dict):
            err(f"对象 {digest} 的元数据必须是对象")
            continue
        size = meta.get("size")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            err(f"对象 {digest} 的 size 必须是非负整数")
            continue
        otype = meta.get("type", "blob")
        if not isinstance(otype, str) or not otype:
            err(f"对象 {digest} 的 type 必须是非空字符串")
            continue
        refs = meta.get("refs", [])
        if not isinstance(refs, list):
            err(f"对象 {digest} 的 refs 必须是数组")
            continue
        ok_refs: list[str] = []
        bad = False
        for r in refs:
            if not _is_digest(r):
                err(f"对象 {digest} 引用了非法摘要: {r!r}")
                bad = True
            else:
                ok_refs.append(r)
        if bad:
            continue
        parsed.objects[digest] = {"type": otype, "size": size}
        parsed.edges[digest] = ok_refs

    if errors:
        raise InventoryError(errors)

    # 完整性：所有被引用的摘要（标签、保留、对象 refs）都必须存在于 objects
    referenced: set[str] = set(parsed.retained)
    for tag_map in parsed.tags.values():
        referenced.update(tag_map.values())
    for dsts in parsed.edges.values():
        referenced.update(dsts)
    parsed.missing = sorted(d for d in referenced if d not in parsed.objects)
    return parsed


def compute_reachable(objects: dict, edges: dict[str, list[str]], roots: set[str]) -> set[str]:
    """从根集合出发沿引用边可达的对象集合（只统计 objects 中存在的）。"""
    seen: set[str] = set()
    stack = [r for r in roots if r in objects]
    while stack:
        cur = stack.pop()
        if cur in seen:
            continue
        seen.add(cur)
        stack.extend(d for d in edges.get(cur, ()) if d not in seen and d in objects)
    return seen


@dataclass
class Graph:
    """一份盘点在内存中的图结构。"""

    objects: dict[str, dict]  # digest -> {"type","size"}
    edges: dict[str, list[str]]
    tags: dict[tuple[str, str], str]  # (repo, tag) -> digest
    retained: set[str]


def plan_deletion(graph: Graph, selected: set[tuple[str, str]]) -> tuple[list[dict], int]:
    """计算退役 selected 标签后，仅因此不再可达的对象。

    返回 (deletes, total_bytes)；deletes 每项 {digest,type,size}，按摘要排序。
    跨仓共享对象天然只计一次（对象表按摘要全局唯一）。
    """
    unknown = sorted(f"{r}:{t}" for r, t in selected if (r, t) not in graph.tags)
    if unknown:
        raise InventoryError([f"标签不在当前盘点中: {', '.join(unknown)}"])

    roots_before = set(graph.tags.values()) | graph.retained
    remaining = {k: v for k, v in graph.tags.items() if k not in selected}
    roots_after = set(remaining.values()) | graph.retained

    before = compute_reachable(graph.objects, graph.edges, roots_before)
    after = compute_reachable(graph.objects, graph.edges, roots_after)

    deletes = []
    total = 0
    for digest in sorted(before - after):
        meta = graph.objects[digest]
        deletes.append({"digest": digest, "type": meta["type"], "size": meta["size"]})
        total += meta["size"]
    return deletes, total


@dataclass
class RehearsalView:
    rehearsal_id: int
    inventory_id: int
    tags: list[dict]  # [{repo, tag, digest}] digest 为预演时原指向
    deletes: list[dict]  # [{digest, type, size}]


def validate_download(rehearsal: RehearsalView, current: Graph) -> list[dict]:
    """用最新盘点复核预演结果，返回冲突列表（空 = 可发单）。

    冲突类型：
      tag_missing      所选标签已不存在
      tag_moved        所选标签指向有变
      object_missing   待删对象在最新盘点中已缺失
      object_reachable 待删对象重新可达
    无关变动（其它标签/新增对象）不产生冲突。
    """
    conflicts: list[dict] = []

    retired = set()
    for t in rehearsal.tags:
        key = (t["repo"], t["tag"])
        retired.add(key)
        now = current.tags.get(key)
        if now is None:
            conflicts.append({
                "type": "tag_missing",
                "repo": t["repo"],
                "tag": t["tag"],
                "was": t["digest"],
                "detail": f"标签 {t['repo']}:{t['tag']} 已不存在（预演时指向 {t['digest']}）",
            })
        elif now != t["digest"]:
            conflicts.append({
                "type": "tag_moved",
                "repo": t["repo"],
                "tag": t["tag"],
                "was": t["digest"],
                "now": now,
                "detail": f"标签 {t['repo']}:{t['tag']} 指向有变：{t['digest']} -> {now}",
            })

    remaining = {k: v for k, v in current.tags.items() if k not in retired}
    roots_after = set(remaining.values()) | current.retained
    reachable_after = compute_reachable(current.objects, current.edges, roots_after)

    for d in rehearsal.deletes:
        digest = d["digest"]
        if digest not in current.objects:
            conflicts.append({
                "type": "object_missing",
                "digest": digest,
                "detail": f"待删对象 {digest} 在最新盘点中已缺失",
            })
        elif digest in reachable_after:
            conflicts.append({
                "type": "object_reachable",
                "digest": digest,
                "detail": f"待删对象 {digest} 重新可达（可能有新标签/保留指向它）",
            })
    return conflicts
