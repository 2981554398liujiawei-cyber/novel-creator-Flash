#!/usr/bin/env python3
from __future__ import annotations

import argparse
import difflib
import json
import re
import statistics
from collections import Counter, defaultdict
from pathlib import Path

from common import atomic_write_text, list_chapters, output_under, read_text, validate_workspace_layout

FILTER_WORDS = ("看见", "听见", "感觉到", "意识到", "注意到", "发现自己")
ABSTRACT_EMOTIONS = ("紧张", "愤怒", "悲伤", "震惊", "恐惧", "激动", "焦虑", "不安", "绝望", "感动")
CLICHE_ACTIONS = ("瞳孔骤缩", "倒吸一口凉气", "嘴角勾起", "心中一凛", "眉头一皱", "眼神一凝", "浑身一震")
HEDGES = ("仿佛", "似乎", "不禁", "忍不住", "莫名", "隐隐", "下意识")
DIALOGUE_TAGS = ("说道", "问道", "回答道", "开口道", "淡淡地说", "沉声说道")


def paragraphs(text: str) -> list[str]:
    return [" ".join(part.split()) for part in re.split(r"\n\s*\n", text) if len(" ".join(part.split())) >= 30]


def ending(text: str, length: int = 220) -> str:
    clean = " ".join(text.split())
    return clean[-length:]


def ending_type(text: str) -> str:
    tail = ending(text, 180)
    if re.search(r"[？?][”’\"']?\s*$", tail):
        return "question"
    if re.search(r"(门|电话|铃声|脚步|消息|来信|敲门|有人来了).{0,18}[。！!?]?\s*$", tail):
        return "arrival_or_signal"
    if re.search(r"(决定|选择|答应|拒绝|发誓|必须|要去|不会再).{0,30}[。！]?\s*$", tail):
        return "decision"
    if re.search(r"(原来|竟然|真相|身份|名字是|就是他|就是她).{0,30}[。！]?\s*$", tail):
        return "reveal"
    if re.search(r"(血|火|刀|枪|杀|死|危险|追来|逼近).{0,30}[。！]?\s*$", tail):
        return "threat"
    return "aftermath_or_image"


def count_terms(text: str, terms: tuple[str, ...]) -> Counter[str]:
    return Counter({term: text.count(term) for term in terms if text.count(term)})


