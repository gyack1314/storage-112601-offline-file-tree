"""版本化目录存储的行为测试：逐条对应演练要求。"""

import contextlib
import io
import shutil
import tempfile
import unittest
from pathlib import Path

from scenario_player import play
from versioned_store import DirectoryService

EXAMPLES = Path(__file__).with_name("examples")


def make_service():
    directory = tempfile.mkdtemp(prefix="versioned-store-test-")
    service = DirectoryService(directory)
    return service, directory


def boot(service, extra_nodes=()):
    nodes = [
        {"id": "d", "parent": "/", "name": "lab", "kind": "d"},
        {"id": "f", "parent": "d", "name": "run", "kind": "f", "text": "v1"},
        {"id": "u", "parent": "/", "name": "unrelated", "kind": "d"},
    ] + list(extra_nodes)
    result = service.root(1, nodes)
    assert result["ok"], result
    return result


class ServiceTestCase(unittest.TestCase):
    def setUp(self):
        self.service, self.directory = make_service()
        self.addCleanup(shutil.rmtree, self.directory, True)

    def stage_commit(self, client, batch, base, ops, blobs=()):
        staged = self.service.stage(client, batch, base, ops)
        self.assertTrue(staged["ok"], staged)
        for name, text in blobs:
            self.service.add_blob(batch, name, text.encode("utf-8"))
        return self.service.commit(batch)


class IdentityAndMergeTests(ServiceTestCase):
    def test_move_and_write_merge_and_identity_survives(self):
        boot(self.service)
        self.assertTrue(self.service.open_reader("old", 1)["ok"])
        # 终端 a 把记录夹 lab 改名为 archive
        moved = self.stage_commit("a", "move", 1, [{"op": "move", "id": "d", "parent": "/", "name": "archive"}])
        self.assertEqual(moved["rev"], 2)
        # 终端 b 基于同一基准修改其中仍有相同身份的文件
        fixed = self.stage_commit("b", "fix", 1, [{"op": "write", "id": "f", "blob": "B"}], [("B", "corrected")])
        self.assertEqual(fixed["rev"], 3)
        self.assertEqual(fixed["conflicts"], [])
        # 合并结果：新路径下读到新内容
        self.assertTrue(self.service.open_reader("now", 3)["ok"])
        read = self.service.read_path("now", "/archive/run")
        self.assertEqual((read["id"], read["text"]), ("f", "corrected"))
        # 旧读者的快照不受另一个终端合并后的文件名影响
        read_old = self.service.read_path("old", "/lab/run")
        self.assertEqual((read_old["id"], read_old["text"]), ("f", "v1"))
        gone = self.service.read_path("old", "/archive/run")
        self.assertFalse(gone["ok"])
        # 管理员能查移动前后如何指向同一身份
        report = self.service.inspect(node="f")
        self.assertEqual(report["current"]["path"], "/archive/run")
        trail = [(entry["path"], entry["text"]) for entry in report["history"]]
        self.assertEqual(
            trail,
            [("/lab/run", "v1"), ("/archive/run", "v1"), ("/archive/run", "corrected")],
        )
        snap = next(s for s in report["snapshots"] if s["reader"] == "old")
        self.assertEqual((snap["reader"], snap["rev"], snap["text"]), ("old", 1, "v1"))

    def test_rename_and_write_same_file_merge_aspectwise(self):
        boot(self.service)
        self.stage_commit("a", "ren", 1, [{"op": "move", "id": "f", "parent": "d", "name": "run2"}])
        result = self.stage_commit("b", "wr", 1, [{"op": "write", "id": "f", "blob": "B"}], [("B", "x")])
        self.assertEqual(result["conflicts"], [])
        self.assertTrue(self.service.open_reader("r", result["rev"])["ok"])
        read = self.service.read_path("r", "/lab/run2")
        self.assertEqual(read["text"], "x")

    def test_identical_changes_do_not_conflict(self):
        boot(self.service)
        self.stage_commit("a", "m1", 1, [{"op": "move", "id": "d", "parent": "/", "name": "archive"}])
        result = self.stage_commit("b", "m2", 1, [{"op": "move", "id": "d", "parent": "/", "name": "archive"}])
        self.assertEqual(result["conflicts"], [])

    def test_move_into_own_descendant_rejected(self):
        boot(self.service, [{"id": "sub", "parent": "d", "name": "sub", "kind": "d"}])
        result = self.service.stage("a", "bad", 1, [{"op": "move", "id": "d", "parent": "sub", "name": "loop"}])
        self.assertFalse(result["ok"])
        self.assertIn("自身后代", result["error"])

    def test_cross_batch_move_cycle_becomes_conflict_not_cycle(self):
        boot(self.service, [{"id": "y", "parent": "/", "name": "y", "kind": "d"}])
        self.stage_commit("a", "mx", 1, [{"op": "move", "id": "d", "parent": "y", "name": "x"}])
        result = self.stage_commit("b", "my", 1, [{"op": "move", "id": "y", "parent": "d", "name": "z"}])
        self.assertEqual([c["kind"] for c in result["conflicts"]], ["move-cycle"])
        # 目录树不能成环：y 仍在原处
        self.assertTrue(self.service.open_reader("r", result["rev"])["ok"])
        self.assertTrue(self.service.read_path("r", "/y")["ok"])
        # 安装会形成环的一侧被拒绝；选择当前一侧可以落地
        cid = result["conflicts"][0]["cid"]
        head = self.service.head
        bad = self.service.resolve(cid, ["mx", "my"], "my", head)
        self.assertFalse(bad["ok"])
        good = self.service.resolve(cid, ["mx", "my"], "mx", head)
        self.assertTrue(good["ok"], good)


