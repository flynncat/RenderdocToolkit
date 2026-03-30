#!/usr/bin/env python3
import argparse
import json
import re
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Tuple


ROOT_CAUSE_RULES = [
    {
        "id": "depth-state-change",
        "title": "深度状态变化导致可见性异常",
        "issue_keywords": ["消失", "看不见", "穿帮", "z-fight", "闪烁", "flicker", "invisible"],
        "diff_keywords": ["depth", "ztest", "z write", "zwrite", "compare", "lequal", "less", "greater"],
        "validation": [
            "在同一 draw event 对比 before/after 的 depth test func 与 depth write。",
            "导出深度图并比对关键区域是否被异常覆盖或未写入。",
        ],
    },
    {
        "id": "cull-raster-change",
        "title": "剔除或光栅状态变化导致几何体丢失",
        "issue_keywords": ["消失", "缺失", "边缘错", "背面", "cull"],
        "diff_keywords": ["cull", "raster", "front", "back", "winding", "scissor", "viewport"],
        "validation": [
            "临时关闭 cull（None）验证几何体是否恢复。",
            "检查 viewport/scissor 尺寸和偏移是否与目标 RT 一致。",
        ],
    },
    {
        "id": "blend-alpha-change",
        "title": "混合或 Alpha 路径变化导致颜色/透明异常",
        "issue_keywords": ["透明", "发白", "发黑", "叠加", "alpha", "颜色不对", "wrong color"],
        "diff_keywords": ["blend", "alpha", "src", "dst", "premult", "equation", "factor"],
        "validation": [
            "核对 src/dst blend factor 与 blend op 是否一致。",
            "确认资源是预乘 alpha 还是直通 alpha，避免混用。",
        ],
    },
    {
        "id": "srgb-format-change",
        "title": "纹理格式或色彩空间变更导致偏色",
        "issue_keywords": ["偏色", "发灰", "过亮", "过暗", "颜色", "gamma", "srgb"],
        "diff_keywords": ["srgb", "format", "rgba", "unorm", "snorm", "texture", "sampler"],
        "validation": [
            "对比目标纹理格式（sRGB/UNORM）与采样器配置。",
            "检查后处理 pass 是否重复做 gamma 或 tone mapping。",
        ],
    },
    {
        "id": "shadow-bias-change",
        "title": "阴影偏置或比较采样变化导致阴影问题",
        "issue_keywords": ["阴影", "acne", "peter", "漏光", "shadow", "抖动"],
        "diff_keywords": ["shadow", "bias", "slope", "pcf", "compare", "depth bias", "cascad"],
        "validation": [
            "核对 depth bias/slope bias 是否变化。",
            "对比 shadow map 分辨率、级联划分与 compare func。",
        ],
    },
    {
        "id": "shader-variant-change",
        "title": "Shader 变体或宏路径变化导致行为回归",
        "issue_keywords": ["异常", "颜色不对", "结果不一致", "回归", "shader"],
        "diff_keywords": ["shader", "variant", "define", "keyword", "ps", "vs", "cs"],
        "validation": [
            "对比 before/after 对应 stage 的 shader 反编译文本和资源绑定。",
            "确认 feature keyword 与材质宏开关一致。",
        ],
    },
    {
        "id": "resource-binding-change",
        "title": "资源绑定变化导致输入数据错误",
        "issue_keywords": ["错纹理", "错法线", "错材质", "黑块", "噪点", "binding"],
        "diff_keywords": ["binding", "descriptor", "set", "slot", "cbuffer", "ubo", "sampler", "texture"],
        "validation": [
            "逐槽对比 descriptor/set/slot 绑定资源是否一致。",
            "检查常量缓冲关键参数（曝光、矩阵、阈值）是否变化。",
        ],
    },
    {
        "id": "drawcount-overhead",
        "title": "Draw/Pass 数量变化导致性能回退",
        "issue_keywords": ["卡顿", "掉帧", "性能", "慢", "fps", "stutter"],
        "diff_keywords": ["draw", "dispatch", "pass", "overdraw", "full screen", "barrier"],
        "validation": [
            "统计 draw/dispatch 变化，并定位新增热点 pass。",
            "检查是否出现额外全屏后处理或重复渲染路径。",
        ],
    },
]


