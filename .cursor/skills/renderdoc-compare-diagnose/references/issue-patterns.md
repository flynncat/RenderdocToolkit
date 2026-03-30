# Issue Patterns (RenderDoc 对比归因)

## 常见问题与高概率原因

### 1) 物体消失 / 局部不显示

- 深度测试函数变化（`LESS`/`LEQUAL`/`GREATER`）
- 深度写入被关闭或提前清空异常
- 剔除模式变化（`Cull Back/Front/None`）
- 视口、裁剪矩形、RT 尺寸变化导致裁切
- VS 输出位置异常（矩阵、骨骼、实例数据）

### 2) 颜色错误 / 发灰 / 偏色

- SRGB/线性空间状态不一致
- 纹理格式或采样器状态变化（滤波、寻址）
- 常量缓冲内容变更（曝光、色调映射参数）
- Blend 状态变化（预乘/非预乘混用）
- PS 替换或编译宏变化

### 3) 阴影异常（acne、peter-panning、闪烁）

- 深度偏置（Depth Bias / Slope Bias）变化
- 阴影图分辨率或级联分割变化
- 比较采样状态变化（PCF、Compare func）
- Light VP 矩阵抖动或精度问题
- 接收/投射 pass 顺序变化

### 4) 透明度异常（过亮、过暗、层次错）

- Blend 方程变化（Add/Min/Max）
- Src/Dst 因子变化
- Draw 顺序变化（透明排序失效）
- 预乘 Alpha 资源被当作直通 Alpha 使用
- OIT 相关资源丢失或未清理

### 5) 性能回退（FPS 降低）

- Draw call/dispatch 数量暴增
- 纹理与缓冲尺寸/数量增加
- 状态切换频率上升（pipeline churn）
- 额外全屏 pass 或重采样 pass
- 过度同步或 barrier 变化

## 证据优先级

报告时优先引用：

1. 同时命中“用户问题关键词 + pass 名称 + diff 关键词”的证据
2. 与 GPU 状态直接相关的关键词（depth/blend/raster/sampler/shader）
3. 资源与格式变化证据（format/size/mips/srgb）

## 建议验证步骤模板

针对每个根因候选，提供 1-2 条可操作验证：

- 在同一 EID 对比 pipeline state（before/after）
- 导出并对比相关 RT/Depth 贴图
- 固定某状态（如关闭 cull 或改 compare func）做 A/B
- 追踪像素历史，确认覆盖来源与失败原因