class ConflictTests(ServiceTestCase):
    def test_content_conflict_keeps_both_versions_and_resolves(self):
        boot(self.service)
        self.stage_commit("a", "wa", 1, [{"op": "write", "id": "f", "blob": "A"}], [("A", "alpha")])
        result = self.stage_commit("b", "wb", 1, [{"op": "write", "id": "f", "blob": "B"}], [("B", "beta")])
        self.assertEqual(len(result["conflicts"]), 1)
        cid = result["conflicts"][0]["cid"]
        conflict = self.service.conflicts[cid]
        self.assertEqual(conflict["kind"], "content")
        # 双方版本都保存
        texts = {
            side["batch"]: self.service.blobs.get(side["subtree"]["f"]["content"]).decode()
            for side in conflict["sides"]
        }
        self.assertEqual(texts, {"wa": "alpha", "wb": "beta"})
        # 解决必须点名具体双方版本
        wrong = self.service.resolve(cid, ["wa"], "wa", self.service.head)
        self.assertFalse(wrong["ok"])
        done = self.service.resolve(cid, ["wa", "wb"], "wb", self.service.head)
        self.assertTrue(done["ok"], done)
        self.assertTrue(self.service.open_reader("r", done["rev"])["ok"])
        self.assertEqual(self.service.read_path("r", "/lab/run")["text"], "beta")
        # 重送同一解决是幂等的
        again = self.service.resolve(cid, ["wa", "wb"], "wb", self.service.head)
        self.assertTrue(again.get("repeat"))

    def test_stale_resolve_cannot_override_later_modification(self):
        boot(self.service)
        self.stage_commit("a", "wa", 1, [{"op": "write", "id": "f", "blob": "A"}], [("A", "alpha")])
        result = self.stage_commit("b", "wb", 1, [{"op": "write", "id": "f", "blob": "B"}], [("B", "beta")])
        cid = result["conflicts"][0]["cid"]
        expected = self.service.head
        # 另一个终端后来再次修改同一文件：并入冲突成为第三候选
        late = self.stage_commit("c", "wc", self.service.head, [{"op": "write", "id": "f", "blob": "C"}], [("C", "gamma")])
        self.assertEqual(late["joined"], [cid])
        # 别处的一个提交推进了目录修订
        self.stage_commit("d", "elsewhere", self.service.head, [{"op": "move", "id": "u", "parent": "/", "name": "kept"}])
        # 借旧选择（过期 expected 或过期候选名单）都不能覆盖后来的修改
        stale_expected = self.service.resolve(cid, ["wa", "wb", "wc"], "wa", expected)
        self.assertFalse(stale_expected["ok"])
        stale_candidates = self.service.resolve(cid, ["wa", "wb"], "wa", self.service.head)
        self.assertFalse(stale_candidates["ok"])
        done = self.service.resolve(cid, ["wa", "wb", "wc"], "wc", self.service.head)
        self.assertTrue(done["ok"], done)
        self.assertTrue(self.service.open_reader("r", done["rev"])["ok"])
        self.assertEqual(self.service.read_path("r", "/lab/run")["text"], "gamma")

    def test_delete_directory_vs_create_inside(self):
        boot(self.service)
        self.stage_commit("a", "drop", 1, [{"op": "delete", "id": "d"}])
        result = self.stage_commit(
            "b", "new", 1,
            [
                {"op": "create", "id": "g", "parent": "d", "name": "run2", "kind": "f", "blob": "B"},
                {"op": "move", "id": "u", "parent": "/", "name": "kept"},
            ],
            [("B", "run2")],
        )
        # 冲突不吞掉不相关目录的已确认改动
        self.assertEqual(len(result["conflicts"]), 1)
        self.assertTrue(self.service.open_reader("r", result["rev"])["ok"])
        self.assertTrue(self.service.read_path("r", "/kept")["ok"])
        self.assertFalse(self.service.read_path("r", "/lab")["ok"])
        cid = result["conflicts"][0]["cid"]
        conflict = self.service.conflicts[cid]
        self.assertEqual(conflict["scope"], "d")
        self.assertEqual({s["batch"] for s in conflict["sides"]}, {"drop", "new"})
        # 选择新增一侧：目录与其中的新文件一起回来
        done = self.service.resolve(cid, ["drop", "new"], "new", self.service.head)
        self.assertTrue(done["ok"], done)
        self.assertTrue(self.service.open_reader("r2", done["rev"])["ok"])
        self.assertEqual(self.service.read_path("r2", "/lab/run2")["text"], "run2")
        self.assertEqual(self.service.read_path("r2", "/lab/run")["text"], "v1")

    def test_delete_side_wins_and_created_file_is_discarded(self):
        boot(self.service)
        self.stage_commit("a", "drop", 1, [{"op": "delete", "id": "d"}])
        result = self.stage_commit(
            "b", "new", 1,
            [{"op": "create", "id": "g", "parent": "d", "name": "run2", "kind": "f", "blob": "B"}],
            [("B", "run2")],
        )
        cid = result["conflicts"][0]["cid"]
        done = self.service.resolve(cid, ["drop", "new"], "drop", self.service.head)
        self.assertTrue(done["ok"], done)
        self.assertTrue(self.service.open_reader("r", done["rev"])["ok"])
        self.assertFalse(self.service.read_path("r", "/lab")["ok"])

    def test_delete_is_not_silently_revived_by_late_rename(self):
        boot(self.service)
        self.stage_commit("a", "drop", 1, [{"op": "delete", "id": "d"}])
        result = self.stage_commit("b", "ren", 1, [{"op": "move", "id": "d", "parent": "/", "name": "archive"}])
        self.assertEqual([c["kind"] for c in result["conflicts"]], ["delete"])
        # 当前修订里目录仍是删除状态：来晚的重命名没有静默复活它
        self.assertTrue(self.service.open_reader("r", result["rev"])["ok"])
        self.assertFalse(self.service.read_path("r", "/archive")["ok"])
        self.assertFalse(self.service.read_path("r", "/lab")["ok"])
        # 只有显式选择重命名一侧才能恢复
        cid = result["conflicts"][0]["cid"]
        done = self.service.resolve(cid, ["drop", "ren"], "ren", self.service.head)
        self.assertTrue(done["ok"], done)
        self.assertTrue(self.service.open_reader("r2", done["rev"])["ok"])
        self.assertEqual(self.service.read_path("r2", "/archive/run")["text"], "v1")

    def test_path_collision_between_concurrent_creates(self):
        boot(self.service)
        self.stage_commit("a", "ca", 1, [{"op": "create", "id": "x", "parent": "u", "name": "same", "kind": "f", "blob": "A"}], [("A", "from-a")])
        result = self.stage_commit("b", "cb", 1, [{"op": "create", "id": "y", "parent": "u", "name": "same", "kind": "f", "blob": "B"}], [("B", "from-b")])
        self.assertEqual([c["kind"] for c in result["conflicts"]], ["path-collision"])
        cid = result["conflicts"][0]["cid"]
        done = self.service.resolve(cid, ["ca", "cb"], "cb", self.service.head)
        self.assertTrue(done["ok"], done)
        self.assertTrue(self.service.open_reader("r", done["rev"])["ok"])
        read = self.service.read_path("r", "/unrelated/same")
        self.assertEqual((read["id"], read["text"]), ("y", "from-b"))
        # 落选一方失去路径但身份仍在案
        report = self.service.inspect(node="x")
        self.assertIsNone(report["current"])
        self.assertEqual(report["history"][0]["path"], "/unrelated/same")


