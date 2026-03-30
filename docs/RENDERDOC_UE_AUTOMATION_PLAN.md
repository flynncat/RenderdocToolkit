# RenderDoc + UE 自动诊断联动方案

## 1. 目标

在现有 RenderDoc 对比分析 Web UI 的基础上，补充一条 UE 自动诊断链路：

- 基于 RenderDoc 首轮分析与 EID 深挖结果
- 自动生成 UE 侧排查建议
- 自动扫描 UE 项目源码与插件源码
- 输出“最可能的业务代码入口”和“自动验证候选路径”

首期不直接控制 UE 编辑器执行蓝图，而是先完成：

1. 诊断结论到 UE 行动项的映射
2. UE 项目源码扫描
3. 将源码扫描结果纳入 session 追问上下文

## 2. 分阶段路线

### Phase 1：源码扫描器

输入：

- UE 项目根目录
- 当前 session 的 RenderDoc 结论

输出：

- `.uproject` 位置
- 核心 Source 模块
- 相关插件
- 最可疑文件列表
- 按根因假设生成的源码排查建议

### Phase 2：测试蓝图生成器

输入：

- RenderDoc 结论
- 源码扫描结果

输出：

- 自动生成的 UE Editor Utility / 测试蓝图模板
- 可执行验证流程

### Phase 3：UE 执行器

输入：

- 诊断任务 JSON

输出：

- UE 内自动执行
- 回传验证结果

## 3. 当前仓库下的已知工程结构

当前确认：

- 游戏工程根：`G:\UGit\LetsgoDevelop2\LetsGo`
- 工程文件：`LetsGo.uproject`
- 模块入口：`LetsGo/Source/LetsGo`
- 高相关插件：
  - `Plugins/TMRDC/MQ/QQAvatar`
  - `Plugins/MOE/GameFramework/GameCore`
  - `Plugins/MOE/GameFramework/GamePlugins/Gameplay/LetsGoAvatarMerge`
  - `Plugins/TMRDC/MQ/AvatarCustomization`
  - `Plugins/ProjectMoe/Gameplay/MoeGameFeature/Feature_SP`

## 4. 源码扫描器设计

### 输入

- `project_root`
- 当前 session 的：
  - `issue`
  - `pass_name`
  - `eid_before/eid_after`
  - `top_hypothesis`

### 扫描目录优先级

1. `LetsGo/Source`
2. `LetsGo/Plugins/TMRDC/MQ/QQAvatar`
3. `LetsGo/Plugins/MOE/GameFramework/GameCore`
4. `LetsGo/Plugins/MOE/GameFramework/GamePlugins/Gameplay/LetsGoAvatarMerge`
5. `LetsGo/Plugins/TMRDC/MQ/AvatarCustomization`
6. `LetsGo/Plugins/ProjectMoe/Gameplay/MoeGameFeature/Feature_SP`

### 扫描关键词

基础关键词：

- `AttachToComponent`
- `SetLeaderPoseComponent`
- `SetMasterPoseComponent`
- `CopyPoseFromMesh`
- `CreateDynamicMaterialInstance`
- `SetMaterial`
- `SetScalarParameterValue`
- `SetVectorParameterValue`
- `MaterialParameterCollection`
- `LevelSequence`
- `MovieScene`
- `Sequencer`
- `MorphTarget`
- `Face`
- `Facial`
- `Head`
- `Socket`

按根因假设动态追加：

- 若是 `shader-permutation-switch`
  - `StaticSwitch`
  - `Quality`
  - `Permutation`
  - `CreateDynamicMaterialInstance`
  - `SetMaterial`
- 若是 `resource-chain-shift`
  - `TextureParameter`
  - `LUT`
  - `Mask`
  - `AO`
  - `Normal`
- 若是 `uniform-layout-shift`
  - `MPC`
  - `MID`
  - `SetScalarParameterValue`
  - `SetVectorParameterValue`

### 输出结构

- `ue_scan.json`
- `ue_scan.md`

包含：

- 工程发现结果
- 扫描目录
- 匹配文件 Top N
- 每个文件的匹配理由
- 推荐优先检查顺序
- 针对当前根因的 UE 侧自动验证建议

## 5. 与 Web UI 集成

新增 session 级接口：

- `POST /api/sessions/{session_id}/ue-source-scan`

入参：

- `project_root`

行为：

1. 读取当前 session
2. 获取 top hypothesis
3. 扫描 UE 工程目录
4. 生成 `ue_scan.md/json`
5. 写入 session artifacts

## 6. 与追问系统集成

追问引擎新增一层上下文优先级：

1. `ue_scan`
2. `eid_deep_dive`
3. `analysis`

这样用户问：

- “代码里该先看哪几个文件？”
- “为什么怀疑 QQAvatar？”
- “这个问题更像挂载还是材质实例？”

时，系统会优先引用源码扫描结果。

## 7. 未来自动化执行方向

当源码扫描器稳定后，下一步可扩展：

- 生成 UE 蓝图节点级验证流程
- 生成 Editor Utility Blueprint / Python 验证脚本
- 自动创建测试 Actor / 测试蓝图
- 自动运行指定 Sequence 并记录材质实例变化
