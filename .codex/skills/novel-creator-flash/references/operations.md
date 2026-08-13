# Novel Creator Flash 操作参考

## 1. 生产轮次与正式批次是两层概念

Flash 的**生产轮次**可以很大：主Agent根据任务量与写手数一次规划多个五章块；每个 Writer 一次顺序写 5 章。正式审读和正史提交仍以当前 `state/current.json.batch` 为顺序单元，默认 5 章。

例如用户还需要 50 章且启用 10 个 Writer：

```text
主Agent先规划 1—50 的骨架
Writer-1  → 1—5
Writer-2  → 6—10
...
Writer-10 → 46—50
```

十个 Writer 可以并行完成 raw；主Agent之后仍按 1—5、6—10……的顺序整合、盲读、连续性检查与提交。

生产轮次不能用来跳过正史顺序。

## 2. 配置

长期项目设置：

```bash
"${CLAUDE_SKILL_DIR}/scripts/novelctl-skill" configure \
  --min-chars 3000 --target-chars 3500 --soft-max-chars 4000 \
  --writer-pool-size 10 --blind-reader-count 1
```

`writer_pool_size`：1—10，表示最多可同时启用多少个五章 Writer 席位。

`blind_reader_count`：1—3，表示**每个五章正式候选块**需要多少种盲读意见：

- 1：综合 Reader（Flow + Prose + Character + Hook 基础覆盖）
- 2：综合 Reader + Hook
- 3：综合 Reader + Hook + Character

当有多个五章块待审时，可以同时启动多个 Reader 实例，因此总体盲读吞吐随生产规模增加。

## 3. 准备并行生产

让系统按配置满载：

```bash
"${CLAUDE_SKILL_DIR}/scripts/novelctl-skill" prepare-production
```

显式启用 10 个写手、生产 50 章：

```bash
"${CLAUDE_SKILL_DIR}/scripts/novelctl-skill" prepare-production --writers 10 --chapters 50
```

如果只需要 20 章：

```bash
"${CLAUDE_SKILL_DIR}/scripts/novelctl-skill" prepare-production --writers 4 --chapters 20
```

生产任务不要求总章数是 5 的倍数。完整五章块交给 Writer；本轮请求余 1—4 章时不额外制造章节，也不启动 Writer，由主Agent顺序创作；若作品尚未结束，先留在 staging 等后续自然补足五章，**不要**写成终局。只有作者明确结束作品/卷时才使用 `final_tail`。整个请求不足 5 章时 Writer 数为 0。

`prepare-production` 返回：

- 本轮规划范围；
- Writer 数量；
- 每席连续五章范围；
- 每章唯一 raw 路径；
- 每席唯一 report JSON 路径；
- 每个五章块对应的盲读任务建议。

主Agent必须**先完成整个生产范围的骨架**再派发 Writer。全局规划要控制方向，不要为 50 章写 50 份机械章节合同；详细度集中在五章块目标、进入/离开接口、每章关键选择和禁止提前兑现。

## 4. Writer 一次写五章

派发每个 Writer 前，主Agent为该五章块的起始章运行一次 `context --role writer`，生成 assignment 中指定的 `state/context/chapter-NNNN-writer-context.md`。这是清洁创作包，不把 batch/review/latest_chapter 等后台状态交给 Writer；一块只生成一次，不逐章重建。

每席任务卡至少包含：

- assignment 给出的清洁 `context` 路径；
- 本轮总方向的**压缩摘要**（只保留与本席有关的全局方向，禁止复制整轮几十章详细计划）；
- 本席五章范围；
- 五章逐章短骨架；
- 块级进入接口与离开接口；
- 人物欲望、主要阻力、关键选择；
- 允许变化与不得提前兑现；
- 2—4 条当前真正相关的 Prose Contract 约束；短正向样本通常 0—1 段；
- 五个 raw 路径；
- 一个 report JSON 路径。

Writer 必须在自己的上下文中顺序完成这 5 章；写后一章时承接自己刚写完的前一章实际结尾。

新生产只使用 **Writer report schema 2 / Block Interface**：每完成一章追加 `chapter_deltas`，五章完成后补齐 `block_interface`。旧 schema 1 的六字段报告只为读取历史项目兼容，**不得作为新 Writer 任务卡或新 report 模板**。真正影响后续的 `possible_continuity_risks / hard_inventions / must_carry_forward / plan_deviations` 必须进入 schema 2 结构，不能只靠主Agent记忆转述。

## 5. 检查生产轮次

```bash
"${CLAUDE_SKILL_DIR}/scripts/novelctl-skill" production-status
```

检查每个 Writer 的五个 raw 文件：