class UploadAndCommitTests(ServiceTestCase):
    def test_commit_waits_for_bytes_and_never_exposes_incomplete_tree(self):
        boot(self.service)
        self.service.stage("b", "fix", 1, [{"op": "write", "id": "f", "blob": "B"}])
        early = self.service.commit("fix")
        self.assertFalse(early["ok"])
        self.assertEqual(early["missing"], ["B"])
        # 路径变化先到也不能把文件树暴露成不完整版本
        self.assertEqual(self.service.head, 1)
        self.service.add_blob("fix", "B", "corrected".encode("utf-8"))
        done = self.service.commit("fix")
        self.assertTrue(done["ok"])
        self.assertEqual(done["rev"], 2)
        # 管理员能查哪个分支仍有未兑现字节
        self.service.stage("c", "pending", 2, [{"op": "write", "id": "f", "blob": "C"}])
        report = self.service.inspect(batch="pending")
        self.assertEqual(report["missing"], ["C"])
        overview = self.service.inspect()
        self.assertEqual(overview["batches"]["pending"]["missing"], ["C"])

    def test_resend_of_commit_and_stage_is_idempotent(self):
        boot(self.service)
        ops = [{"op": "move", "id": "d", "parent": "/", "name": "archive"}]
        self.service.stage("a", "move", 1, ops)
        first = self.service.commit("move")
        # 终端未收到提交响应后重送：同一改名不能重复执行
        second = self.service.commit("move")
        self.assertTrue(second["repeat"])
        self.assertEqual(second["rev"], first["rev"])
        self.assertEqual(self.service.head, first["rev"])
        staged_again = self.service.stage("a", "move", 1, ops)
        self.assertTrue(staged_again["repeat"])
        # 同一批次号登记不同内容要被拒绝
        changed = self.service.stage("a", "move", 1, ops + [{"op": "delete", "id": "u"}])
        self.assertFalse(changed["ok"])

    def test_restart_recovers_staged_batch_blobs_and_readers(self):
        boot(self.service)
        self.service.open_reader("old", 1)
        self.service.stage("b", "fix", 1, [{"op": "write", "id": "f", "blob": "B"}])
        self.service.add_blob("fix", "B", "corrected".encode("utf-8"))
        revived = DirectoryService(self.directory)
        self.assertEqual(revived.head, 1)
        result = revived.commit("fix")
        self.assertTrue(result["ok"], result)
        self.assertEqual(revived.read_path("old", "/lab/run")["text"], "v1")

    def test_pull_reports_full_tree_and_incremental_changes(self):
        boot(self.service)
        full = self.service.pull()
        self.assertEqual(full["rev"], 1)
        self.assertEqual(full["paths"]["f"], "/lab/run")
        self.assertIn("v1", full["blobs"].values())
        self.stage_commit("a", "fix", 1, [{"op": "write", "id": "f", "blob": "B"}], [("B", "v2")])
        inc = self.service.pull(since=1)
        self.assertEqual(set(inc["changes"]), {"f"})
        self.assertIn("v2", inc["blobs"].values())


