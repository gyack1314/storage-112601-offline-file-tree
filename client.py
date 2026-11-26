"""合成客户端：拉取、离线变更提交、冲突决定和按修订读取。

直接运行可看到一段完整演练：

    python3 client.py

演练覆盖：两个终端从同一版本出发离线工作（一个重命名记录夹、一个补文件），
重连后自动合并；同一文件的内容冲突与解决；删除与新增冲突；分批上报续传；
以及管理员视角的身份、未兑现字节与旧快照查询。
"""

import shutil
import tempfile

from versioned_store import DirectoryService


class OfflineClient:
    """一个可断连工作的终端：先拉取，离线登记变更，重连后分批上报。"""

    def __init__(self, service, name):
        self.service = service
        self.name = name
        self.seq = 0
        self.base = None
        self.ops = []
        self.texts = {}

    # -- 拉取 ----------------------------------------------------------

    def pull(self, since=None):
        """拉取目录状态，离线工作从这里开始。"""
        report = self.service.pull(since=since)
        self.base = report["rev"]
        self.ops = []
        self.texts = {}
        return report

    # -- 离线登记（不产生任何服务端调用） ------------------------------

    def _blob(self, text):
        name = f"b{len(self.texts) + 1}"
        self.texts[name] = text
        return name

    def create_file(self, nid, parent, name, text):
        self.ops.append({"op": "create", "id": nid, "parent": parent, "name": name, "kind": "f", "blob": self._blob(text)})

    def create_dir(self, nid, parent, name):
        self.ops.append({"op": "create", "id": nid, "parent": parent, "name": name, "kind": "d"})

    def write(self, nid, text):
        self.ops.append({"op": "write", "id": nid, "blob": self._blob(text)})

    def move(self, nid, parent, name):
        self.ops.append({"op": "move", "id": nid, "parent": parent, "name": name})

    def delete(self, nid):
        self.ops.append({"op": "delete", "id": nid})

    # -- 重连上报 ------------------------------------------------------

    def submit(self, batch=None, upload_first=True):
        """把离线变更作为一批提交。批次号确定，未收到响应时重送是安全的。"""
        self.seq += 1
        batch = batch or f"{self.name}-{self.seq}"
        staged = self.service.stage(self.name, batch, self.base, self.ops)
        if not staged["ok"]:
            return {"ok": False, "batch": batch, "error": staged["error"]}
        if upload_first:
            for name, text in self.texts.items():
                self.service.add_blob(batch, name, text.encode("utf-8"))
        result = self.service.commit(batch)
        if result["ok"]:
            self.ops, self.texts = [], {}
        result["batch"] = batch
        return result

    def upload_pending(self, batch):
        """续传某一批的字节（提交响应提示缺少内容之后）。"""
        for name, text in self.texts.items():
            self.service.add_blob(batch, name, text.encode("utf-8"))

    # -- 冲突决定与按修订读取 ------------------------------------------

    def conflicts(self):
        return self.service.list_conflicts()

    def decide(self, cid, candidates, winning_batch, expected):
        """对冲突做出决定：必须点名双方候选批次与当下修订。"""
        return self.service.resolve(cid, candidates, winning_batch, expected)

    def open(self, reader, rev):
        return self.service.open_reader(reader, rev)

    def read(self, reader, path):
        return self.service.read_path(reader, path)