- 文件存在且无硬链接；
- 章号标题正确；
- 达到项目篇幅下限；
- report JSON 存在且结构正确；
- 汇总持久化连续性风险。

不要等待整轮 `all_ready=true`。按正史顺序处理已经 `ready=true` 的五章块；未 ready 的后续块只阻塞自己。

## 6. 主Agent顺序整合

即使 raw 是 50 章并行产生，主Agent也必须按五章块从前向后整合 canonical staging。

块 6—10 的 raw 只是一组候选材料。真正整合它时，要以已经确定的 1—5 实际结尾、状态和计划外创作增量为准。必要时修改 6—10 的开场、关系余温、规则解释和细节继承。

原料稿永远不能直接 commit。

## 7. 盲读

每个正式五章候选块冻结后，`prepare-review` 生成 `.novel/blind-packets/...md`，其中只放目标读者说明和冻结正文；Reader 只允许 Read 盲读包目录，任务消息必须指定唯一 packet 路径。终局 `final_tail` 可包含 1—4 章。Reader 不读取 canon/plot/state。

当前 Claude Code 的自定义 Subagent 会自动加载 `CLAUDE.md` / `CLAUDE.local.md`，因此这些文件不得存放幕后答案、人物秘密或盲读标准。

## 8. 连续性风险必须显式判断

`prepare-review` 要求主Agent显式记录本单元的连续性复核决定：

```bash
"${CLAUDE_SKILL_DIR}/scripts/novelctl-skill" prepare-review --continuity-review skip --continuity-reason "主Agent确认本单元简单且接口明确"
```

或：

```bash
"${CLAUDE_SKILL_DIR}/scripts/novelctl-skill" prepare-review \
  --continuity-review invoke --continuity-reason "跨 POV 且核心道具换手"
```

如果对应 Writer report 的 `possible_continuity_risks` 非空，脚本会拒绝 `skip`，必须 `invoke`。没有 Writer 风险时也不是自动跳过：每个正式五章单元都由主Agent显式判断，复杂或拿不准就调用 `novel-fast-continuity-reviewer`。

## 9. 终稿与提交

```text
专项 Reader + 每单元必跑 Pure Reader
→ 主Agent局部修订
→ Prose Craft Pass（最多 3—5 处）
→ 如本单元决定 invoke：Continuity Reviewer 检查修订后候选稿
→ cheap final-clean + finalize-review
→ prepare-delta
→ commit
→ 下一五章块
```

finalize 后正文再次变化，必须重新 finalize。

## 10. Skill-bound Python launcher

Bash：

```bash
"${CLAUDE_SKILL_DIR}/scripts/novelctl-skill" --help
```

PowerShell：

```powershell
& "${CLAUDE_SKILL_DIR}\scripts\novelctl-skill.ps1" --help
```

launcher 自动寻找 Python 3.10+：Unix 优先 `python3` / `python`，Windows 还支持 `py -3`。

## 11. 安装与旧版迁移

普通项目安装：

```bash
bash install.sh /path/to/project
```

如果检测到旧 `novel-creator-fast-production`，默认只提示、不删除。确认迁移时：

```bash
bash install.sh /path/to/project --migrate
```

旧组件会移动到 `.claude/backups/`，不会静默删除。


## 最终篇幅、纯盲读与成稿洁净

- `prepare-review` 会先检查当前正式单元每章是否达到 `minimum_effective_chars`。Writer 在 raw 阶段负责第一次就写到最低篇幅；若整合后的 canonical staging 仍不足，主Agent修正真实故事容量，不能交给 Reader 灌水。
- 每个正式单元除原有 Reader 外，固定调用 `novel-fast-pure-reader` 一次。只给同一个 blind packet，不给任何编辑检查表；主Agent把自然阅读反馈写入 `pure_reader.response`。
- Continuity Reviewer 由主Agent按每个正式单元的实际需要决定，并用 `--continuity-review invoke|skip` 显式记录；Writer report 有连续性风险时必须 invoke。
- 所有正文修改结束后 `finalize-review` 内部再次运行 final clean scan。生产元数据、占位符、重复粘贴等 blocker 必须先修。


## 轻量执行边界

- 五章是正式审读 cadence，不是每次用户请求的强制写作数量；用户只要求一章就只写一章。
- Pure Reader 每个正式创作单元固定调用一次，这是唯一额外固定盲读；不要再因为“更保险”自动追加其它 Reader。
- Continuity Reviewer 是主Agent每单元显式判断的按需检查，放在 Reader 反馈修订之后；不需要“审前一次 + 审后一次”。
- 篇幅不足只做一次结构性恢复；仍不足就调整章节容量，不循环补字。
- `final-clean` 只对确定性生产残渣、占位符和大段重复做硬门禁；“第九章/卷/Writer”等可能合法的词只作为语义候选交给主Agent/Reviewer复核。 若 `warnings` 非空，主Agent只做一次上下文判断：确属作品生产结构泄漏就修；有明确世界内意义就保留。不要因为 warning 反复改写或再次启动 Reader。


