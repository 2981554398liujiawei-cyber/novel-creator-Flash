# Novel Creator Flash

`novel-creator-flash` 是快速批量小说生产 Claude Code Skill。每个写手席位一次顺序完成连续五章原料，多席并行；主Agent根据任务量与实际启用席位动态规划生产范围、派发任务卡、顺序统稿、Five-Chapter Harmonization、连续性、审稿和终稿。

## 核心流程

```text
主Agent读取正史 / Working State / Reader Model / Prose Contract
→ 根据用户总任务规划整体方向，但默认 Wavefront 只领先约 2 个五章块
→ prepare-production 调度当前 wave；10 席是容量，不是默认全部超前
→ Writer 各自在自己的五章块内顺序写 raw，并逐章更新 schema-2 provisional delta
→ production-status：ready 块可立即按正史顺序整合，不等整轮 all_ready
→ 前块定稿后对后续 speculative 块 rebase-production
→ 主Agent Interface Repair + 极窄 Voice Alignment + adopt-interface
→ 五章候选达到篇幅门槛后 Flow Reader + Pure Reader
→ 主Agent裁决高价值反馈并局部修订；Continuity Reviewer 按需看候选终稿
→ final-clean / warning adjudication → finalize-review → 顺序 commit
→ integration-metrics 只给并发建议，再进入下一 wave
```

## 为什么 raw 不能直接提交

Writer-1 ～ Writer-10 是共享同一创作原则的并行执行席位，不是十种作者人格。默认只让少量 block 领先正史；越往后的 speculative raw 越需要以实际 Working State 做 rebase。raw 永远不能直接进入 canon。

同一 Writer 的五章会在自身上下文中顺序完成，因此能保留块内关系微变化、情绪余温、意象和对白回声；跨块事实、人物知识与临时发明则通过 Block Interface 回到主Agent。

新生产只使用 **Writer report schema 2**：

- `chapter_deltas`：每章实际产生、且会影响后续的暂定变化；
- `block_interface.assumed_entry / exit_state`：进入假设与离开状态；
- `must_carry_forward / plan_deviations`：后续必须承接或与计划不一致的关键项；
- `reader_now_believes / reader_now_wonders`：读者侧变化；
- `soft_inventions / hard_inventions / creative_keep`：计划外创作增量；
- `possible_continuity_risks`：确定性风险输入。

旧 schema-1 六字段 report 只用于历史项目读取兼容，**不会再作为新 Writer 任务格式**。

## Interface Repair / Voice Alignment

整合后、冻结前先修事实与入口失配，再做极窄声音对齐：重复开场、情绪重置、人物声音漂移、叙述距离突变、句法断层、重复解释和任务卡接口感。只修失配与接缝，不把五章磨成同一种节奏。


正式 review batch 永久固定为 5。并行 Writer 每席仍写连续 5 章；若一次请求在完整 Writer 块之外余 1—4 章，这部分由主Agent顺序写并留在 staging，后续请求自然补足该五章单元。**只有作品真实结束**时，最后 1—4 章才记录为 `final_tail`。

## 盲读池

默认每个五章正式候选块使用 1 名综合盲读者；可配置为 2—3 名。多个五章块可以并行启动多个 Reader 实例，因此读者吞吐会随生产规模扩展：

- 1 名：`novel-fast-reader-flow`，作为综合盲读主席，覆盖 Flow + Prose + Character + Hook 基础维度；
- 2 名：综合盲读主席 + `novel-fast-reader-hook`；
- 3 名：再增加 `novel-fast-reader-character`。

负面 finding 统一包含 `chapter / location / evidence / issue / reader_effect / minimal_action`，便于主Agent聚合。


## 按单元决定的连续性复核

每完成一个正式五章审读单元，主Agent都显式判断是否值得调用 `novel-fast-continuity-reviewer`：复杂跨章承接、跨 Writer 接缝、POV/时间跳跃、人物知识边界复杂、大幅修订、Reader 暗示事实问题或主Agent自己拿不准时应调用；真正简单且主Agent已经核清时可以显式 `skip` 并记录理由。Writer report 只要含 `possible_continuity_risks`，脚本就强制 `invoke`。Reviewer 同时检查硬连续性与低级成稿错误，但不参与文笔编辑。

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

全局安装仍使用 `--scope global` / `-Scope Global`。Skill 安装到 `.claude/skills/novel-creator-flash/`，并安装 10 个写手席位、3 个专项盲读者、1 个纯盲读者和 1 名按单元需要调用的 Continuity Reviewer。默认不会强制占满全部席位。

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


### 本版审读闭环

每个正式审读单元新增一个没有编辑职责的纯盲读者；所有章节必须在审读前达到项目 hard minimum；所有创作修改结束后，`finalize-review` 会对最终正文执行 Narrative Boundary / 占位符 / 重复段 final clean 检查。Continuity Reviewer 同时检查硬连续性与低级成稿错误。


### 轻量调度说明

五章是审读 cadence，不是用户每次必须写五章。Pure Reader 每个正式创作单元固定一次；其它 Continuity/专项 Reader 不因“更保险”自动叠加。篇幅不足只允许一次结构性恢复。Flash 可以在某个五章 raw 块 ready 后立即开始该块整合，不等待整个 production wave。

## V10 当前架构

- Working State：未提交章节用 sparse provisional delta 承接实际 staging 正文，只记录会影响后续创作的变化；正文 hash 变化会让旧 provisional 失效。
- Reader Model：维护少量 promise / question / belief / suspicion 生命周期；不把每个细节登记成账本。
- 重大节点可做极短 Creative Divergence；Reader 意见由主Agent `accept / protect / defer / reject`，不自动照单全改。
- Pure Reader 每个正式创作单元固定一次；Continuity Reviewer 继续按需，并绑定它实际检查过的候选稿 hash。
- final-clean 只硬拦确定性残渣；语义 warning 必须修掉或在当前 hash 下裁决。合法同类世界内用法可成组裁决，但不会形成永久白名单。
- 重写按 prose / semantic / structural 分级复审；五章永远只作为 review / transaction 边界，不作为强制叙事小弧。
- Flash 使用有限 Wavefront、schema-2 Block Interface、rebase / voice alignment 和 raw→canonical integration metrics；`production / main-agent / imported` 三种来源明确分离。
- 新生产不再使用 legacy schema-1 六字段 Writer report；旧格式只保留读取兼容。