class GarbageCollectionTests(ServiceTestCase):
    def test_gc_collects_only_unreferenced_blocks(self):
        boot(self.service)
        digest_v1 = self.service.revs[1]["nodes"]["f"]["content"]
        # 冲突两侧的字节在待解决期间都要保留
        self.stage_commit("a", "wa", 1, [{"op": "write", "id": "f", "blob": "A"}], [("A", "alpha")])
        result = self.stage_commit("b", "wb", 1, [{"op": "write", "id": "f", "blob": "B"}], [("B", "beta")])
        cid = result["conflicts"][0]["cid"]
        digest_beta = self.service.blobs.digest("beta".encode("utf-8"))
        kept = self.service.gc()
        self.assertEqual(kept["collected"], [])
        # 解决后落选一侧的字节才可清理；旧修订与胜出一侧仍然保留
        self.service.resolve(cid, ["wa", "wb"], "wa", self.service.head)
        swept = self.service.gc()
        self.assertEqual(swept["collected"], [digest_beta])
        self.assertTrue(self.service.blobs.has(digest_v1))
        self.assertTrue(self.service.blobs.has(self.service.blobs.digest("alpha".encode("utf-8"))))

    def test_gc_keeps_bytes_needed_by_pending_uploads(self):
        boot(self.service)
        self.service.stage("c", "pending", 1, [{"op": "write", "id": "f", "blob": "C"}])
        self.service.add_blob("pending", "C", "gamma".encode("utf-8"))
        self.assertEqual(self.service.gc()["collected"], [])
        # 已提交批次里无人引用的孤儿字节可以清理
        self.service.stage("d", "orphan", 1, [{"op": "move", "id": "u", "parent": "/", "name": "kept"}])
        self.service.add_blob("orphan", "X", "junk".encode("utf-8"))
        self.service.commit("orphan")
        swept = self.service.gc()
        self.assertEqual(swept["collected"], [self.service.blobs.digest("junk".encode("utf-8"))])


