"""回放存储演练消息：把 JSONL 输入逐条交给版本化目录服务并打印结果。

用法：

    python3 scenario_player.py examples/rename-and-edit.jsonl
    python3 scenario_player.py examples/*.jsonl --data-dir /tmp/keep
"""

import argparse
import json
import shutil
import tempfile
from pathlib import Path

from scenario_reader import iter_messages
from versioned_store import DirectoryService

DEFAULT_KINDS = {
    "blob", "commit", "edit", "inspect", "open", "read", "resolve", "restart", "root",
}


def print_inspect(report, indent="  "):
    if not report.get("ok"):
        print(f"{indent}查询失败：{report['error']}")
        return
    if "id" in report:
        current = report["current"]
        if current:
            where = f"修订 {current['rev']} 路径 {current['path']}，内容 {current['text']!r}"
        else:
            where = "当前修订中不存在"
        print(f"{indent}节点 {report['id']}（{report['kind']}）：{where}")
        for entry in report["history"]:
            text = f"，内容 {entry['text']!r}" if entry.get("text") is not None else ""
            print(f"{indent}  rev {entry['rev']}: {entry['path'] or '（不存在）'}{text}　来源 {entry['cause']}")
        for snap in report["snapshots"]:
            print(f"{indent}  读者 {snap['reader']} 停在 rev {snap['rev']}：{snap['path']} 内容 {snap['text']!r}")
        if report["pending_conflicts"]:
            print(f"{indent}  待解决冲突：{'、'.join(report['pending_conflicts'])}")
    elif "batch" in report:
        print(
            f"{indent}批次 {report['batch']}（客户端 {report['client']}，基准 {report['base']}）：{report['status']}"
        )
        if report["result"]:
            print(f"{indent}  提交结果：{json.dumps(report['result'], ensure_ascii=False)}")
        if report["missing"]:
            print(f"{indent}  未兑现字节：{'、'.join(report['missing'])}")
        else:
            print(f"{indent}  字节全部就位：{'、'.join(sorted(report['blobs'])) or '（无需字节）'}")
        if report["conflicts"]:
            print(f"{indent}  涉及冲突：{'、'.join(report['conflicts'])}")
    elif "reader" in report:
        print(f"{indent}读者 {report['reader']} 停在修订 {report['rev']}")
        if "read" in report:
            print(f"{indent}  读取结果：{json.dumps(report['read'], ensure_ascii=False)}")
    else:
        print(f"{indent}当前修订 {report['head']}，存储内容块 {report['blobs_stored']} 个")
        for batch, info in report["batches"].items():
            missing = f"，未兑现字节 {'、'.join(info['missing'])}" if info["missing"] else ""
            print(f"{indent}  批次 {batch}（{info['client']}）：{info['status']}{missing}")
        for conflict in report["pending_conflicts"]:
            print(
                f"{indent}  待解决冲突 {conflict['cid']}（{conflict['kind']}，范围 {conflict['scope']}，"
                f"候选 {'、'.join(conflict['candidates'])}）"
            )
        for reader, rev in report["readers"].items():
            print(f"{indent}  读者 {reader} 停在修订 {rev}")


