# Novel Creator Fast Production

`novel-creator-fast-production` 是基于稳定批次版拆出的快速生产分支。它把“创作吞吐量”和“正史安全”分开：五名写手并行产出原料稿，三名盲读者并行提供读者反馈，主 Claude 负责理解用户意图、十章规划、任务卡、整合、连续性、审稿与终稿。

## 默认规模

```text
规划窗口：10 章
生产批次：5 章
写手池：5 名
盲读池：3 名
```

五名写手各写一章独立原料稿。原料稿不能直接提交；主 Claude 必须按章号顺序整合成 canonical staging，并以实际前章结尾修正后章开场。

三名盲读者分别关注：

- `novel-fast-reader-flow`：节奏、清晰度、重复结构和跳读点；
- `novel-fast-reader-character`：人物欲望、声音、关系和情绪体验；
- `novel-fast-reader-hook`：类型承诺、回报、规则理解和追读动力。

## 标准流程

```text
读取设定与已有正文
→ 主 Claude 规划未来十章
→ prepare-production 生成五个互不冲突的写手路径
→ 五名写手并行产出五章 raw 草稿
→ production-status 检查齐套、路径和篇幅
→ 主 Claude 顺序整合为五章 canonical staging
→ 篇幅检查与连续性检查
→ prepare-review 冻结候选终稿
→ 三名盲读者并行阅读同一版五章正文
→ 主 Claude 根据反馈局部修订并统定终稿
→ 在批次审读记录写入 reader panel 与 main continuity
→ finalize-review 绑定终稿哈希
→ 逐章 prepare-delta 与 commit
→ 更新后续六至十五章方向
→ audit + export
```

## 为什么写手稿不能直接提交

并行写手看不到相邻章节的实际成稿，只能依靠主 Claude 预先提供的起止接口。因此它们生产的是“高质量原料”，不是正史。主 Claude 必须统一：

- 开场与上一章真实结尾；
- 人物声音和叙述距离；
- 规则、数值和名词口径；
- 场景动作、时间和地点；
- 五章节奏与回报分布；
- 本批结束状态和下一批压力。

## 初始化与配置

要求 Python 3.10 或更高版本。

```bash
python "${CLAUDE_SKILL_DIR}/scripts/novelctl.py" init /path/to/book \
  --title '书名' --genre '类型' \
  --chapter-min-chars 3000 --chapter-target-chars 3500 --chapter-soft-max-chars 4000 \
  --batch-size 5 --planning-window 10 \
  --writer-pool-size 5 --blind-reader-count 3
```

长期要求应写入项目设置：

```bash
python "${CLAUDE_SKILL_DIR}/scripts/novelctl.py" configure /path/to/book \
  --min-chars 3000 --target-chars 3500 --soft-max-chars 4000 \
  --batch-size 5 --planning-window 10 \
  --writer-pool-size 5 --blind-reader-count 3
```

优先级：当前用户明确要求 > 项目设置 > Skill 默认值。

## 并行写手准备

```bash
python "${CLAUDE_SKILL_DIR}/scripts/novelctl.py" prepare-production /path/to/book
```

返回五个唯一输出路径，例如：

```text
.novel/production/batch-0001-0005/raw/chapter-0001-novel-fast-writer-1.md
.novel/production/batch-0001-0005/raw/chapter-0002-novel-fast-writer-2.md
...
```

写手完成后：

```bash
python "${CLAUDE_SKILL_DIR}/scripts/novelctl.py" production-status /path/to/book
```

`all_ready=true` 只表示原料稿齐套且达到篇幅底线，不表示可以提交。

## 批次审读记录

主 Claude 顺序整合并修订五章后运行：

```bash
python "${CLAUDE_SKILL_DIR}/scripts/novelctl.py" prepare-review /path/to/book
```

审读记录中的 `first_reader` 代表聚合后的读者面板，至少需要项目配置数量的独立盲读者完成；`continuity` 由主 Claude 填写，`checked_by` 必须为 `main-claude`，blocking 必须为零。

修订完成后：

```bash
python "${CLAUDE_SKILL_DIR}/scripts/novelctl.py" finalize-review /path/to/book
```

之后任何正文修改都会使哈希失效，必须重新 finalize。

## 批次锚点

已有第 1—2 章、从第 3 章开始生产第 3—7 章：

```bash
python "${CLAUDE_SKILL_DIR}/scripts/novelctl.py" configure /path/to/book \
  --batch-start 3 --batch-size 5 --planning-window 10
```

批次边界显式保存在状态中，不使用章号取模。

## 安装

```bash
./install.sh --scope project --project-path /path/to/project
```

```powershell
.\install.ps1 -Scope Project -ProjectPath 'C:\path\to\project'
```

已有同名快速生产版时使用 `--force` / `-Force`。

## 测试

```bash
python tests/run_fast.py
python tests/run_full.py
```

真实 Claude Code 代理启动仍需在安装并登录 `claude` CLI 的环境中测试。Python 回归只能验证定义、路径、状态和脚本行为，不能替代真实模型并发质量测试。

## 许可

许可状态见 `NOTICE.md`。