class AdminTests(ServiceTestCase):
    def test_inspect_reports_reader_snapshot_content(self):
        boot(self.service)
        self.service.open_reader("old", 1)
        self.stage_commit("a", "fix", 1, [{"op": "write", "id": "f", "blob": "B"}], [("B", "v2")])
        report = self.service.inspect(reader="old", path="/lab/run")
        self.assertEqual(report["rev"], 1)
        self.assertEqual(report["read"]["text"], "v1")
        node = self.service.inspect(node="f")
        self.assertEqual(node["snapshots"], [
            {"reader": "old", "rev": 1, "path": "/lab/run",
             "content": node["history"][0]["content"], "text": "v1"}
        ])


class ScenarioReplayTests(unittest.TestCase):
    """把随附示例原样回放，锁定演练契约。"""

    def setUp(self):
        self.directory = tempfile.mkdtemp(prefix="scenario-replay-")
        self.addCleanup(shutil.rmtree, self.directory, True)

    def replay(self, name):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            service = play(EXAMPLES / name, self.directory, None)
        return service, output.getvalue()

    def test_rename_and_edit_example(self):
        service, transcript = self.replay("rename-and-edit.jsonl")
        # 字节到齐前提交暂缓，重启后续传完成，旧读者仍读 v1
        self.assertIn("暂缓：缺少字节", transcript)
        self.assertIn("读者 old 在 rev 1 读到 /lab/run → 文件 f，内容 'v1'", transcript)
        self.assertEqual(service.head, 3)
        self.assertTrue(service.open_reader("check", 3)["ok"])
        self.assertEqual(service.read_path("check", "/archive/run")["text"], "corrected")

    def test_delete_and_create_example(self):
        service, transcript = self.replay("delete-and-create.jsonl")
        self.assertIn("新冲突 c1（delete，范围 d）", transcript)
        # 不相关目录的改动照常落地
        self.assertTrue(service.open_reader("check", 3)["ok"])
        self.assertTrue(service.read_path("check", "/kept")["ok"])
        self.assertFalse(service.read_path("check", "/lab")["ok"])
        # 冲突双方版本都在，候选是 drop 与 new
        conflict = service.conflicts["c1"]
        self.assertEqual({s["batch"] for s in conflict["sides"]}, {"drop", "new"})


if __name__ == "__main__":
    unittest.main()