def normalize_text(s: str) -> str:
    return re.sub(r"\s+", " ", s.lower()).strip()


def run_cmd(args: List[str]) -> Tuple[int, str]:
    proc = subprocess.run(
        args,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        shell=False,
    )
    output = (proc.stdout or "") + ("\n" + proc.stderr if proc.stderr else "")
    return proc.returncode, output


def collect_diff(before: Path, after: Path, diff_report: Path = None) -> Tuple[str, str]:
    if diff_report:
        return "external_diff_report", diff_report.read_text(encoding="utf-8", errors="replace")

    rc, out = run_cmd(["rdc", "diff", str(before), str(after)])
    if rc == 0 and out.strip():
        return "rdc diff", out

    fallback = (
        "无法执行 `rdc diff before.rdc after.rdc`。\n"
        "请确认 rdc-cli 已安装并可执行，或通过 --diff-report 提供手工导出的 diff 文本。"
        f"\n\nrc={rc}\n{out}"
    )
    return "rdc diff failed", fallback


def extract_pass_evidence(diff_text: str, pass_name: str, max_items: int = 8) -> List[str]:
    if not pass_name.strip():
        return []
    lines = diff_text.splitlines()
    p = normalize_text(pass_name)
    hits = []
    for idx, line in enumerate(lines):
        if p in normalize_text(line):
            start = max(0, idx - 1)
            end = min(len(lines), idx + 2)
            snippet = " | ".join(x.strip() for x in lines[start:end] if x.strip())
            hits.append(snippet)
            if len(hits) >= max_items:
                break
    return hits


def score_rule(rule: Dict, issue_text: str, diff_text: str, pass_name: str) -> Dict:
    issue_n = normalize_text(issue_text)
    diff_n = normalize_text(diff_text)
    pass_n = normalize_text(pass_name)

    issue_hits = [k for k in rule["issue_keywords"] if normalize_text(k) in issue_n]
    diff_hits = [k for k in rule["diff_keywords"] if normalize_text(k) in diff_n]

    score = 0.0
    score += min(40, len(issue_hits) * 12)
    score += min(45, len(diff_hits) * 6)
    if pass_n and pass_n in diff_n:
        score += 10
    score = min(95, round(score, 1))

    # 从 diff 文本中提取与关键词相关的证据行
    evidence = []
    lines = diff_text.splitlines()
    kw = [normalize_text(k) for k in rule["diff_keywords"]]
    for line in lines:
        ln = normalize_text(line)
        if any(k in ln for k in kw):
            evidence.append(line.strip())
            if len(evidence) >= 5:
                break

    if pass_n:
        for line in lines:
            if pass_n in normalize_text(line):
                evidence.append(line.strip())
                if len(evidence) >= 7:
                    break

    confidence = "low"
    if score >= 70:
        confidence = "high"
    elif score >= 45:
        confidence = "medium"

    return {
        "id": rule["id"],
        "title": rule["title"],
        "score": score,
        "confidence": confidence,
        "issue_hits": issue_hits,
        "diff_hits": diff_hits,
        "evidence": evidence,
        "validation": rule["validation"],
    }


