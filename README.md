# Novel Creator Flash

`novel-creator-flash` 是快速批量小说生产 Claude Code Skill。多个写手并行产出五章原料稿，三个盲读者并行提供读者反馈；主Agent负责理解用户意图、十章骨架、任务卡、顺序整合、Five-Chapter Harmonization、连续性、审稿和终稿。

## 核心流程

```text
主Agent读取设定与 Prose Contract
→ 规划未来十章
→ prepare-production
→ 多写手并行产出 raw
→ 主Agent读取计划外创作增量并顺序整合五章
→ Five-Chapter Harmonization
→ 篇幅检查
→ 三名盲读者并行
→ 主Agent连续性检查与局部修订
→ Prose Craft Pass（最多 3—5 个高价值段落）
→ finalize-review
→ 顺序提交五章
→ audit + export
```

## 为什么 raw 不能直接提交

并行写手看不到相邻章节的实际成稿，因此 raw 只是高质量原料。主Agent必须按照章号顺序整合，并继承前章实际产生的关系微变化、情绪余温、意象、物件和对白回声。

每名写手除正文外会报告：

- `newly_invented_details`
- `character_micro_changes`
- `new_promises`
- `motifs_or_objects`
- `strong_lines_or_moments`
- `possible_continuity_risks`

这些报告帮助主Agent发现计划之外但值得保留的创作增量。

## Five-Chapter Harmonization

整合后、冻结前专门检查跨写手接缝：重复开场、情绪重置、人物声音漂移、叙述距离突变、句法断层、重复解释和任务卡接口感。只修接缝，不把五章磨成同一种节奏。

## 盲读池

默认三名：

- `novel-fast-reader-flow`：Flow + Prose，兼看机械感和跨 writer 拼接感；
- `novel-fast-reader-character`：人物声音、关系、情绪与沉默方式；
- `novel-fast-reader-hook`：类型承诺、回报、规则和追读。

负面 finding 统一包含 `chapter / location / evidence / issue / reader_effect / minimal_action`，便于主Agent聚合。

## Prose Contract

新项目创建 `canon/prose-contract.md`。写手只接收本章相关的少量文气约束和短样本，不读取整份风格清单。主Agent不执行全文 humanize，不使用禁词表批量清洗。

## 安装

```bash
bash install.sh /path/to/project
```

或：

```powershell
./install.ps1 -ProjectPath C:\path\to\project
```

Skill 安装到 `.claude/skills/novel-creator-flash/`，并安装五名写手和三名盲读者。

## 测试

```bash
python tests/run_fast.py
python tests/run_full.py
```

工程测试验证路径隔离、批次、审读凭证、状态和事务安全；真实并发吞吐与文学质量仍应在实际 Claude Code 环境中做五章盲测。
