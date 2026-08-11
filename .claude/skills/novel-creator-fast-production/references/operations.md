# 快速生产操作参考

## 1. 主 Claude 先规划十章

十章骨架至少明确：每章功能、视角、即时欲望、主要阻力、关键选择、开场接口、结束接口、允许变化和禁止提前兑现。接口必须足够清楚，让并行写手在看不到相邻成稿时仍能到达同一结构目标。

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
声音短样本
篇幅范围
只读资料清单
```

## 3. 检查原料稿

```bash
novelctl.py production-status BOOK
```

缺稿、硬链接、路径错误或低于篇幅底线时，不进入整合。写手只修自己的 raw 文件。

## 4. 主 Claude 顺序整合

按章号读取 raw 文件，将可用正文整理到：

```text
.novel/staging/chapter-NNNN.md
```

整合后一章时，必须以此前已整合章节的实际结尾和 scene bridge 为准。删除任务卡痕迹、接口说明、重复设定和不同写手之间的语气断层。

## 5. 读者面板

五章 canonical staging 完成并通过篇幅检查后运行 `prepare-review`。把同一版五章正文内联给配置数量的盲读者。默认三名读者并行，主 Claude 等待全部返回。

主 Claude 汇总为：

```json
{
  "status": "completed",
  "required_count": 3,
  "completed_readers": [
    "novel-fast-reader-flow",
    "novel-fast-reader-character",
    "novel-fast-reader-hook"
  ],
  "verdict": "acceptable",
  "ending_pull": "strong",
  "revision_applied": true,
  "issue_tags": [],
  "highest_value_revision": ""
}
```

最多保留三个跨读者合并后的 issue tag。

## 6. 主 Claude 连续性结论

主 Claude 自行检查五章内部及与上一批的时间、地点、动作、POV、人物知识、物品、任务、伏笔和结尾接口，并写入：

```json
{
  "status": "completed",
  "checked_by": "main-claude",
  "blocking_count": 0,
  "warning_count": 0
}
```

这是一份流程凭证，不是自动证明。主 Claude 仍需真正阅读全文。

## 7. 终稿与提交

修订后运行 `finalize-review`，再顺序执行每章 `prepare-delta` 与 `commit`。批次中任意一章修改后，重新 finalize。

## 8. 生产失败恢复

- 单个写手失败：只重派该 chapter assignment。
- 原料稿偏离接口：主 Claude 可要求原写手定向修订，或自己整合修复。
- 盲读意见冲突：保留用户意图和类型承诺，由主 Claude 裁决。
- canonical staging 失败：raw 文件不删除，可重新整合。
- commit 失败：保留 staging，只修 delta、状态或审读记录。