def to_markdown(
    source: str,
    before: Path,
    after: Path,
    pass_name: str,
    issue: str,
    pass_evidence: List[str],
    ranked: List[Dict],
) -> str:
    lines = []
    lines.append("# RenderDoc 对比诊断报告")
    lines.append("")
    lines.append(f"- 生成时间: {datetime.now().isoformat(timespec='seconds')}")
    lines.append(f"- 数据源: `{source}`")
    lines.append(f"- Before: `{before}`")
    lines.append(f"- After: `{after}`")
    lines.append(f"- 目标 Pass: `{pass_name}`")
    lines.append(f"- 问题描述: {issue}")
    lines.append("")

    lines.append("## Pass 相关差异证据")
    if pass_evidence:
        for e in pass_evidence:
            lines.append(f"- {e}")
    else:
        lines.append("- 未在 diff 文本中直接匹配到该 pass 名称。建议确认 pass 命名或提供更精确关键词。")
    lines.append("")

    lines.append("## Top 根因候选")
    for idx, item in enumerate(ranked[:5], start=1):
        lines.append(f"### {idx}. {item['title']}  (score={item['score']}, {item['confidence']})")
        if item["issue_hits"]:
            lines.append(f"- 问题关键词命中: {', '.join(item['issue_hits'])}")
        if item["diff_hits"]:
            lines.append(f"- 差异关键词命中: {', '.join(item['diff_hits'][:8])}")
        if item["evidence"]:
            lines.append("- 证据:")
            for ev in item["evidence"][:5]:
                lines.append(f"  - {ev}")
        lines.append("- 建议验证:")
        for v in item["validation"]:
            lines.append(f"  - {v}")
        lines.append("")

    lines.append("## 结论建议")
    lines.append("- 先验证 Top1 与 Top2，若均不成立，再继续检查 shader 与资源绑定路径。")
    lines.append("- 若问题仅在特定 GPU 出现，补充 `rdc gpus` 与驱动版本对比信息。")
    return "\n".join(lines).strip() + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Compare two RenderDoc captures for a target pass and diagnose likely causes."
    )
    parser.add_argument("--before", required=True, help="Path to before .rdc")
    parser.add_argument("--after", required=True, help="Path to after .rdc")
    parser.add_argument("--pass", dest="pass_name", required=True, help="Target pass name to focus on")
    parser.add_argument("--issue", required=True, help="Issue description from user")
    parser.add_argument("--out-dir", default="out", help="Output directory")
    parser.add_argument("--diff-report", default="", help="Optional pre-exported diff text file")
    args = parser.parse_args()

    before = Path(args.before).expanduser().resolve()
    after = Path(args.after).expanduser().resolve()
    out_dir = Path(args.out_dir).expanduser().resolve()
    diff_report = Path(args.diff_report).expanduser().resolve() if args.diff_report else None

    if not before.exists():
        raise FileNotFoundError(f"before capture not found: {before}")
    if not after.exists():
        raise FileNotFoundError(f"after capture not found: {after}")
    if diff_report and not diff_report.exists():
        raise FileNotFoundError(f"diff report not found: {diff_report}")

    out_dir.mkdir(parents=True, exist_ok=True)

    source, diff_text = collect_diff(before, after, diff_report)
    (out_dir / "raw_diff.txt").write_text(diff_text, encoding="utf-8", errors="replace")

    pass_evidence = extract_pass_evidence(diff_text, args.pass_name)
    scored = [score_rule(rule, args.issue, diff_text, args.pass_name) for rule in ROOT_CAUSE_RULES]
    ranked = sorted(scored, key=lambda x: x["score"], reverse=True)

    report = {
        "meta": {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "source": source,
            "before": str(before),
            "after": str(after),
            "pass_name": args.pass_name,
            "issue": args.issue,
        },
        "pass_evidence": pass_evidence,
        "ranked_causes": ranked,
    }
    (out_dir / "analysis.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    md = to_markdown(source, before, after, args.pass_name, args.issue, pass_evidence, ranked)
    (out_dir / "analysis.md").write_text(md, encoding="utf-8")

    print(f"[OK] 分析完成。输出目录: {out_dir}")
    print(f" - {out_dir / 'analysis.md'}")
    print(f" - {out_dir / 'analysis.json'}")
    print(f" - {out_dir / 'raw_diff.txt'}")


if __name__ == "__main__":
    main()
