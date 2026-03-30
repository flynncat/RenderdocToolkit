---
name: renderdoc-compare-diagnose
description: Compare two RenderDoc captures for a target pass and diagnose likely root causes for rendering issues. Use when the user provides two .rdc files, asks for frame/pass diffs, GPU regression analysis, or root-cause diagnosis from capture comparison.
---

# RenderDoc Compare Diagnose

## 适用场景

当用户提供以下输入时使用本技能：

- 两个 RenderDoc capture 文件（`.rdc`）
- 需要聚焦的 pass 名称（如 `ShadowPass`、`GBuffer`、`Transparent`）
- 问题描述（如“颜色偏灰”“物体消失”“阴影抖动”“性能下降”）

## 目标输出

输出一个“可执行结论包”：

1. 关键差异摘要（与目标 pass 相关）
2. Top 根因候选（按置信度排序）
3. 每个候选的证据（来自 diff 文本）
4. 建议验证步骤（下一步在 RenderDoc 里如何确认）

## 快速流程

1. 确认环境：`rdc` 命令可用
2. 运行对比分析脚本（见下方命令）
3. 查看生成的 `analysis.md` 与 `analysis.json`
4. 将结论反馈给用户，并给出验证建议

## 命令

```powershell
python .cursor/skills/renderdoc-compare-diagnose/scripts/compare_pass_issue.py `
  --before "D:/captures/before.rdc" `
  --after "D:/captures/after.rdc" `
  --pass "ShadowPass" `
  --issue "阴影出现严重 acne 和闪烁" `
  --out-dir ".cursor/skills/renderdoc-compare-diagnose/out"
```

## 输出文件

- `analysis.json`: 机器可读，包含分数、证据、命中规则
- `analysis.md`: 人类可读，适合直接给用户
- `raw_diff.txt`: 原始 diff 文本（若成功执行 `rdc diff`）

## 诊断策略

- 先以用户给定 `pass` 为主过滤证据，再补充全局差异
- 使用问题关键词（issue）+ 差异关键词（diff）做联合评分
- 优先报告“状态变化 + 资源变化 + shader 变化”交集项

## 如果本技能信息不足

按以下顺序退化处理：

1. 先继续使用本技能，但要求补充：
   - 更具体的 pass 名称
   - 问题截图或像素位置
   - 是否仅某 GPU/分辨率复现
2. 若 `rdc diff` 不可用，导入手工 diff 文本：
   - 使用 `--diff-report path/to/diff.txt`
3. 若需要更深度分析（像素历史、shader debug），切换到 `renderdoc-skill` 仓库推荐流程并使用其 `rdc-cli` 命令集。

## 参考

- 深度问题模式与证据映射见 [references/issue-patterns.md](references/issue-patterns.md)
- 环境安装脚本见 [scripts/setup_env.ps1](scripts/setup_env.ps1)