def demo():
    directory = tempfile.mkdtemp(prefix="versioned-store-demo-")
    service = DirectoryService(directory)
    print(f"数据目录：{directory}\n")

    print("== 1. 初始目录与拉取")
    service.root(1, [
        {"id": "lab", "parent": "/", "name": "lab", "kind": "d"},
        {"id": "run", "parent": "lab", "name": "run", "kind": "f", "text": "v1"},
        {"id": "misc", "parent": "/", "name": "misc", "kind": "d"},
    ])
    a = OfflineClient(service, "a")
    b = OfflineClient(service, "b")
    a.pull()
    b.pull()
    print(f"两个终端各自拉取到修订 {a.base}，随后断开连接\n")

    print("== 2. 离线工作：a 重命名记录夹，b 补一个文件并修改 run")
    a.move("lab", "/", "archive")
    b.create_file("notes", "lab", "notes", "field notes")
    b.write("run", "corrected")
    print("a：lab → archive；b：在 lab 里新增 notes、把 run 改为 corrected\n")

    print("== 3. 重连合并：互不干涉的变化自动合并")
    ra = a.submit()
    print(f"a 提交 {ra['batch']} → 修订 {ra['rev']}")
    rb = b.submit()
    print(f"b 提交 {rb['batch']} → 修订 {rb['rev']}，冲突 {rb['conflicts']}")
    a.open("reader-a", 1)
    print(f"打开于修订 1 的读者仍读 /lab/run：{a.read('reader-a', '/lab/run')['text']!r}")
    b.open("reader-b", rb["rev"])
    print(f"b 在合并后读 /archive/run：{b.read('reader-b', '/archive/run')['text']!r}")
    print(f"b 在合并后读 /archive/notes：{b.read('reader-b', '/archive/notes')['text']!r}")
    print("（a 的重命名与 b 的修改落在同一身份上，b 的旧快照读者 a 不受影响）\n")

    print("== 4. 分批上报：字节未到齐不形成修订，续传后重送安全")
    c = OfflineClient(service, "c")
    c.pull()
    c.write("run", "v3")
    batch = "c-1"
    service.stage("c", batch, c.base, c.ops)
    early = service.commit(batch)
    print(f"路径变化先到，提交被暂缓：{early['error']}，缺少 {early['missing']}（当前修订仍是 {service.head}）")
    c.upload_pending(batch)
    done = service.commit(batch)
    print(f"续传字节后提交成功 → 修订 {done['rev']}")
    again = service.commit(batch)
    print(f"未收到响应重送同一批次：rev {again['rev']}，repeat={again['repeat']}（改名不会重复执行）\n")

    print("== 5. 同一文件内容冲突：双方版本都保存，过期选择被拒绝")
    d1 = OfflineClient(service, "d1")
    d2 = OfflineClient(service, "d2")
    d1.pull()
    d2.pull()
    d1.write("run", "from-d1")
    d2.write("run", "from-d2")
    r1 = d1.submit()
    r2 = d2.submit()
    cid = r2["conflicts"][0]["cid"]
    info = service.list_conflicts()[0]
    print(f"d1 → 修订 {r1['rev']}；d2 与 d1 冲突 {cid}，候选 {info['candidates']}，范围 {info['paths']}")
    stale = d2.decide(cid, ["d1-1"], "d1-1", service.head)
    print(f"只点名一方的决定被拒绝：{stale['error']}")
    decided = d2.decide(cid, info["candidates"], "d2-1", service.head)
    print(f"点名双方版本后决定 d2-1 胜出 → 修订 {decided['rev']}")
    d2.open("final", decided["rev"])
    print(f"按修订读取 /archive/run：{d2.read('final', '/archive/run')['text']!r}\n")

    print("== 6. 删除目录 vs 在里面新增文件；不相关目录不受影响")
    e1 = OfflineClient(service, "e1")
    e2 = OfflineClient(service, "e2")
    e1.pull()
    e2.pull()
    e1.delete("misc")
    e2.create_file("keep", "misc", "keep", "still needed")
    e2.create_file("free", "/", "free", "unrelated")
    s1 = e1.submit()
    s2 = e2.submit()
    cid2 = s2["conflicts"][0]["cid"]
    e2.open("after", s2["rev"])
    print(f"e1 删 misc → 修订 {s1['rev']}；e2 在 misc 里新增 → 冲突 {cid2}")
    print(f"不相关的 /free 照常落地：{e2.read('after', '/free')['text']!r}")
    won = e2.decide(cid2, [s["batch"] for s in service.conflicts[cid2]["sides"]], "e2-1", service.head)
    e2.open("resolved", won["rev"])
    print(f"决定保留新增一侧 → /misc/keep 可读：{e2.read('resolved', '/misc/keep')['text']!r}\n")

    print("== 7. 管理员视角")
    node = service.inspect(node="run")
    print("文件 run 移动前后指向同一身份：")
    for entry in node["history"]:
        print(f"  rev {entry['rev']}: {entry['path']} 内容 {entry['text']!r} 来源 {entry['cause']}")
    service.stage("z", "z-1", service.head, [{"op": "write", "id": "run", "blob": "Z"}])
    batch = service.inspect(batch="z-1")
    print(f"分支 {batch['batch']} 仍有未兑现字节：{batch['missing']}")
    snap = service.inspect(reader="reader-a", path="/lab/run")
    print(f"旧快照 reader-a 停在修订 {snap['rev']}，仍在读：{snap['read']['text']!r}")
    swept = service.gc()
    print(f"垃圾清理：清理 {len(swept['collected'])} 块，保留 {swept['kept']} 块（可读版本、待处理冲突与续传需要的都保留）")

    shutil.rmtree(directory, True)


if __name__ == "__main__":
    demo()
