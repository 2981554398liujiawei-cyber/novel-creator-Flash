# Novel Creator Flash

`novel-creator-flash` 是快速批量小说生产 Claude Code Skill。每个写手席位一次顺序完成连续五章原料，多席并行；主Agent根据任务量与实际启用席位动态规划生产范围、派发任务卡、顺序统稿、Five-Chapter Harmonization、连续性、审稿和终稿。

## 核心流程

```text
主Agent读取设定与 Prose Contract
→ 根据任务量启用 K 个写手
→ 一次规划 K×5 章（10 席即 50 章）
→ prepare-production
→ K 个写手各顺序写连续 5 章 raw，并行生产
→ 主Agent按五章块读取计划外创作增量并顺序整合
→ Five-Chapter Harmonization
→ 篇幅检查
→ 配置数量的盲读者并行
→ 低风险主Agent连续性检查 / 高风险轻量 Reviewer
→ 主Agent局部修订
→ Prose Craft Pass（最多 3—5 个高价值段落）
→ finalize-review
→ 顺序提交五章
→ audit + export
```

## 为什么 raw 不能直接提交

Writer-1 ～ Writer-10 是**并行执行席位**：共享同一创作原则，只通过五章任务卡和上下文区分工作，不代表十种不同的作者人格或十套文风。每个实际启用的席位一次负责一个连续五章块。


不同写手块看不到前一个块的实际终稿，因此 raw 仍只是高质量原料；但同一写手的五章会在其自己的上下文中顺序完成，能保留块内的关系微变化、情绪余温、意象、物件和对白回声。主Agent必须按五章块顺序整合，并把已经定稿块产生的新事实传导到后续块。

每个五章写手块除五章正文外会持久化一个 report JSON：

- `newly_invented_details`
- `character_micro_changes`
- `new_promises`
- `motifs_or_objects`
- `strong_lines_or_moments`
- `possible_continuity_risks`

这些报告帮助主Agent发现计划之外但值得保留的创作增量。

## Five-Chapter Harmonization

整合后、冻结前专门检查跨写手接缝：重复开场、情绪重置、人物声音漂移、叙述距离突变、句法断层、重复解释和任务卡接口感。只修接缝，不把五章磨成同一种节奏。


正式 review batch 永久固定为 5。并行 Writer 每席仍写连续 5 章；若本轮任务最后余 1—4 章，则这部分由主Agent顺序写，记录为 `final_tail`，不启动 Writer，也不把 review batch 改小。

## 盲读池

默认每个五章正式候选块使用 1 名综合盲读者；可配置为 2—3 名。多个五章块可以并行启动多个 Reader 实例，因此读者吞吐会随生产规模扩展：

- 1 名：`novel-fast-reader-flow`，作为综合盲读主席，覆盖 Flow + Prose + Character + Hook 基础维度；
- 2 名：综合盲读主席 + `novel-fast-reader-hook`；
- 3 名：再增加 `novel-fast-reader-character`。

负面 finding 统一包含 `chapter / location / evidence / issue / reader_effect / minimal_action`，便于主Agent聚合。


## 条件式轻量连续性复核

普通低风险批次继续由主Agent做窄范围连续性检查。若 Writer 报告连续性风险，或批次包含跨 POV、连续战斗、谜题揭示、能力/装备/伤势大变化、大幅重组 raw、核心设定首次兑现、卷尾等高风险因素，则 `prepare-review --continuity-risk high`，并调用 `novel-fast-continuity-reviewer`。它只检查硬事实与前后章接口，不参与文笔编辑。

## Prose Contract

新项目创建 `canon/prose-contract.md`。写手只接收本章相关的少量文气约束和短样本，不读取整份风格清单。主Agent不执行全文 humanize，不使用禁词表批量清洗。

## 安装

macOS / Linux（推荐 positional path）：

```bash
bash install.sh /path/to/project
```

等价显式写法：

```bash
bash install.sh --scope project --project-path /path/to/project
```

PowerShell 同样支持 positional path：

```powershell
./install.ps1 C:\path\to\project
# 或 ./install.ps1 -ProjectPath C:\path\to\project
```

全局安装仍使用 `--scope global` / `-Scope Global`。Skill 安装到 `.claude/skills/novel-creator-flash/`，并安装 10 个写手席位、3 个盲读者角色和 1 名按风险触发的轻量连续性 Reviewer。默认不会强制占满全部席位。

## 测试

```bash
python3 tests/run_fast.py  # 或 python / py -3
python3 tests/run_full.py  # 或 python / py -3
```

工程测试验证路径隔离、批次、审读凭证、状态和事务安全；真实并发吞吐与文学质量仍应在实际 Claude Code 环境中做多块生产盲测。

## Claude Code 权限烟雾测试

Skill 只精确预授权 Bash 下绑定当前项目的 `scripts/novelctl-skill` launcher，不预授权可显式指定 workspace 的人工 `scripts/novelctl`，也不授权泛化 Bash。PowerShell 使用随包 `novelctl-skill.ps1`，但当前 Claude Code 只明确支持在 Bash allowed-tools 规则中展开 `${CLAUDE_SKILL_DIR}`，因此 Windows 端不做宽泛 PowerShell 预授权。launcher 自动寻找 Python 3.10+。安装并登录 Claude Code 后可运行：

```bash
python tests/smoke_claude_skill_permissions.py --require
```

该测试会在临时项目中安装本 Skill，并用 `dontAsk` 权限模式要求 Claude Code 通过 Skill 实际执行随包 Skill-bound launcher `scripts/novelctl-skill --help`。若本机没有 `claude` CLI，不带 `--require` 时会明确跳过。



## 盲读隔离提醒
自定义 Subagent 会加载 `CLAUDE.md` / `CLAUDE.local.md`，不要把幕后答案、人物秘密、预期转折或盲读标准写入这些文件。
