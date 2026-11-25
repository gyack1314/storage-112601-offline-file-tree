# 断连工作目录的文件身份与差异合并

演练采用合成 UTF-8 内容，路径用正斜杠。离线 edit 引用明确的 id；批次 stage/commit 可以重试。

## 输入消息

每个 JSONL 文件是一段独立输入记录。每行包括整数秒点 at、消息名称 kind 与对象 record。at 代表中间件收到消息的时刻，同一秒内按行序提交。业务时间和对应回执引用写在 record 中。

- **root**　建立初始目录 rev 与 nodes，节点持久 id，parent 为父节点身份，目录 kind=d，文件 kind=f。
- **edit**　client 提出 batch、base 与 ops。操作有 move、write、delete、create，均按 id。写入引用 blob；create 提供 id、parent 和 name。
- **blob**　指定 batch 补交 blob 源字节。
- **commit**　客户端尝试发布 batch。
- **resolve**　明确冲突的 id、候选批次和选择的 winning_batch，expected 是当下目录修订。
- **open**　读者为目录 rev 建 reader。
- **read**　按 reader 和 path 读取。
- **restart**　目录服务重启。
- **inspect**　按批次或节点查询。

## 示例

- rename-and-edit.jsonl
- delete-and-create.jsonl

## 读取演练资料

运行环境为 Python 3.11，只使用标准库。在根目录运行以下命令可核对文件格式及读取接口。

    python3 scenario_reader.py examples
    python3 -m unittest discover -v

资料读取器会按文件统计各类参与方消息的数量。演练记录只描述各参与方交给系统的输入。
