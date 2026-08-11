# Novel Creator Flash 快速生产操作参考

## 1. 主Agent先规划十章

十章骨架至少明确：每章功能、视角、即时欲望、主要阻力、关键选择、开场接口、结束接口、允许变化和禁止提前兑现。接口必须足够清楚，让并行写手在看不到相邻成稿时仍能到达同一结构目标。

同时从 `canon/prose-contract.md` 选择本批真正相关的 3—6 条文气约束和最多两个短样本。不要把整份契约塞给五名写手。

## 2. 准备写手池

```bash
novelctl.py prepare-production BOOK
```

按返回的 assignment 调用对应写手。不要自行交换输出路径。批次大于写手池时按 `wave` 分波执行，同一写手不能同时处理两个文件。

每张写手任务卡必须包含：

```text
唯一输出路径
章号与标题方向
用户阅读意图
十章骨架摘要
本章开场锚点
本章结束接口
视角人物欲望、阻力、筹码和可能误判
允许变化与禁止提前兑现
人物声音短样本
少量 Prose Contract 约束与短样本
篇幅范围
只读资料清单
```

写手返回正文之外，还要报告：`newly_invented_details`、`character_micro_changes`、`new_promises`、`motifs_or_objects`、`strong_lines_or_moments`、`possible_continuity_risks`。这些只是索引，主Agent裁决哪些进入后续正文。

## 3. 检查原料稿

```bash
novelctl.py production-status BOOK
```

缺稿、硬链接、路径错误或低于篇幅底线时，不进入整合。写手只修自己的 raw 文件。

## 4. 主Agent顺序整合

按章号读取 raw 文件，将可用正文整理到：

```text
.novel/staging/chapter-NNNN.md
```

整合后一章时，必须以前面已整合章节的实际结尾为准。主Agent还应先阅读写手的计划外创作增量：人物微变化、突然有生命的配角、意象、物件、承诺或强句，只有真正有价值且不破坏骨架的内容才继承。

## 5. Five-Chapter Harmonization

五章 canonical staging 完成后，冻结前检查整批接缝：

- 每章是否都重新开机、重新交代人物或地点；
- 前章情绪余温、关系微变化和新信息是否在后章被承认；
- 同一人物的判断方式、语速和沉默方式是否漂移；
- POV / 叙述距离是否突然改变；
- 某章句法是否明显来自另一种 writer 节奏；
- 是否重复解释前章已经展示的规则；
- 是否出现“根据任务卡重新开场”的接口痕迹。

只修接缝，不把五章磨成同一种句长、同一种节奏或同一种情绪强度。

## 6. 读者面板

五章通过篇幅检查后运行 `prepare-review`，把同一版五章正文内联给配置数量的盲读者。默认三名读者并行：

- Flow + Prose；
- Character；
- Hook。

所有负面 finding 统一使用：

```yaml
- chapter: N
  location: ""
  evidence: ""
  issue: ""
  reader_effect: ""
  minimal_action: ""
```

Flow + Prose 额外指出最有生命与最平/最通用的段落。主Agent等待全部返回后，合并最多三个 `issue_tags` 和一个最高价值修改。

## 7. 主Agent连续性结论

主Agent自行检查五章内部及与上一批的时间、地点、动作、POV、人物知识、物品、任务、伏笔、情绪承接和结尾接口，并写入：

```json
{
  "status": "completed",
  "checked_by": "main-agent",
  "blocking_count": 0,
  "warning_count": 0
}
```

这是流程凭证，不是自动证明。主Agent仍需真正阅读全文。

## 8. 局部修订与 Prose Craft Pass

先处理 reader panel 和 continuity 的明确问题，再执行一次克制的 Prose Craft Pass，整批最多 3—5 个高价值段落。检查具体性、叙述距离、句法压力、重复解释、机械整齐、潜台词和有生命的细节。

不得全文 humanize；不得用禁词表批量替换；不得为了“人味”故意制造语病；不得把不同 writer 的合理差异全部磨掉。

## 9. 终稿与提交

修订后运行 `finalize-review`，再顺序执行每章 `prepare-delta` 与 `commit`。批次中任意一章修改后，重新 finalize。

## 10. 生产失败恢复

- 单个写手失败：只重派该 chapter assignment。
- 原料稿偏离接口：主Agent可要求原写手定向修订，或自己整合修复。
- 盲读意见冲突：保留用户意图、Prose Contract 和类型承诺，由主Agent裁决。
- canonical staging 失败：raw 文件不删除，可重新整合。
- commit 失败：保留 staging，只修 delta、状态或审读记录。
