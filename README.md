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

## 存储实现与合成客户端

- **versioned_store.py**　版本化目录服务。节点创建时获得持久 id，改字节、改名、搬移、删除都按 id 引用并登记批次与基准修订；提交时做三路合并，互不干涉的变化自动合并，冲突双方版本都存入冲突记录并指出范围；解决冲突必须点名全部候选批次且 expected 等于当下修订，过期选择不能覆盖后来的修改；目录可按修订读取，读者持有打开时的快照；字节到齐并验明后才形成可查询的新修订，提交与登记可安全重送；垃圾清理只清没有可读版本、待处理冲突或续传需要的内容块。状态持久化在数据目录（state.json 与 blobs/），重启后恢复。
- **scenario_player.py**　把演练 JSONL 逐条回放给服务并打印结果：`python3 scenario_player.py examples/rename-and-edit.jsonl`。
- **client.py**　合成客户端，提供拉取（pull）、离线变更提交（submit）、冲突决定（decide）和按修订读取（open/read）。直接运行 `python3 client.py` 可看到两个终端离线工作、重连合并、冲突解决与管理员查询的完整演练。
- **test_versioned_store.py**　行为测试，覆盖身份保持、自动合并、各类冲突、续传与幂等重送、重启恢复、快照隔离与垃圾清理。

管理员查询通过 `inspect` 进行：按节点可查移动前后如何指向同一身份、各读者快照仍在读哪份内容；按批次可哪个分支仍有未兑现字节；无参数时给出整体概览。

运行全部测试：

    python3 -m unittest discover -v