def density_warning(
    label: str,
    terms: tuple[str, ...],
    texts: list[tuple[int, str]],
    *,
    min_total: int,
    min_per_10k: float,
    min_chapters: int,
) -> str | None:
    all_text = "\n".join(text for _, text in texts)
    counts = count_terms(all_text, terms)
    total = sum(counts.values())
    total_chars = len(all_text)
    per_10k = total * 10000 / max(1, total_chars)
    hit_chapters = [number for number, text in texts if any(term in text for term in terms)]
    required_chapters = min(max(1, min_chapters), max(1, len(texts)))
    if total < min_total or per_10k < min_per_10k or len(set(hit_chapters)) < required_chapters:
        return None
    details = "、".join(f"{term}×{count}" for term, count in counts.most_common(6))
    chapter_note = "、".join(str(number) for number in sorted(set(hit_chapters))[:8])
    return (
        f"{label}密度偏高：共 {total} 次（约 {per_10k:.1f}/万字，分布于第{chapter_note}章）；"
        f"{details}。低可信词频提示，仅在具体段落确实机械时处理。"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Lightweight prose-risk and repetition scan; it does not judge literary quality.")
    parser.add_argument("workspace", nargs="?", default=".")
    parser.add_argument("--recent", type=int, default=30)
    parser.add_argument("--output", default="quality-scan.md")
    args = parser.parse_args()
    if args.recent < 1:
        parser.error("recent must be positive")

    root = Path(args.workspace).resolve(strict=True)
    try:
        validate_workspace_layout(root)
    except ValueError as exc:
        parser.error(str(exc))
    selected = list_chapters(root)[-max(2, args.recent) :]
    texts = [(number, read_text(path)) for number, path in selected]
    warnings: dict[str, list[str]] = defaultdict(list)

    paragraph_sources: dict[str, list[int]] = {}
    for number, text in texts:
        for paragraph in paragraphs(text):
            normalized = re.sub(r"\s+", "", paragraph)
            paragraph_sources.setdefault(normalized, []).append(number)
    for paragraph, chapters in paragraph_sources.items():
        unique = sorted(set(chapters))
        if len(unique) >= 2 and len(paragraph) >= 60:
            warnings["跨章重复"].append(f"较长段落在第{', '.join(map(str, unique[:8]))}章重复；片段：{paragraph[:80]}…")

    for index, (number_a, text_a) in enumerate(texts):
        end_a = ending(text_a)
        for number_b, text_b in texts[index + 1 :]:
            ratio = difflib.SequenceMatcher(None, end_a, ending(text_b)).ratio()
            if ratio >= 0.82:
                warnings["章末重复"].append(f"第{number_a}章与第{number_b}章章末表达相似度 {ratio:.2f}")

    sentence_starts: Counter[str] = Counter()
    ending_types: list[tuple[int, str]] = []
    all_text = "\n".join(text for _, text in texts)
    sentence_lengths: list[int] = []
    paragraph_lengths: list[int] = []
    exposition: list[tuple[int, str]] = []
    for number, text in texts:
        ending_types.append((number, ending_type(text)))
        for sentence in re.split(r"[。！？!?]", text):
            clean = re.sub(r"\s+", "", sentence)
            if len(clean) >= 8:
                sentence_starts[clean[:8]] += 1
                sentence_lengths.append(len(clean))
        for paragraph in paragraphs(text):
            compact = re.sub(r"\s+", "", paragraph)
            paragraph_lengths.append(len(compact))
            quote_count = compact.count("“") + compact.count('"')
            explain_terms = sum(compact.count(term) for term in ("所谓", "事实上", "意味着", "因为", "因此", "历史", "规则", "原理"))
            if len(compact) >= 220 and quote_count <= 1 and explain_terms >= 3:
                exposition.append((number, compact[:100]))

    for start, count in sentence_starts.most_common(15):
        if count >= max(5, len(texts) // 2):
            warnings["句式重复"].append(f"高频句首 `{start}` 出现 {count} 次")

    for label, terms, min_total, min_density, min_chapters in (
        ("过滤词", FILTER_WORDS, 8, 8.0, 2),
        ("抽象情绪词", ABSTRACT_EMOTIONS, 8, 6.0, 2),
        ("高频惯用动作", CLICHE_ACTIONS, 3, 1.5, 2),
        ("模糊与缓冲词", HEDGES, 10, 8.0, 2),
        ("对白标签", DIALOGUE_TAGS, 8, 8.0, 2),
    ):
        message = density_warning(
            label,
            terms,
            texts,
            min_total=min_total,
            min_per_10k=min_density,
            min_chapters=min_chapters,
        )
        if message:
            warnings["模板化风险"].append(message)

    if len(sentence_lengths) >= 20:
        mean = statistics.fmean(sentence_lengths)
        deviation = statistics.pstdev(sentence_lengths)
        if mean >= 10 and deviation / mean < 0.28:
            warnings["节奏均匀"].append(f"句长变化偏小：平均 {mean:.1f} 字，标准差 {deviation:.1f}；检查是否缺少快慢变化。")
    if len(paragraph_lengths) >= 8:
        mean = statistics.fmean(paragraph_lengths)
        deviation = statistics.pstdev(paragraph_lengths)
        if mean >= 45 and deviation / mean < 0.22:
            warnings["节奏均匀"].append(f"段长变化偏小：平均 {mean:.1f} 字，标准差 {deviation:.1f}。")
    for number, excerpt in exposition[:8]:
        warnings["设定说明"].append(f"第{number}章可能存在连续设定说明段：{excerpt}…")

    recent_types = ending_types[-6:]
    type_counts = Counter(kind for _, kind in recent_types)
    for kind, count in type_counts.items():
        if count >= 3:
            chapters = [str(number) for number, value in recent_types if value == kind]
            warnings["结尾类型"].append(f"最近章节中 `{kind}` 结尾出现 {count} 次：第{', '.join(chapters)}章；考虑改变结束方式。")

    total_warnings = sum(len(items) for items in warnings.values())
    lines = [
        "# 文体与模板化风险扫描", "",
        "> 这些结果只是报警器，不是文学评分器。命中表达不等于错误，不能机械删除。", "",
        f"- 扫描章节：{len(texts)}",
        f"- 风险提示：{total_warnings}", "",
    ]
    if warnings:
        for category in sorted(warnings):
            lines.extend([f"## {category}", ""])
            lines.extend(f"- {item}" for item in warnings[category])
            lines.append("")
    else:
        lines.extend(["- 未发现达到阈值的重复或模板化风险。", ""])
    output = output_under(root, "audits", args.output, "quality-scan.md")
    atomic_write_text(output, "\n".join(lines).rstrip() + "\n")
    print(json.dumps({"warnings": total_warnings, "categories": sorted(warnings), "output": output.relative_to(root).as_posix()}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