def play(scenario, data_dir, supported=None):
    service = DirectoryService(data_dir)
    for row in iter_messages(scenario, supported or DEFAULT_KINDS):
        at, kind, record = row["at"], row["kind"], row["record"]
        prefix = f"at={at} {kind}"
        if kind == "root":
            result = service.root(record["rev"], record["nodes"])
            if result["ok"]:
                print(f"{prefix}: 建立初始修订 {result['rev']}（{result['nodes']} 个节点）")
            else:
                print(f"{prefix}: 失败：{result['error']}")
        elif kind == "restart":
            service = DirectoryService(data_dir)
            print(f"{prefix}: 服务重启，状态自磁盘恢复（当前修订 {service.head}）")
        elif kind == "open":
            result = service.open_reader(record["reader"], record["rev"])
            if result["ok"]:
                print(f"{prefix}: 读者 {record['reader']} 打开修订 {record['rev']}")
            else:
                print(f"{prefix}: 失败：{result['error']}")
        elif kind == "edit":
            result = service.stage(record["client"], record["batch"], record["base"], record["ops"])
            if result["ok"]:
                again = "（重送）" if result.get("repeat") else ""
                print(
                    f"{prefix}: 批次 {record['batch']} 已登记{again}"
                    f"（客户端 {record['client']}，基准 {record['base']}，{len(record['ops'])} 个操作）"
                )
            else:
                print(f"{prefix}: 批次 {record['batch']} 被拒绝：{result['error']}")
        elif kind == "blob":
            data = record["text"].encode("utf-8")
            result = service.add_blob(record["batch"], record["blob"], data)
            print(f"{prefix}: 批次 {record['batch']} 收到字节 {record['blob']}（{len(data)} 字节）")
        elif kind == "commit":
            result = service.commit(record["batch"])
            if result["ok"]:
                again = "（重送，结果不变）" if result.get("repeat") else ""
                extra = ""
                if result["conflicts"]:
                    extra = "；新冲突 " + "、".join(
                        f"{c['cid']}（{c['kind']}，范围 {c['scope']}）" for c in result["conflicts"]
                    )
                if result.get("joined"):
                    extra += "；并入待解决冲突 " + "、".join(result["joined"])
                print(f"{prefix}: 批次 {record['batch']} 发布，当前修订 {result['rev']}{again}{extra}")
            else:
                missing = f"，缺少 {'、'.join(result['missing'])}" if result.get("missing") else ""
                print(f"{prefix}: 批次 {record['batch']} 暂缓：{result['error']}{missing}")
        elif kind == "resolve":
            result = service.resolve(
                record["id"], record["candidates"], record["winning_batch"], record["expected"]
            )
            if result["ok"]:
                again = "（重送，结果不变）" if result.get("repeat") else ""
                print(f"{prefix}: 冲突 {record['id']} 由 {record['winning_batch']} 胜出，修订 {result['rev']}{again}")
            else:
                print(f"{prefix}: 冲突 {record['id']} 解决失败：{result['error']}")
        elif kind == "read":
            result = service.read_path(record["reader"], record["path"])
            if not result["ok"]:
                print(f"{prefix}: 读者 {record['reader']} 读 {record['path']} 失败：{result['error']}")
            elif result["kind"] == "f":
                print(
                    f"{prefix}: 读者 {record['reader']} 在 rev {result['rev']} 读到 "
                    f"{record['path']} → 文件 {result['id']}，内容 {result['text']!r}"
                )
            else:
                names = "、".join(c["name"] for c in result["children"]) or "（空）"
                print(
                    f"{prefix}: 读者 {record['reader']} 在 rev {result['rev']} 读到 "
                    f"{record['path']} → 目录 {result['id']}，含 {names}"
                )
        elif kind == "inspect":
            report = service.inspect(
                batch=record.get("batch"), node=record.get("id"), reader=record.get("reader")
            )
            print(f"{prefix}:")
            print_inspect(report)
    return service


def main():
    parser = argparse.ArgumentParser(description="回放存储演练消息")
    parser.add_argument("scenario", type=Path, nargs="+", help="JSONL 演练文件")
    parser.add_argument("--data-dir", type=Path, default=None, help="持久化目录（默认每次用临时目录）")
    args = parser.parse_args()
    for scenario in args.scenario:
        registry = scenario.parent / "message_types.json"
        supported = (
            set(json.loads(registry.read_text(encoding="utf-8"))) if registry.exists() else DEFAULT_KINDS
        )
        if args.data_dir is not None:
            data_dir = args.data_dir
            if data_dir.exists():
                shutil.rmtree(data_dir)
        else:
            data_dir = Path(tempfile.mkdtemp(prefix="scenario-"))
        print(f"== {scenario.name}（数据目录 {data_dir}）")
        play(scenario, data_dir, supported)


if __name__ == "__main__":
    main()
