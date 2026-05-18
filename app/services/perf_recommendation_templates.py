"""Recommendation templates keyed by ``rule_id`` from ``perf_rule_engine``.

Each template provides three role-targeted action lists (TA / 美术 / 引擎),
plus a uniform ``verification`` line and an optional ``expected_gain_text``
the report renderer will display next to the finding.

Keep the wording concrete and actionable.  Avoid jargon-only sentences - the
report is meant to be read by 美术 / QA as well.

When adding a new ``rule_id``, also remember to:
1. Add the rule to ``perf_rule_engine.py``
2. Add a unit test fixture in ``perf_sessions/.../findings.json`` if useful

Templates are pure data; ``perf_report_builder`` is responsible for
formatting them into Markdown / HTML.
"""
from __future__ import annotations

from typing import Any, Dict


TEMPLATES: Dict[str, Dict[str, Any]] = {
    "R001_overdraw_heavy": {
        "title": "过绘制严重",
        "root_cause_hypothesis": (
            "同一像素被多次写入。常见原因是多层透明叠加、毛发壳渲染、"
            "贴花/特效堆叠，或者前后排序不合理导致 early-Z 失效。"
        ),
        "ta": [
            "检查这些 draw 是否能加入 ZPrePass / Depth Equal 路径，让后续 PS 提前 reject",
            "确认材质 blend mode 是否必须保留，可考虑 Masked 替代部分 Translucent",
            "如果是多层 shell / 贴花，按距离裁剪掉远处的额外层",
        ],
        "art": [
            "排查粒子 / 贴花 / 特效是否在镜头近处密集叠加，能否降低覆盖范围或层数",
            "确认资产视觉效果是否值得这部分开销，必要时降级或简化",
        ],
        "engine": [
            "检查 instance 级别的视锥裁剪 / 屏幕面积裁剪是否生效",
            "对成本极高的多层效果考虑 ScreenPercentage / dynamic resolution 兜底",
        ],
        "verification": "再次抓帧时该 EID 的 ps_invocations / coverage_pixels 比值应回落到阈值以下",
        "expected_gain_text": "通常能省下 0.5-3 ms 不等，取决于过绘制层数",
    },
    "R002_fullscreen_heavy_ps": {
        "title": "全屏覆盖 + 重 PS (ALU 填充率瓶颈)",
        "root_cause_hypothesis": (
            "整屏 (>80% 覆盖) 跑了指令数很高 (>200) 的像素着色器。"
            "这类 draw 通常是后处理、全屏 SSAO/SSR、屏幕空间反射、Tonemap+ColorGrade 合成。"
        ),
        "ta": [
            "审视该 shader 的指令构成，能否把多个 pass 合并 / 把 transcendental (pow/sin/exp) 替换为 LUT",
            "检查是否启用了非必要的 quality 分支 (如 high-quality SSR 在低端机)",
            "尝试半分辨率 / quarter-resolution 渲染再 upscale",
        ],
        "art": [
            "确认全屏后处理参数是否在最低必要档位 (如 SSAO samples、Bloom iteration)",
            "对低端档位关闭低性价比的全屏效果",
        ],
        "engine": [
            "如果是 UE 后处理，检查 r.MobileXxx / DefaultFeature.* 等 cvar 是否开启",
            "评估是否能合并多个全屏 pass 减少 RT 切换 + 重新采样",
        ],
        "verification": "改完后该 EID 的 ps_instruction_count 或 ps_invocations 应下降",
        "expected_gain_text": "全屏 pass 的优化通常按毫秒计 (~0.5-2 ms)，且对移动端尤为显著",
    },
    "R003_fullscreen_bandwidth": {
        "title": "全屏覆盖 + 大量贴图 (带宽填充率瓶颈)",
        "root_cause_hypothesis": (
            "全屏 draw 同时绑定了 >=4 张共计 >=4MB 的纹理，"
            "带宽消耗 = 屏幕像素 × 平均每像素采样字节，移动端容易撞 LPDDR 上限。"
        ),
        "ta": [
            "把多张小贴图打成 atlas / 多通道封装到同一张纹理 (RGB+A)",
            "为不同 quality level 配置不同的纹理精度 (BC1 / ASTC 4x4 / 8x8)",
            "审查每张纹理是否真的需要全分辨率 (法线 / AO / mask 通常可减半)",
        ],
        "art": [
            "排查是否有 RGBA8 当 R8 用 / 重复贴图占了多个 slot",
            "美术贴图压缩格式是否符合该 quality level 的预设",
        ],
        "engine": [
            "对 mobile / Vulkan 抓帧确认是否触发了非压缩 fallback (RGBA8 vs ASTC)",
            "看是否能在 shader 内用 textureLod(...,0) 替代默认 mip 选择，避免 LOD 计算开销",
        ],
        "verification": "再次抓帧后该 EID 的 texture_total_mb 与 texture_count 应下降",
        "expected_gain_text": "带宽收益直接反映在能耗与帧时间上 (~0.3-1.5 ms)",
    },
    "R004_translucency_overdraw": {
        "title": "Translucency Pass 过绘制",
        "root_cause_hypothesis": (
            "Translucency pass 单 draw 的 PS 调用量 >1M，通常是多层 FurShell / "
            "厚体积烟雾 / 屏幕空间粒子 / 角色描边等堆叠造成。"
        ),
        "ta": [
            "检查该材质 blend mode 是否必须是 Translucent，能否降级为 Masked / AlphaToCoverage",
            "若为 FurShell / 多层壳，按距离 / 屏幕面积裁剪 shell 层数 (例如 10 -> 6)",
            "在 shader 末尾增加 alpha < epsilon discard，配合 EARLY_FRAGMENT_TESTS",
        ],
        "art": [
            "降低半透明特效 / 粒子的覆盖面积或密度",
            "确认该资产视觉效果是否值得这部分开销，必要时退到更便宜的实现",
        ],
        "engine": [
            "确认 instance bbox culling / 距离 cull 是否在该 pass 生效",
            "检查是否启用了 SoftEdge / DepthFade，能否提前 discard",
            "评估是否能将 Translucent 改为 Order-Independent 简化排序",
        ],
        "verification": "再次抓帧时该 EID 的 ps_invocations 应明显下降",
        "expected_gain_text": "Translucency 过绘制优化常常一次能省 0.5-1.5 ms",
    },
    "R005_shadow_pass_too_heavy": {
        "title": "ShadowDepths Pass 占比偏高",
        "root_cause_hypothesis": (
            "Shadow Depth 渲染占了整帧 GPU 时间的 >20%，"
            "通常是 shadow map 分辨率过高 / cascade 太多 / 投影投射物过多。"
        ),
        "ta": [
            "降低 shadow map 分辨率或 cascade 数量 (Mobile 2 cascade 通常够)",
            "对小物件 / 远处物件关闭投影 (CastShadow=false)",
            "评估是否能用 distance-field shadow / cached static shadow 替代部分动态阴影",
        ],
        "art": [
            "排查角色 / 道具是否有不必要的 CastShadow",
            "复杂网格的 LOD 是否参与 shadow pass，能否做更激进的 shadow-only LOD",
        ],
        "engine": [
            "检查 r.Shadow.MaxResolution / r.Shadow.CSM.* 配置",
            "考虑 Per-Object shadow 替代 CSM 用于关键物件",
        ],
        "verification": "ShadowDepths pass 的 GPU 时间百分比应回落到阈值以下",
        "expected_gain_text": "Shadow 优化收益通常 0.5-2 ms，移动端尤其敏感",
    },
    "R006_post_processing_heavy": {
        "title": "PostProcessing Pass 占比偏高",
        "root_cause_hypothesis": (
            "后处理占整帧 >25%，可能是 SSAO/SSR/Bloom/Tonemap/AA 等多个全屏特效叠加，"
            "或者 LUT / ColorGrading 用了高精度纹理。"
        ),
        "ta": [
            "盘点当前启用的全屏后处理特效，关闭低端档位上低性价比的项",
            "把多个全屏 pass 合并为一个 composite pass，减少 RT 切换",
            "评估半分辨率渲染 + 后处理升采样",
        ],
        "art": [
            "确认 ColorGrading LUT 是否使用合理精度 (32x32x32 通常够移动端)",
            "Bloom iteration / threshold 是否过低导致过度采样",
        ],
        "engine": [
            "检查 r.PostProcessAAQuality / r.BloomQuality / r.MobileSSGI 等 cvar",
            "评估 TSR / TAAU / FSR 等上采样替代纯像素后处理",
        ],
        "verification": "PostProcessing pass 的百分比应回落到阈值以下",
        "expected_gain_text": "后处理优化通常能省 0.5-2.5 ms",
    },
    "R007_shader_alu_outlier": {
        "title": "个别 PS 着色器指令数明显偏多",
        "root_cause_hypothesis": (
            "该 PS shader 在帧内指令数排名前 5%，且 PS 调用量 >=10万。"
            "常见原因：材质蓝图复杂、permutation 命中了高质量分支、"
            "或者用了昂贵的内置函数 (pow / sin / refract / texGrad)。"
        ),
        "ta": [
            "用 RenderDoc 反汇编 / Mali Offline Compiler 检查 ALU vs Texture 比例",
            "审视材质蓝图：是否有可预计算到顶点 / 常量的中间量",
            "把 pow(x, 2.2) / sqrt(x) 这类可化简的表达式手动展开",
        ],
        "art": [
            "确认该材质是否走了最高质量 permutation，是否应限制到镜头特写时才用",
            "评估能否用查表纹理 (LUT) 替代复杂的运行时计算",
        ],
        "engine": [
            "检查 shader permutation 选择逻辑，避免低端机命中重质量分支",
            "评估是否能用 visual_probe_simplifier 工具做半自动简化验证",
        ],
        "verification": "改完后该 shader 的 instruction_count 应明显下降",
        "expected_gain_text": "高频 shader 每减一条指令都能放大成像素级开销，收益通常 0.2-1 ms",
    },
    "R008_huge_texture_low_use": {
        "title": "大体量贴图但屏幕占用很小",
        "root_cause_hypothesis": (
            "该 draw 绑定的纹理总量 >=16MB 但屏幕覆盖 <5%，"
            "意味着大量纹理被采样但渲染像素极少，性价比极低。"
        ),
        "ta": [
            "审视这些大纹理是否真的需要绑定到该 draw (可能是 leftover slot)",
            "把高频小覆盖物件迁移到独立的 atlas 或更小的纹理变体",
            "考虑用 Imposter / Billboard 替代远处大物件",
        ],
        "art": [
            "确认资产纹理分辨率是否过高 (远处物件常被设置成 2K 而实际只占几十像素)",
            "评估能否使用 streaming pool 控制按距离加载的分辨率",
        ],
        "engine": [
            "开启 Texture Streaming 并检查 streaming distance 是否合理",
            "对小物件强制 mip bias 让其使用更低 mip",
        ],
        "verification": "再次抓帧时该 EID 的 texture_total_mb 应下降，或该 draw 被裁剪掉",
        "expected_gain_text": "节省 GPU 内存与带宽，配合 streaming 可见加载抖动减少",
    },
    "R010_high_tri_low_pixel": {
        "title": "高三角面但像素覆盖极低 (微小三角形浪费)",
        "root_cause_hypothesis": (
            "该 draw 提交了 >1万 三角面但只覆盖 <1% 屏幕，"
            "意味着大部分三角形小于 1 像素，光栅化效率极低 (TBDR 上尤其严重)。"
        ),
        "ta": [
            "为该网格制作更激进的 LOD，远距离 LOD 三角面数减少 60-80%",
            "评估是否能用 Imposter / Decimated mesh 替代远处实例",
            "检查 mesh 是否被冗余实例化 (HISM 拷贝太多)",
        ],
        "art": [
            "美术 LOD 制作时确认 LOD2/LOD3 的简化比例",
            "对小型装饰物开启距离淘汰",
        ],
        "engine": [
            "启用 / 调整 Nanite 或 GPU-driven culling (如适用)",
            "审视 HISM / ISM 的距离裁剪与 minDrawDistance",
        ],
        "verification": "再次抓帧时该 EID 的 triangles 应下降，或被 cull 掉",
        "expected_gain_text": "顶点处理与光栅化都会受益，移动端尤为明显",
    },
    "R014_unique_texture_explosion": {
        "title": "单 Pass 内唯一纹理种类爆炸",
        "root_cause_hypothesis": (
            "同一个 scene pass 引用了 >=50 张不同的纹理资源，"
            "造成 GPU 纹理缓存命中率低、CPU 端绑定状态切换频繁。"
        ),
        "ta": [
            "把高频小贴图打包到 atlas，减少独立 binding 数量",
            "审视该 pass 的材质数量，是否能合并到更少的 master material",
        ],
        "art": [
            "盘点这一类资产是否每个个体都用了独立 unique 贴图，能否复用",
            "对装饰类资产强制使用 atlas / trim sheet",
        ],
        "engine": [
            "检查 pass 内是否有不必要的 dynamic material instance 导致绑定爆炸",
            "评估 bindless texture / texture array 支持情况",
        ],
        "verification": "再次抓帧时该 pass 的 unique texture 总数应明显下降",
        "expected_gain_text": "CPU 绑定开销与 GPU 缓存命中都会受益",
    },
}


def get_template(rule_id: str) -> Dict[str, Any]:
    """Return the template for ``rule_id``, or a safe default if unknown."""
    if rule_id in TEMPLATES:
        return TEMPLATES[rule_id]
    return {
        "title": rule_id,
        "root_cause_hypothesis": "暂无模板。",
        "ta": [],
        "art": [],
        "engine": [],
        "verification": "",
        "expected_gain_text": "",
    }