## Working State / Reader Model

每完成一章 staging：

```bash
"${CLAUDE_SKILL_DIR}/scripts/novelctl-skill" working-state --scaffold --chapter N
```

主Agent只填写**会改变后续创作判断**的 provisional 信息：真正变化的 `summary / dominant_change / current_patch / reader_model_updates` 才写；没有变化的状态沿用即可，不为字段完整度制造 metadata，不把 Working State 当“章节报表”。下一次 `context --role author|writer` 会自动从 provisional deltas 重新编译 Working State，因此不需要为刷新缓存额外跑一次命令；只有想显式查看暂定状态时才单独运行 `working-state`。`prepare-delta` 会优先继承 provisional delta。Working State 不是正史；commit 成功后相应 provisional delta 会被移除并重新编译。

`reader_model_updates` 只记录真正影响读者预期的 promise / question / belief / suspicion 生命周期，不把每个细节都登记。

Reader 返回后，在 review record 的 `feedback_adjudication` 中只记录少量高价值决定：`accept / protect / defer / reject`。没有高价值冲突时 `decisions: []` 合法，但 `status` 要由主Agent置为 `completed`。

五章是 review / transaction 边界，不是叙事节拍。


## Flash Wavefront

默认 `speculative_lookahead_blocks=2`。`prepare-production --chapters 50` 不再一次把 50 章全部派出，而是只调度当前 wave，并返回 `deferred_chapters`；前置块收敛后继续下一 wave。需要极限速度时显式 `--allow-deep-speculation`。 本轮出现 1—4 章 `main_agent_remainder` 时，它只表示请求余数，必须等它前面的 speculative 五章块按正史顺序收敛到其入口后再由主Agent写；不得绕过尚未定稿的前置块提前写余数。

Writer report schema 2 在每章后追加 `chapter_deltas`，五章后给出 `block_interface.assumed_entry / exit_state / must_carry_forward / plan_deviations / reader_now_* / soft_inventions / hard_inventions / creative_keep`。前一块 final 后对后一块运行：

```bash
"${CLAUDE_SKILL_DIR}/scripts/novelctl-skill" rebase-production --start N
```

只修 mismatch，不全面重写。主Agent完成该块 staging 整合后运行 `adopt-interface --start N`，把 Writer 每章 provisional delta 转入 Working State，并按实际整合稿校正。块提交后可运行 `integration-metrics --start N --record`；它只给 lookahead 建议，不自动改配置。


## 写前节奏提示

写下一创作窗口前若已有足够历史，可运行一次 `quality --recent 6` 查看最近章节的重复风险；结果只作为规划输入，不要求清零 warning，也不新建节奏账本。


### 正文来源

`prepare-review` 正式区分 `production / main-agent / imported`。Writer 块使用 production；主Agent直接完成的五章使用 `--source-mode main-agent`（有完整 provisional source 时可自动识别）；只有外部导入稿使用 `--source-mode imported`。不要把主Agent正文伪装成 imported。


### 修订复审

- 纯措辞：`rewrite --review-level prose` → `confirm-rewrite`。
- 事实/知识/承接：`rewrite --review-level semantic` → Continuity Reviewer 看前/本/后章 → `review-rewrite --blocking-count 0 --checked-by <reviewer>` → `confirm-rewrite`。
- 结构：`rewrite --review-level structural` → `impact --chapter N` → 校正受影响状态/下游 → Continuity Reviewer → `review-rewrite ... --structural-state-reconciled` → `confirm-rewrite`。

正式 review 中，Continuity 若最初 skip、但 Reader 后来发现事实疑点，用 `review-continuity --invoke --reason ...` 升级；Reviewer 返回后用 `review-continuity --complete --blocking-count 0 --warning-count N` 绑定当前候选稿 hashes。

`finalize-review` 若报告 semantic warning，会把**完整 warning 集**写入 review record 并停止。真实残渣直接修正文后重跑；世界内合法表达可按单条 `adjudicate-warning --id ...`，也可在同一最终稿 hash 下把当前同类别（必要时限定某章）的 warning 一次性 `--category ... [--chapter N]` 裁决。成组裁决只覆盖本次扫描实际存在的 warning IDs，不是永久白名单；正文一改，旧裁决自动失效。
