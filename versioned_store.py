"""版本化目录存储：持久身份、三路合并、冲突保存与按修订读取。

现场记录终端可能从同一目录版本出发离线工作，重连后按批次上报变更。本模块提供：

- 持久身份：文件与目录在创建时获得 id，改字节、改名、搬移、删除都按 id 引用；
- 三路合并：批次引用本地看到的基准修订，互不干涉的变化自动合并；
- 冲突保存：双方版本都存入冲突记录并指出范围，解决时必须点名候选批次与当下修订；
- 一致读取：目录可按修订读取，读者持有打开时的快照，之后的合并不影响它；
- 分批上报：字节到齐并验明后才形成可查询的新修订，缺少内容让终端续传，提交可安全重送；
- 垃圾清理：只清理没有可读版本、待处理冲突或续传需要的内容块。

状态持久化在数据目录：state.json 记录修订、批次、冲突与读者，blobs/ 按内容哈希存字节。
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path

ROOT_ID = "/"


def _valid_name(name):
    return isinstance(name, str) and name not in ("", ".", "..") and "/" not in name


def _subtree_ids(nodes, root):
    """nodes 中 root 及其全部后代的 id 集合。"""
    if root not in nodes:
        return set()
    found = {root}
    frontier = [root]
    while frontier:
        current = frontier.pop()
        for nid, state in nodes.items():
            if state["parent"] == current and nid not in found:
                found.add(nid)
                frontier.append(nid)
    return found


def _path_of(nodes, nid):
    """节点在某一修订视图中的绝对路径；不存在时返回 None。"""
    if nid not in nodes:
        return None
    parts = []
    current = nid
    while current != ROOT_ID:
        state = nodes.get(current)
        if state is None:
            return None
        parts.append(state["name"])
        current = state["parent"]
    return "/" + "/".join(reversed(parts))


def _diff(before, after):
    """两个节点视图之间的差异：id -> (之前状态, 之后状态)。"""
    changes = {}
    for nid in set(before) | set(after):
        if before.get(nid) != after.get(nid):
            changes[nid] = (before.get(nid), after.get(nid))
    return changes


def _apply_within(subtree, changes):
    """把 changes 中落在 subtree 内的部分应用上去（用于构造冲突某一侧的版本）。"""
    progressed = True
    done = set()
    while progressed:
        progressed = False
        for nid, (_before, after) in changes.items():
            if nid in done:
                continue
            if after is None:
                if nid in subtree:
                    for x in _subtree_ids(subtree, nid):
                        subtree.pop(x, None)
                    done.add(nid)
                    progressed = True
            elif nid in subtree or after["parent"] in subtree:
                subtree[nid] = copy.deepcopy(after)
                done.add(nid)
                progressed = True
    return subtree


class BlobStore:
    """按内容寻址的字节块存储，相同内容只存一份。"""

    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def digest(data):
        return hashlib.sha256(data).hexdigest()

    def put(self, data):
        digest = self.digest(data)
        path = self.directory / digest
        if not path.exists():
            tmp = self.directory / (digest + ".tmp")
            tmp.write_bytes(data)
            os.replace(tmp, path)
        return digest

    def has(self, digest):
        return (self.directory / digest).is_file()

    def get(self, digest):
        return (self.directory / digest).read_bytes()

    def all_hashes(self):
        return {
            p.name
            for p in self.directory.iterdir()
            if p.is_file() and not p.name.endswith(".tmp")
        }

    def remove(self, digest):
        path = self.directory / digest
        if path.exists():
            path.unlink()


class DirectoryService:
    """版本化目录服务。所有方法返回 dict，ok 字段表示成败。"""

    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.blobs = BlobStore(self.directory / "blobs")
        self._state_file = self.directory / "state.json"
        if self._state_file.exists():
            state = json.loads(self._state_file.read_text(encoding="utf-8"))
        else:
            state = {
                "head": None,
                "revs": {},
                "batches": {},
                "conflicts": {},
                "readers": {},
                "pending_blobs": {},
                "seq": 0,
            }
        self.head = state["head"]
        self.revs = {int(k): v for k, v in state["revs"].items()}
        self.batches = state["batches"]
        self.conflicts = state["conflicts"]
        self.readers = state["readers"]
        self.pending_blobs = state["pending_blobs"]
        self.seq = state["seq"]

    def _save(self):
        state = {
            "head": self.head,
            "revs": {str(k): v for k, v in self.revs.items()},
            "batches": self.batches,
            "conflicts": self.conflicts,
            "readers": self.readers,
            "pending_blobs": self.pending_blobs,
            "seq": self.seq,
        }
        tmp = self.directory / "state.json.tmp"
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, self._state_file)

    # ------------------------------------------------------------------
    # 初始目录
    # ------------------------------------------------------------------

    def root(self, rev, nodes):
        """建立初始目录修订。nodes 中文件节点可带 text 作为初始内容。"""
        if self.head is not None:
            return {"ok": False, "error": "初始目录已建立"}
        if not isinstance(rev, int) or rev < 1:
            return {"ok": False, "error": "rev 应为正整数"}
        tree = {ROOT_ID: {"parent": None, "name": "", "kind": "d", "content": None}}
        seen = set()
        for node in nodes:
            nid = node.get("id")
            if not nid or nid == ROOT_ID or nid in seen:
                return {"ok": False, "error": f"节点 id 无效或重复：{nid!r}"}
            seen.add(nid)
        for node in nodes:
            kind = node.get("kind")
            if kind not in ("d", "f"):
                return {"ok": False, "error": f"节点 {node.get('id')} 类型应为 d 或 f"}
            if not _valid_name(node.get("name")):
                return {"ok": False, "error": f"节点 {node.get('id')} 名称无效"}
            content = None
            if kind == "f":
                text = node.get("text", "")
                if not isinstance(text, str):
                    return {"ok": False, "error": f"节点 {node.get('id')} 的 text 应为字符串"}
                content = self.blobs.put(text.encode("utf-8"))
            tree[node["id"]] = {
                "parent": node.get("parent"),
                "name": node["name"],
                "kind": kind,
                "content": content,
            }
        for nid, state in tree.items():
            if nid == ROOT_ID:
                continue
            parent = state["parent"]
            if parent not in tree:
                return {"ok": False, "error": f"节点 {nid} 的父节点 {parent} 不存在"}
            if tree[parent]["kind"] != "d":
                return {"ok": False, "error": f"节点 {nid} 的父节点 {parent} 不是目录"}
        names = set()
        for nid, state in tree.items():
            if nid == ROOT_ID:
                continue
            key = (state["parent"], state["name"])
            if key in names:
                return {"ok": False, "error": f"同级出现同名节点 {state['name']}"}
            names.add(key)
        for nid in tree:
            chain, current = set(), nid
            while current is not None:
                if current in chain:
                    return {"ok": False, "error": "初始目录存在环"}
                chain.add(current)
                current = tree[current]["parent"]
            if ROOT_ID not in chain:
                return {"ok": False, "error": "初始目录存在游离节点"}
        self.revs[rev] = {"nodes": tree, "cause": {"root": True}, "origin": {}}
        self.head = rev
        self._save()
        return {"ok": True, "rev": rev, "nodes": len(tree) - 1}

    # ------------------------------------------------------------------
    # 离线变更登记与字节补交
    # ------------------------------------------------------------------

    def _needs_blob(self, op):
        return op["op"] == "write" or (op["op"] == "create" and op.get("kind") == "f")

    def stage(self, client, batch, base, ops):
        """登记一批离线变更。重送同一批次（内容完全一致）是安全的。"""
        if batch in self.batches:
            entry = self.batches[batch]
            if entry["client"] == client and entry["base"] == base and entry["ops"] == ops:
                return {"ok": True, "batch": batch, "status": entry["status"], "repeat": True}
            return {"ok": False, "error": f"批次 {batch} 已登记且内容不同"}
        if base not in self.revs:
            return {"ok": False, "error": f"基准修订 {base} 不存在"}
        if not isinstance(ops, list):
            return {"ok": False, "error": "ops 应为列表"}
        _nodes, _changes, error = self._apply_ops(
            self.revs[base]["nodes"], ops, content_of=lambda op: None
        )
        if error:
            return {"ok": False, "error": error}
        blobs = self.pending_blobs.pop(batch, {})
        self.batches[batch] = {
            "client": client,
            "base": base,
            "ops": copy.deepcopy(ops),
            "blobs": blobs,
            "status": "staged",
            "result": None,
        }
        self._save()
        return {"ok": True, "batch": batch, "status": "staged"}

    def add_blob(self, batch, name, data):
        """为指定批次补交一块字节。内容按哈希验明并去重存储。"""
        digest = self.blobs.put(data)
        entry = self.batches.get(batch)
        if entry is not None:
            entry["blobs"][name] = digest
        else:
            self.pending_blobs.setdefault(batch, {})[name] = digest
        self._save()
        return {"ok": True, "batch": batch, "blob": name, "hash": digest}

    # ------------------------------------------------------------------
    # 操作应用与三路合并
    # ------------------------------------------------------------------

    def _apply_ops(self, nodes, ops, content_of):
        """把一批操作依次应用到 nodes 上，返回 (新视图, 净变更, 错误)。"""
        nodes = copy.deepcopy(nodes)
        changes = {}

        def remember(nid):
            if nid not in changes:
                changes[nid] = copy.deepcopy(nodes.get(nid))

        for index, op in enumerate(ops):
            if not isinstance(op, dict) or "op" not in op:
                return None, None, f"第 {index} 个操作缺少 op 字段"
            kind = op["op"]
            nid = op.get("id")
            if kind == "create":
                parent, name, nkind = op.get("parent"), op.get("name"), op.get("kind")
                if not nid or nid == ROOT_ID:
                    return None, None, f"第 {index} 个操作：节点 id 无效"
                if nid in nodes:
                    return None, None, f"第 {index} 个操作：节点 {nid} 已存在"
                if nkind not in ("d", "f"):
                    return None, None, f"第 {index} 个操作：类型应为 d 或 f"
                if not _valid_name(name):
                    return None, None, f"第 {index} 个操作：名称 {name!r} 无效"
                if parent not in nodes:
                    return None, None, f"第 {index} 个操作：父节点 {parent} 不存在"
                if nodes[parent]["kind"] != "d":
                    return None, None, f"第 {index} 个操作：父节点 {parent} 不是目录"
                if nkind == "f" and not op.get("blob"):
                    return None, None, f"第 {index} 个操作：文件创建缺少 blob 引用"
                if any(s["parent"] == parent and s["name"] == name for s in nodes.values()):
                    return None, None, f"第 {index} 个操作：{parent} 下已有同名节点 {name}"
                remember(nid)
                nodes[nid] = {
                    "parent": parent,
                    "name": name,
                    "kind": nkind,
                    "content": content_of(op) if nkind == "f" else None,
                }
            elif kind == "write":
                if nid not in nodes:
                    return None, None, f"第 {index} 个操作：节点 {nid} 不存在"
                if nodes[nid]["kind"] != "f":
                    return None, None, f"第 {index} 个操作：{nid} 不是文件"
                if not op.get("blob"):
                    return None, None, f"第 {index} 个操作：写入缺少 blob 引用"
                remember(nid)
                nodes[nid]["content"] = content_of(op)
            elif kind == "move":
                parent, name = op.get("parent"), op.get("name")
                if nid == ROOT_ID:
                    return None, None, f"第 {index} 个操作：根目录不能移动"
                if nid not in nodes:
                    return None, None, f"第 {index} 个操作：节点 {nid} 不存在"
                if not _valid_name(name):
                    return None, None, f"第 {index} 个操作：名称 {name!r} 无效"
                if parent not in nodes:
                    return None, None, f"第 {index} 个操作：父节点 {parent} 不存在"
                if nodes[parent]["kind"] != "d":
                    return None, None, f"第 {index} 个操作：父节点 {parent} 不是目录"
                walk = parent
                while walk is not None:
                    if walk == nid:
                        return None, None, f"第 {index} 个操作：目录不能移到自身后代"
                    walk = nodes[walk]["parent"]
                if any(
                    i != nid and s["parent"] == parent and s["name"] == name
                    for i, s in nodes.items()
                ):
                    return None, None, f"第 {index} 个操作：目标位置已有同名节点 {name}"
                remember(nid)
                nodes[nid]["parent"] = parent
                nodes[nid]["name"] = name
            elif kind == "delete":
                if nid == ROOT_ID:
                    return None, None, f"第 {index} 个操作：根目录不能删除"
                if nid not in nodes:
                    return None, None, f"第 {index} 个操作：节点 {nid} 不存在"
                for x in _subtree_ids(nodes, nid):
                    remember(x)
                for x in _subtree_ids(nodes, nid):
                    del nodes[x]
            else:
                return None, None, f"第 {index} 个操作：未知操作 {kind!r}"
        net = {}
        for nid, before in changes.items():
            after = copy.deepcopy(nodes.get(nid))
            if before != after:
                net[nid] = (before, after)
        return nodes, net, None

    def _attribute(self, nid, base_rev):
        """nid 在 (base_rev, head] 区间内最后一次变更来自哪个批次。"""
        for rev in range(self.head, base_rev, -1):
            if rev in self.revs and nid in self.revs[rev].get("origin", {}):
                return self.revs[rev]["origin"][nid]
        return "?"

    def _merge(self, base_rev, base_nodes, head_nodes, my_nodes, my_changes, their_changes, batch_id, ops):
        """把 my_changes 三路合并到 head_nodes 上。

        返回 (合并后视图, 已应用的 id 列表, 新冲突列表, 加入既有冲突的 cid 列表)。
        """
        merged = copy.deepcopy(head_nodes)
        applied = []
        specs = []
        joined = []
        held = set()

        def head_subtree(root):
            if root not in head_nodes:
                return None
            return {i: copy.deepcopy(head_nodes[i]) for i in _subtree_ids(head_nodes, root)}

        def my_view_subtree(scope):
            """我方视角下 scope 子树的版本：基准子树加上我方落在其中的变更。"""
            if scope in my_changes and my_changes[scope][1] is None:
                return None
            if scope not in base_nodes:
                return None
            sub = {i: copy.deepcopy(base_nodes[i]) for i in _subtree_ids(base_nodes, scope)}
            return _apply_within(sub, my_changes)

        def make_spec(kind, scope, my_side, their_side, their_batch, extra_ids=(), extra_paths=()):
            ids = {scope} | set(extra_ids)
            for side in (my_side, their_side):
                if side:
                    ids |= set(side)
            paths = set(extra_paths)
            base_path = _path_of(base_nodes, scope)
            if base_path:
                paths.add(f"{base_path}（基准）")
            head_path = _path_of(head_nodes, scope)
            if head_path:
                paths.add(f"{head_path}（当前）")
            return {
                "kind": kind,
                "scope": scope,
                "ids": sorted(ids),
                "paths": sorted(paths),
                "sides": [
                    {"batch": their_batch, "subtree": their_side},
                    {"batch": batch_id, "subtree": my_side},
                ],
                "base_subtree": (
                    {i: copy.deepcopy(base_nodes[i]) for i in _subtree_ids(base_nodes, scope)}
                    if scope in base_nodes
                    else None
                ),
                "ops": sorted(i for i, op in enumerate(ops) if op.get("id") in ids),
            }

        # 1) 触及待解决冲突范围的变更并入该冲突，成为新的候选版本，范围保持冻结。
        pending = [c for c in self.conflicts.values() if c["status"] == "pending"]
        for nid, (_before, after) in list(my_changes.items()):
            if nid in held:
                continue
            target = None
            for conflict in pending:
                cids = set(conflict["ids"])
                hit = nid in cids
                if not hit and after is None and nid in base_nodes:
                    hit = bool(_subtree_ids(base_nodes, nid) & cids)
                if not hit and after is not None:
                    walk = after["parent"]
                    while walk is not None:
                        if walk in cids:
                            hit = True
                            break
                        state = my_nodes.get(walk)
                        walk = state["parent"] if state else None
                if hit:
                    target = conflict
                    break
            if target is None:
                continue
            scope = target["scope"]
            if after is None and nid in base_nodes and scope in _subtree_ids(base_nodes, nid):
                side_subtree = None  # 我方删除了冲突范围（或其祖先）
            else:
                if scope in head_nodes:
                    current = head_subtree(scope)
                else:
                    current = copy.deepcopy(target.get("base_subtree")) or {}
                side_subtree = _apply_within(current, my_changes)
            target["sides"].append({"batch": batch_id, "subtree": side_subtree})
            side_ids = set(side_subtree) if side_subtree else set()
            all_ids = set(target["ids"]) | side_ids
            target["ids"] = sorted(all_ids)
            base_scope = _subtree_ids(base_nodes, scope) if scope in base_nodes else set()
            for mid, (_mb, mafter) in my_changes.items():
                if mafter is None and mid in base_scope:
                    held.add(mid)
                elif mafter is not None and mid in side_ids:
                    held.add(mid)
            held.add(nid)
            if after is None and nid in base_nodes:
                held |= _subtree_ids(base_nodes, nid) & set(my_changes)
            joined.append(target["cid"])

        # 2) 常规三路合并。
        for nid, (before, after) in my_changes.items():
            if nid in held:
                continue
            if nid in their_changes:
                _tbefore, tafter = their_changes[nid]
                if after == tafter:
                    continue  # 双方做了相同变更
                if after is None or tafter is None:
                    # 一方删除、另一方修改（含改名）：删除不能被来晚的变更静默复活。
                    if after is None:
                        my_side, their_side = None, head_subtree(nid)
                        held |= _subtree_ids(base_nodes, nid) & set(my_changes)
                    else:
                        my_side, their_side = my_view_subtree(nid), None
                    specs.append(make_spec("delete", nid, my_side, their_side, self._attribute(nid, base_rev)))
                    held.add(nid)
                    continue
                if before is None:
                    # 双方用同一 id 分别创建
                    specs.append(make_spec(
                        "create", nid, {nid: copy.deepcopy(after)}, {nid: copy.deepcopy(tafter)},
                        self._attribute(nid, base_rev),
                    ))
                    held.add(nid)
                    continue
                my_loc = (after["parent"], after["name"])
                their_loc = (tafter["parent"], tafter["name"])
                base_loc = (before["parent"], before["name"])
                loc_conflict = my_loc != their_loc and my_loc != base_loc and their_loc != base_loc
                ct_conflict = (
                    after["content"] != tafter["content"]
                    and after["content"] != before["content"]
                    and tafter["content"] != before["content"]
                )
                if loc_conflict or ct_conflict:
                    kind = "content" if ct_conflict else "rename"
                    specs.append(make_spec(
                        kind, nid, {nid: copy.deepcopy(after)}, {nid: copy.deepcopy(tafter)},
                        self._attribute(nid, base_rev),
                        extra_paths=[_path_of(my_nodes, nid)],
                    ))
                    held.add(nid)
                    continue
                # 双方改了不同侧面（如一方改名、另一方写内容）：自动合并
                final = copy.deepcopy(after)
                if their_loc != base_loc:
                    final["parent"], final["name"] = their_loc
                if tafter["content"] != before["content"]:
                    final["content"] = tafter["content"]
                merged[nid] = final
                applied.append(nid)
                continue
            if after is None:
                # 我方删除整棵子树：若对方在其中改过或新增过，整棵子树挂起为冲突。
                subtree = _subtree_ids(base_nodes, nid)
                interferers = [
                    x for x in subtree
                    if x in their_changes and their_changes[x][1] is not None
                ]
                for x, (tb, ta) in their_changes.items():
                    if tb is None and ta is not None:
                        walk = ta["parent"]
                        while walk is not None:
                            if walk == nid:
                                interferers.append(x)
                                break
                            state = head_nodes.get(walk)
                            walk = state["parent"] if state else None
                if interferers:
                    specs.append(make_spec(
                        "delete", nid, None, head_subtree(nid),
                        self._attribute(interferers[0], base_rev),
                    ))
                    held.add(nid)
                    held |= subtree & set(my_changes)
                    continue
                for x in subtree:
                    merged.pop(x, None)
                applied.append(nid)
                continue
            # 我方让节点存在（创建/写入/搬移）：检查祖先、环与同名。
            walk = after["parent"]
            missing = None
            while walk is not None:
                if walk not in merged:
                    missing = walk
                    break
                walk = merged[walk]["parent"]
            if missing is not None:
                scope = missing
                while True:
                    state = base_nodes.get(scope)
                    if state is None:
                        break
                    parent = state["parent"]
                    if parent in their_changes and their_changes[parent][1] is None:
                        scope = parent
                    else:
                        break
                if any(s["scope"] == scope and s["kind"] == "delete" for s in specs):
                    held.add(nid)
                    continue
                specs.append(make_spec(
                    "delete", scope, my_view_subtree(scope), None,
                    self._attribute(scope, base_rev),
                    extra_paths=[_path_of(my_nodes, nid)],
                ))
                held.add(nid)
                continue
            walk = after["parent"]
            cycle = False
            while walk is not None:
                if walk == nid:
                    cycle = True
                    break
                state = merged.get(walk)
                walk = state["parent"] if state else None
            if cycle:
                # 环由对方在窗口内的搬动造成：沿环链找到责任批次作为当前侧候选。
                their_batch = "?"
                walk = after["parent"]
                while walk is not None and walk != nid:
                    candidate = self._attribute(walk, base_rev)
                    if candidate != "?":
                        their_batch = candidate
                        break
                    state = merged.get(walk)
                    walk = state["parent"] if state else None
                specs.append(make_spec(
                    "move-cycle", nid, {nid: copy.deepcopy(after)},
                    {nid: copy.deepcopy(head_nodes[nid])} if nid in head_nodes else None,
                    their_batch,
                ))
                held.add(nid)
                continue
            collided = None
            for oid, state in merged.items():
                if oid != nid and state["parent"] == after["parent"] and state["name"] == after["name"]:
                    collided = oid
                    break
            if collided is not None:
                specs.append(make_spec(
                    "path-collision", nid, {nid: copy.deepcopy(after)},
                    {collided: copy.deepcopy(merged[collided])},
                    self._attribute(collided, base_rev),
                    extra_ids={collided},
                    extra_paths=[_path_of(merged, collided)],
                ))
                held.add(nid)
                continue
            merged[nid] = copy.deepcopy(after)
            applied.append(nid)
        return merged, applied, specs, joined

    # ------------------------------------------------------------------
    # 提交与冲突解决
    # ------------------------------------------------------------------

    def commit(self, batch):
        """尝试发布批次。可安全重送：已提交的批次直接返回首次结果。"""
        entry = self.batches.get(batch)
        if entry is None:
            return {"ok": False, "error": f"未知批次 {batch}"}
        if entry["status"] == "committed":
            return dict(entry["result"], repeat=True)
        needed = [op["blob"] for op in entry["ops"] if self._needs_blob(op)]
        missing = sorted({name for name in needed if name not in entry["blobs"]})
        if missing:
            return {"ok": False, "error": "缺少字节，请续传该批", "missing": missing}

        def content_of(op):
            return entry["blobs"].get(op.get("blob"))

        base_rev = entry["base"]
        base_nodes = self.revs[base_rev]["nodes"]
        my_nodes, my_changes, error = self._apply_ops(base_nodes, entry["ops"], content_of)
        if error:
            return {"ok": False, "error": error}
        head_nodes = self.revs[self.head]["nodes"]
        their_changes = _diff(base_nodes, head_nodes)
        merged_nodes, applied, specs, joined = self._merge(
            base_rev, base_nodes, head_nodes, my_nodes, my_changes,
            their_changes, batch, entry["ops"],
        )
        opened = []
        for spec in specs:
            self.seq += 1
            cid = f"c{self.seq}"
            spec["cid"] = cid
            spec["status"] = "pending"
            spec["opened_rev"] = self.head
            spec["resolved"] = None
            self.conflicts[cid] = spec
            opened.append(spec)
        if merged_nodes != head_nodes:
            new_rev = self.head + 1
            self.revs[new_rev] = {
                "nodes": merged_nodes,
                "cause": {"batch": batch},
                "origin": {nid: batch for nid in applied},
            }
            self.head = new_rev
        result = {
            "ok": True,
            "batch": batch,
            "rev": self.head,
            "conflicts": [
                {"cid": s["cid"], "kind": s["kind"], "scope": s["scope"], "paths": s["paths"]}
                for s in opened
            ],
            "joined": sorted(set(joined)),
        }
        if not my_changes:
            result["unchanged"] = True
        entry["status"] = "committed"
        entry["result"] = result
        self._save()
        return result

    def resolve(self, cid, candidates, winning_batch, expected):
        """解决冲突：必须点名全部候选批次，且 expected 等于当下目录修订。"""
        conflict = self.conflicts.get(cid)
        if conflict is None:
            return {"ok": False, "error": f"未知冲突 {cid}"}
        if conflict["status"] == "resolved":
            record = conflict["resolved"]
            if record["winner"] == winning_batch and sorted(candidates) == sorted(record["candidates"]):
                return {"ok": True, "cid": cid, "rev": record["rev"], "winner": winning_batch, "repeat": True}
            return {"ok": False, "error": f"冲突 {cid} 已解决（胜者 {record['winner']}）"}
        if expected != self.head:
            return {
                "ok": False,
                "error": f"期望修订 {expected} 与当前 {self.head} 不符，选择已过期，请按最新修订重新选择",
            }
        known = [s["batch"] for s in conflict["sides"]]
        if sorted(candidates) != sorted(known):
            return {"ok": False, "error": "候选批次与冲突记录不符", "candidates": known}
        if winning_batch not in known:
            return {"ok": False, "error": f"winning_batch {winning_batch} 不在候选中"}
        side = next(s for s in conflict["sides"] if s["batch"] == winning_batch)
        head_nodes = self.revs[self.head]["nodes"]
        new_nodes = copy.deepcopy(head_nodes)
        remove = set()
        for s in conflict["sides"]:
            if s["subtree"]:
                remove |= set(s["subtree"])
        for nid in remove:
            new_nodes.pop(nid, None)
        if side["subtree"]:
            subtree = copy.deepcopy(side["subtree"])
            roots = [i for i, st in subtree.items() if st["parent"] not in subtree]
            for root_id in roots:
                state = subtree[root_id]
                parent = state["parent"]
                if parent not in new_nodes:
                    return {"ok": False, "error": f"冲突范围的父位置 {parent} 已不存在"}
                if any(
                    i != root_id and s2["parent"] == parent and s2["name"] == state["name"]
                    for i, s2 in new_nodes.items()
                ):
                    return {"ok": False, "error": f"目标路径已被占用：{state['name']}"}
                walk = parent
                while walk is not None:
                    if walk == root_id or walk in subtree:
                        return {"ok": False, "error": "安装该版本会形成环"}
                    walk = new_nodes[walk]["parent"]
            new_nodes.update(subtree)
        new_rev = self.head + 1
        self.revs[new_rev] = {
            "nodes": new_nodes,
            "cause": {"resolve": cid, "winner": winning_batch},
            "origin": {i: winning_batch for i in (side["subtree"] or {})},
        }
        self.head = new_rev
        conflict["status"] = "resolved"
        conflict["resolved"] = {"rev": new_rev, "winner": winning_batch, "candidates": list(candidates)}
        self._save()
        return {"ok": True, "cid": cid, "rev": new_rev, "winner": winning_batch}

    # ------------------------------------------------------------------
    # 拉取、读取与读者快照
    # ------------------------------------------------------------------

    def pull(self, since=None):
        """拉取目录状态。给出 since 时只返回相对该修订的差异与所需字节。"""
        head_nodes = self.revs[self.head]["nodes"]
        out = {"ok": True, "rev": self.head}
        if since is not None and since in self.revs and since != self.head:
            changes = _diff(self.revs[since]["nodes"], head_nodes)
            out["changes"] = {
                nid: {"before": before, "after": after} for nid, (before, after) in changes.items()
            }
            hashes = {a["content"] for _b, a in changes.values() if a and a.get("content")}
        else:
            out["nodes"] = copy.deepcopy(head_nodes)
            out["paths"] = {nid: _path_of(head_nodes, nid) for nid in head_nodes}
            hashes = {s["content"] for s in head_nodes.values() if s["content"]}
        out["blobs"] = {h: self.blobs.get(h).decode("utf-8") for h in sorted(hashes)}
        return out

    def open_reader(self, reader, rev):
        """为读者建立指定修订的快照。"""
        if rev not in self.revs:
            return {"ok": False, "error": f"修订 {rev} 不存在"}
        self.readers[reader] = rev
        self._save()
        return {"ok": True, "reader": reader, "rev": rev}

    def read_path(self, reader, path):
        """按读者快照中的路径读取：文件返回内容，目录返回列表。"""
        if reader not in self.readers:
            return {"ok": False, "error": f"未知读者 {reader}"}
        rev = self.readers[reader]
        nodes = self.revs[rev]["nodes"]
        if not isinstance(path, str) or not path.startswith("/"):
            return {"ok": False, "error": f"路径无效：{path!r}"}
        current = ROOT_ID
        for segment in [s for s in path.split("/") if s]:
            nxt = next(
                (i for i, s in nodes.items() if s["parent"] == current and s["name"] == segment),
                None,
            )
            if nxt is None:
                return {"ok": False, "error": f"路径不存在：{path}", "rev": rev}
            current = nxt
        state = nodes[current]
        if state["kind"] == "f":
            if not self.blobs.has(state["content"]):
                return {"ok": False, "error": "内容块缺失", "rev": rev}
            return {
                "ok": True,
                "rev": rev,
                "path": path,
                "id": current,
                "kind": "f",
                "hash": state["content"],
                "text": self.blobs.get(state["content"]).decode("utf-8"),
            }
        children = [
            {"id": i, "name": s["name"], "kind": s["kind"]}
            for i, s in nodes.items()
            if s["parent"] == current
        ]
        return {
            "ok": True,
            "rev": rev,
            "path": path,
            "id": current,
            "kind": "d",
            "children": sorted(children, key=lambda x: x["name"]),
        }

    # ------------------------------------------------------------------
    # 管理员查询
    # ------------------------------------------------------------------

    def list_conflicts(self, status="pending"):
        return [
            {
                "cid": c["cid"],
                "kind": c["kind"],
                "scope": c["scope"],
                "paths": c["paths"],
                "candidates": [s["batch"] for s in c["sides"]],
                "status": c["status"],
            }
            for c in self.conflicts.values()
            if c["status"] == status
        ]

    def _text(self, digest):
        if digest and self.blobs.has(digest):
            return self.blobs.get(digest).decode("utf-8")
        return None

    def inspect(self, batch=None, node=None, reader=None, path=None):
        if batch is not None:
            return self._inspect_batch(batch)
        if node is not None:
            return self._inspect_node(node)
        if reader is not None:
            return self._inspect_reader(reader, path)
        return self._inspect_overview()

    def _inspect_batch(self, batch):
        entry = self.batches.get(batch)
        if entry is None:
            return {"ok": False, "error": f"未知批次 {batch}"}
        needed = sorted({op["blob"] for op in entry["ops"] if self._needs_blob(op)})
        missing = [name for name in needed if name not in entry["blobs"]]
        conflicts = [
            cid for cid, c in self.conflicts.items()
            if any(s["batch"] == batch for s in c["sides"])
        ]
        return {
            "ok": True,
            "batch": batch,
            "client": entry["client"],
            "base": entry["base"],
            "status": entry["status"],
            "ops": entry["ops"],
            "blobs": dict(entry["blobs"]),
            "missing": missing,
            "conflicts": conflicts,
            "result": entry["result"],
        }

    def _inspect_node(self, nid):
        kind = None
        for rev in sorted(self.revs):
            state = self.revs[rev]["nodes"].get(nid)
            if state:
                kind = state["kind"]
                break
        if kind is None:
            for conflict in self.conflicts.values():
                for side in conflict["sides"]:
                    if side["subtree"] and nid in side["subtree"]:
                        kind = side["subtree"][nid]["kind"]
                        break
                if kind:
                    break
        if kind is None:
            return {"ok": False, "error": f"未知节点 {nid}"}
        head_nodes = self.revs[self.head]["nodes"]
        current = None
        if nid in head_nodes:
            state = head_nodes[nid]
            current = {
                "rev": self.head,
                "path": _path_of(head_nodes, nid),
                "content": state["content"],
                "text": self._text(state["content"]),
            }
        history = []
        prev_key = None
        started = False
        for rev in sorted(self.revs):
            nodes = self.revs[rev]["nodes"]
            state = nodes.get(nid)
            if not started and state is None:
                continue  # 历史从节点首次出现开始
            started = True
            path = _path_of(nodes, nid) if state else None
            content = state["content"] if state else None
            key = (path, content, state is not None)
            if key != prev_key:
                history.append({
                    "rev": rev,
                    "path": path,
                    "content": content,
                    "text": self._text(content) if content else None,
                    "cause": self.revs[rev]["cause"],
                })
                prev_key = key
        snapshots = []
        for name, rev in sorted(self.readers.items()):
            nodes = self.revs[rev]["nodes"]
            state = nodes.get(nid)
            snapshots.append({
                "reader": name,
                "rev": rev,
                "path": _path_of(nodes, nid) if state else None,
                "content": state["content"] if state else None,
                "text": self._text(state["content"]) if state and state["content"] else None,
            })
        pending = [
            cid for cid, c in self.conflicts.items()
            if c["status"] == "pending" and nid in c["ids"]
        ]
        return {
            "ok": True,
            "id": nid,
            "kind": kind,
            "current": current,
            "history": history,
            "snapshots": snapshots,
            "pending_conflicts": pending,
        }

    def _inspect_reader(self, reader, path):
        if reader not in self.readers:
            return {"ok": False, "error": f"未知读者 {reader}"}
        out = {"ok": True, "reader": reader, "rev": self.readers[reader]}
        if path is not None:
            out["read"] = self.read_path(reader, path)
        return out

    def _inspect_overview(self):
        batches = {}
        for bid, entry in self.batches.items():
            needed = sorted({op["blob"] for op in entry["ops"] if self._needs_blob(op)})
            batches[bid] = {
                "client": entry["client"],
                "status": entry["status"],
                "missing": [n for n in needed if n not in entry["blobs"]],
            }
        return {
            "ok": True,
            "head": self.head,
            "batches": batches,
            "pending_conflicts": self.list_conflicts(),
            "readers": dict(self.readers),
            "blobs_stored": len(self.blobs.all_hashes()),
        }

    # ------------------------------------------------------------------
    # 垃圾清理
    # ------------------------------------------------------------------

    def gc(self):
        """只清理没有可读版本、待处理冲突或续传需要的内容块。"""
        refs = set()
        for rev in self.revs.values():
            for state in rev["nodes"].values():
                if state["content"]:
                    refs.add(state["content"])
        for conflict in self.conflicts.values():
            if conflict["status"] != "pending":
                continue
            for side in conflict["sides"]:
                for state in (side["subtree"] or {}).values():
                    if state["content"]:
                        refs.add(state["content"])
        for entry in self.batches.values():
            if entry["status"] != "committed":
                refs |= set(entry["blobs"].values())
        for blobs in self.pending_blobs.values():
            refs |= set(blobs.values())
        collected = []
        for digest in sorted(self.blobs.all_hashes()):
            if digest not in refs:
                self.blobs.remove(digest)
                collected.append(digest)
        return {"ok": True, "collected": collected, "kept": len(self.blobs.all_hashes())}
