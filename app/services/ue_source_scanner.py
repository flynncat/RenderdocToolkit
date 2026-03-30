from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Sequence


@dataclass
class MatchRecord:
    path: str
    score: int
    matched_keywords: List[str]
    group_hits: Dict[str, int]
    sample_lines: List[str]


class UESourceScannerService:
    BASE_KEYWORDS = {
        "attach": ["AttachToComponent", "SetupAttachment", "SetLeaderPoseComponent", "SetMasterPoseComponent", "CopyPoseFromMesh", "Socket", "FaceWear"],
        "material": ["CreateDynamicMaterialInstance", "UMaterialInstanceDynamic", "SetMaterial(", "SetMaterialByName", "SetScalarParameterValue", "SetVectorParameterValue", "MaterialParameterCollection", "MID", "MPC"],
        "face": ["Face", "Facial", "Head", "MorphTarget", "FaceMaterial", "FaceEmote"],
        "sequence": ["LevelSequence", "MovieScene", "Sequencer", "MovieSceneParameterSection"],
    }

    HYPOTHESIS_KEYWORDS = {
        "shader-permutation-switch": ["StaticSwitch", "Quality", "Permutation", "CreateDynamicMaterialInstance", "SetMaterial(", "SetMaterialByName"],
        "resource-chain-shift": ["TextureParameter", "LUT", "Mask", "AO", "Normal", "FaceMaterialSlotName", "BakeFaceMaterial"],
        "uniform-layout-shift": ["MPC", "MID", "SetScalarParameterValue", "SetVectorParameterValue", "ParameterCollection", "CustomPrimitiveData"],
        "upstream-input-drift": ["SetLeaderPoseComponent", "SetMasterPoseComponent", "CopyPoseFromMesh", "MorphTarget", "AnimNode", "FaceEmote"],
    }

    PRIORITY_HINTS = [
        "QQAvatar",
        "MoeGameCore",
        "LetsGoAvatarMerge",
        "AvatarCustomization",
        "Feature_SP",
        "FaceCustomization",
        "MoeCharAvatarComponent",
        "CMShowAvatarManager",
        "CMShowFirstPersonDressManager",
    ]

    SCAN_EXTENSIONS = {".cpp", ".h", ".cs", ".ini", ".uplugin", ".uproject"}

    def run(self, project_root: Path, session_detail: Dict[str, Any], out_dir: Path) -> Dict[str, Any]:
        project_root = project_root.resolve()
        out_dir.mkdir(parents=True, exist_ok=True)

        if not project_root.exists():
            raise FileNotFoundError(f"UE 项目根目录不存在: {project_root}")

        session_summary = (session_detail.get("eid_deep_dive_json") or {}).get("summary") or {}
        top_hypothesis = session_summary.get("top_hypothesis") or {}
        hypothesis_id = top_hypothesis.get("id", "")

        uproject_files = [str(path) for path in project_root.rglob("*.uproject")]
        game_root = self._resolve_game_root(project_root, uproject_files)
        scan_roots = self._build_scan_roots(game_root)
        keywords = self._build_keywords(hypothesis_id)

        matches = self._scan_files(scan_roots, keywords)
        matches.sort(key=lambda item: item.score, reverse=True)

        top_matches = [self._match_to_dict(item) for item in matches[:25]]
        summary = self._build_summary(game_root, uproject_files, top_hypothesis, top_matches)

        report = {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "project_root": str(project_root),
            "game_root": str(game_root),
            "uproject_files": uproject_files,
            "scan_roots": [str(path) for path in scan_roots],
            "top_hypothesis": top_hypothesis,
            "summary": summary,
            "top_matches": top_matches,
        }

        json_path = out_dir / "ue_scan.json"
        md_path = out_dir / "ue_scan.md"
        json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        md_path.write_text(self._to_markdown(report), encoding="utf-8")
        return {"json_path": str(json_path), "md_path": str(md_path), "report": report}

    def _resolve_game_root(self, project_root: Path, uproject_files: Sequence[str]) -> Path:
        if uproject_files:
            return Path(uproject_files[0]).parent
        letsgo = project_root / "LetsGo"
        return letsgo if letsgo.exists() else project_root

    def _build_scan_roots(self, game_root: Path) -> List[Path]:
        candidates = [
            game_root / "Source",
            game_root / "Plugins" / "TMRDC" / "MQ" / "QQAvatar",
            game_root / "Plugins" / "MOE" / "GameFramework" / "GameCore",
            game_root / "Plugins" / "MOE" / "GameFramework" / "GamePlugins" / "Gameplay" / "LetsGoAvatarMerge",
            game_root / "Plugins" / "TMRDC" / "MQ" / "AvatarCustomization",
            game_root / "Plugins" / "ProjectMoe" / "Gameplay" / "MoeGameFeature" / "Feature_SP",
            game_root / "Config",
        ]
        return [path for path in candidates if path.exists()]

    def _build_keywords(self, hypothesis_id: str) -> Dict[str, List[str]]:
        keywords = {group: values[:] for group, values in self.BASE_KEYWORDS.items()}
        dynamic = self.HYPOTHESIS_KEYWORDS.get(hypothesis_id, [])
        if dynamic:
            keywords["hypothesis"] = dynamic
        return keywords

    def _iter_source_files(self, roots: Iterable[Path]) -> Iterable[Path]:
        for root in roots:
            for path in root.rglob("*"):
                if not path.is_file():
                    continue
                if path.suffix.lower() not in self.SCAN_EXTENSIONS:
                    continue
                lowered = str(path).lower()
                if any(token in lowered for token in ("\\binaries\\", "\\intermediate\\", "\\saved\\", "\\deriveddatacache\\")):
                    continue
                yield path

    def _scan_files(self, roots: Sequence[Path], keywords: Dict[str, List[str]]) -> List[MatchRecord]:
        matches: List[MatchRecord] = []
        for path in self._iter_source_files(roots):
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue

            matched_keywords: List[str] = []
            group_hits: Dict[str, int] = {}
            sample_lines: List[str] = []
            score = 0

            for group, words in keywords.items():
                hit_count = 0
                for word in words:
                    if word.lower() in text.lower():
                        hit_count += text.lower().count(word.lower())
                        if word not in matched_keywords:
                            matched_keywords.append(word)
                        if len(sample_lines) < 6:
                            for line in text.splitlines():
                                if word.lower() in line.lower():
                                    sample_lines.append(line.strip())
                                    break
                if hit_count:
                    group_hits[group] = hit_count
                    score += min(hit_count, 5) * 5

            for hint in self.PRIORITY_HINTS:
                if hint.lower() in str(path).lower():
                    score += 10

            if matched_keywords:
                matches.append(
                    MatchRecord(
                        path=str(path),
                        score=score,
                        matched_keywords=matched_keywords[:20],
                        group_hits=group_hits,
                        sample_lines=sample_lines[:6],
                    )
                )
        return matches

    def _build_summary(
        self,
        game_root: Path,
        uproject_files: Sequence[str],
        top_hypothesis: Dict[str, Any],
        top_matches: Sequence[Dict[str, Any]],
    ) -> Dict[str, Any]:
        likely_dirs = []
        for item in top_matches[:10]:
            path = item["path"]
            if "QQAvatar" in path and "QQAvatar" not in likely_dirs:
                likely_dirs.append("QQAvatar")
            if "MoeGameCore" in path and "MoeGameCore" not in likely_dirs:
                likely_dirs.append("MoeGameCore")
            if "LetsGoAvatarMerge" in path and "LetsGoAvatarMerge" not in likely_dirs:
                likely_dirs.append("LetsGoAvatarMerge")
            if "AvatarCustomization" in path and "AvatarCustomization" not in likely_dirs:
                likely_dirs.append("AvatarCustomization")
            if "Feature_SP" in path and "Feature_SP" not in likely_dirs:
                likely_dirs.append("Feature_SP")

        return {
            "top_hypothesis_title": top_hypothesis.get("title", ""),
            "suggested_focus": likely_dirs or ["Source", "Plugins"],
            "project_detected": game_root.name,
            "uproject_count": len(uproject_files),
            "top_file_count": len(top_matches),
            "next_action": (
                "优先查看 Top 匹配文件中与材质实例、挂载、Sequencer 参数轨、面部组件相关的实现，"
                "再决定是否生成 UE 自动验证任务。"
            ),
        }

    @staticmethod
    def _match_to_dict(item: MatchRecord) -> Dict[str, Any]:
        return {
            "path": item.path,
            "score": item.score,
            "matched_keywords": item.matched_keywords,
            "group_hits": item.group_hits,
            "sample_lines": item.sample_lines,
        }

    @staticmethod
    def _to_markdown(report: Dict[str, Any]) -> str:
        summary = report["summary"]
        lines = [
            "# UE 源码扫描报告",
            "",
            f"- 生成时间: {report['generated_at']}",
            f"- 项目根目录: `{report['project_root']}`",
            f"- 游戏工程目录: `{report['game_root']}`",
            f"- 发现 `.uproject` 数量: `{summary['uproject_count']}`",
            f"- 当前 RenderDoc Top 假设: `{summary['top_hypothesis_title'] or '未知'}`",
            "",
            "## 优先关注目录",
        ]
        for item in summary.get("suggested_focus", []):
            lines.append(f"- {item}")
        lines.extend(["", "## Top 可疑文件"])
        for idx, item in enumerate(report.get("top_matches", [])[:15], start=1):
            lines.append(f"### {idx}. `{item['path']}`  (score={item['score']})")
            lines.append(f"- 匹配关键词: {', '.join(item.get('matched_keywords', [])[:10])}")
            if item.get("group_hits"):
                lines.append(f"- 分组命中: {item['group_hits']}")
            for sample in item.get("sample_lines", [])[:3]:
                lines.append(f"- 代码片段: {sample}")
            lines.append("")

        lines.extend(
            [
                "## 建议下一步",
                f"- {summary['next_action']}",
                "- 若高分文件集中在 `QQAvatar / MoeGameCore / LetsGoAvatarMerge`，优先把这些文件纳入自动验证任务。",
                "- 若高分文件集中在 `MovieScene / Sequence / Parameter`，优先排查 Sequencer 与手动改参数路径差异。",
            ]
        )
        return "\n".join(lines) + "\n"
