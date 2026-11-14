# 回顾证据的来源与保留范围

这里归档的是用户在当前会话提供的终端日志和拓扑附件。**它们是原始终端记录，不是原始逐任务CSV。** 报告中的逐轮均值、p99和条件分桶数值来自终端打印出的分析摘要；提取脚本不会还原未提供的任务样本。

|归档文件|保留范围|用途|
|---|---|---|
|[0001.log](0001.log)|完整附件，逐字节相同|环境、三亲和smoke、平滑校准|
|[0002.log](0002.log)|完整附件，逐字节相同|smooth/spread/local集中度对照|
|[0003.log](0003.log)|完整附件，逐字节相同|8/16/64/256线程扫描|
|[0004.log](0004.log)|完整附件，逐字节相同|256线程三亲和、指标读写成本|
|[0005.log](0005.log)|原附件第1～170行，逐字节前缀相同|阶段3条件分桶与queue=0检查；截断在首次DAG执行命令之前|
|[topology.json](topology.json)|完整附件，逐字节相同|区分整机拓扑与实际实验CPU子集|

[manifest.json](manifest.json)记录原附件路径、原附件SHA256、归档SHA256、字节数和行数。`0005.log`保留两个不同哈希，原因是本回顾只归档其阶段3前缀；没有改变此前任何一行的内容或行号。DAG结果由另一项工作继续分析。

附件原有CRLF换行也保留；本目录的Git属性禁止对日志和拓扑做换行归一化，避免克隆后哈希改变。新生成的报告、CSV和图不改变这些证据字节。

结构化表的`source_log`、`source_line`（逐轮表另有`source_end_line`）均指向本目录日志。`data/arm_stage3_evidence.json`另保留本次提取时的日志哈希和完整字段。空字段表示该段终端记录没有打印，不填成0或推算为实测值。

从仓库根目录可核验归档完整性：

```bash
python3 - <<'PY'
import hashlib, json, pathlib
root = pathlib.Path('docs/reviews/stages-1-3/evidence')
manifest = json.loads((root / 'manifest.json').read_text())
for item in manifest['files']:
    data = (root / item['file']).read_bytes()
    assert hashlib.sha256(data).hexdigest() == item['archived_sha256'], item['file']
    assert len(data) == item['archived_bytes'], item['file']
    assert len(data.splitlines()) == item['archived_lines'], item['file']
    print('OK', item['file'])
PY
```

这只能证明本回顾归档与记录的哈希一致，不能代替对测试机原始CSV、时钟、资源隔离或统计代码的独立审计。已有二进制SHA、mask、工作量及完成状态信息保留在日志和逐轮表中；未收到的底层数据在正文中明确标注。
