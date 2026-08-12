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

生产任务不要求总章数是 5 的倍数。完整五章块交给 Writer；最后余 1—4 章时不额外制造章节，也不启动 Writer，由主Agent顺序创作，并作为 `final_tail` 收尾。整个任务不足 5 章时 Writer 数为 0。

`prepare-production` 返回：

- 本轮规划范围；
- Writer 数量；
- 每席连续五章范围；
- 每章唯一 raw 路径；
- 每席唯一 report JSON 路径；
- 每个五章块对应的盲读任务建议。

主Agent必须**先完成整个生产范围的骨架**再派发 Writer。全局规划要控制方向，不要为 50 章写 50 份机械章节合同；详细度集中在五章块目标、进入/离开接口、每章关键选择和禁止提前兑现。

## 4. Writer 一次写五章

每席任务卡至少包含：

- 本轮总规划范围；
- 本席五章范围；
- 五章逐章短骨架；
- 块级进入接口与离开接口；
- 人物欲望、主要阻力、关键选择；
- 允许变化与不得提前兑现；
- 少量人物声音样本与 Prose Contract；
- 五个 raw 路径；
- 一个 report JSON 路径。

Writer 必须在自己的上下文中顺序完成这 5 章；写后一章时承接自己刚写完的前一章实际结尾。

Writer report JSON 保存计划外但可能有价值的信息：

- `newly_invented_details`
- `character_micro_changes`
- `new_promises`
- `motifs_or_objects`
- `strong_lines_or_moments`
- `possible_continuity_risks`

报告不是正史，但 `possible_continuity_risks` 是确定性风险输入，不能靠主Agent记忆转述。

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

只有 `all_ready=true` 才进入整合。

## 6. 主Agent顺序整合

即使 raw 是 50 章并行产生，主Agent也必须按五章块从前向后整合 canonical staging。

块 6—10 的 raw 只是一组候选材料。真正整合它时，要以已经确定的 1—5 实际结尾、状态和计划外创作增量为准。必要时修改 6—10 的开场、关系余温、规则解释和细节继承。

原料稿永远不能直接 commit。

## 7. 盲读

每个正式五章候选块冻结后，`prepare-review` 生成 `.novel/blind-packets/...md`，其中只放目标读者说明和冻结正文；Reader 只允许 Read 盲读包目录，任务消息必须指定唯一 packet 路径。终局 `final_tail` 可包含 1—4 章。Reader 不读取 canon/plot/state。

当前 Claude Code 的自定义 Subagent 会自动加载 `CLAUDE.md` / `CLAUDE.local.md`，因此这些文件不得存放幕后答案、人物秘密或盲读标准。

## 8. 连续性风险必须显式判断

`prepare-review` 不提供默认风险值：

```bash
"${CLAUDE_SKILL_DIR}/scripts/novelctl-skill" prepare-review --continuity-risk low
```

或：

```bash
"${CLAUDE_SKILL_DIR}/scripts/novelctl-skill" prepare-review \
  --continuity-risk high --risk-reason "跨 POV 且核心道具换手"
```

如果对应 Writer report 的 `possible_continuity_risks` 非空，脚本会拒绝 `low`，必须 `high`。高风险必须由 `novel-fast-continuity-reviewer` 完成；低风险可由主Agent做窄范围检查。

## 9. 终稿与提交

```text
Reader / Continuity 反馈
→ 主Agent局部修订
→ Prose Craft Pass（最多 3—5 处）
→ finalize-review
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
