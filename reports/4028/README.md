# 4028：同批12轮的尾部核对与筛选收束

[分析与后续边界](../../docs/TARGET_V2_4028_REVIEW.md) · [HTML](report.html) · [PDF](report.pdf)

本轮是4027原12轮的离线补查，不新增性能样本。`evidence.json`保存13行原JSON及源行号、2877项核对、每轮唯一>20ms等待、提交返回后延迟下界及完整均值的算术分解。`extremes.csv`、`mean_terms.csv`便于独立复算。

4028日志SHA256为`d4339b77b2eec3d7ff5acf43ee3bbdedbd08e4c38b8ad179887837321300bc6f`；输入4027证据SHA256为`94d41665929254eb2a7969f1428e8d8bc7f9ffd767ab3640adeb7257a0ae62af`。原始CSV仍在测试机，未在本地读取；回传hash与4027一致，只证明报告间身份一致性。

```bash
python3 reports/4028/build_evidence.py --source-log /path/to/4028.log
python3 reports/4028/render_figures.py
python3 reports/final_review/build_report.py --source docs/TARGET_V2_4028_REVIEW.md \
  --output-dir reports/4028 --basename report --figure-layout inline
```

图展示每轮极端等待的位置和同池callback墙钟覆盖；未覆盖不等于CPU空闲，覆盖也不等于on-CPU。均值分解保留192作共同分母，没有删样本、改变warmup或将其余191条当新验收集。提交返回后下界依赖本次同步直接父子投递，不能泛化到外部异步恢复。

9项补查工具测试在目标机通过。当前无新运行/采集/重复打印命令，默认保护保持关闭；根因及生产SLO仍有边界，详见分析第5节。

本地证据复算2877项通过，阶段总报告校验31390项通过；本页报告的8个本地链接、内嵌图片和PDF均已核对。此次修复PDF中文字体漏显数学负号的问题，重新生成4027、4028和总报告，并从PDF提取文字核对负差值及减法公式；HTML/原始数值不变。以上是分析及出版校验，不是新增C++功能测试或性能实验。
