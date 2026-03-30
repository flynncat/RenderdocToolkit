# RenderDoc 对比分析 Web UI 技术方案

## 1. 目标

构建一个本地运行的 Web UI 工具，用于：

- 拖入两个 `.rdc` 文件
- 必填 `pass`，用于聚焦具体渲染问题
- 输入问题描述
- 可选补充 `EID before / EID after`
- 自动调用已配置的 RenderDoc 分析能力与 Skill
- 生成偏“结论建议”的诊断结果
- 将每次分析保存为独立 `session`
- 基于当前 session 继续追问

该工具运行在本机，不依赖远程文件服务，默认面向单用户使用。

## 2. 产品边界

### 2.1 MVP 范围

- 单机运行
- 本地 Web UI
- 单次上传两个 `.rdc`
- 调用现有分析脚本生成首轮报告
- 保存 session
- 基于 session 继续追问
- 支持补充 EID 信息
- 支持查看已有 session 列表与详情

### 2.2 非 MVP 范围

- 多用户权限系统
- 云端存储
- 分布式任务队列
- 真正完整的 RenderDoc GUI 自动化
- 完整像素级可视化差分面板
- 大模型供应商绑定死在代码里

## 3. 总体架构

采用本地前后端一体化架构：

1. Web UI
   - 文件拖拽上传
   - 分析表单
   - session 列表
   - 报告展示
   - 追问面板

2. 后端服务
   - FastAPI 提供 HTTP API
   - 负责保存文件、创建 session、调度分析脚本
   - 负责上下文装配与追问响应

3. RenderDoc 分析执行层
   - 调用 `rdc-cli`
   - 调用现有 `compare_pass_issue.py`
   - 后续扩展为 EID 深挖、像素历史、debug pixel 等

4. Session 存储层
   - 以目录形式保存输入、分析产物、问答历史
   - 每个 session 一份独立上下文

5. LLM Provider 抽象层
   - 默认提供本地回退策略
   - 预留 OpenAI-compatible API 接口
   - 后续接入真实推理服务时无需改动 UI 和 session 结构

## 4. 推荐技术选型

### 后端

- Python 3.10+
- FastAPI
- Uvicorn
- Pydantic
- Jinja2

原因：

- 与现有 RenderDoc 脚本栈一致
- 调用本地 CLI 简单
- 容易做 session 文件管理
- 后续接入 LLM 方便

### 前端

- 服务端模板 + 原生 JavaScript
- 不引入前端构建工具

原因：

- MVP 开发快
- 依赖少
- 本地工具型项目维护成本低

### 存储

- 文件系统目录存储
- `sessions/<session_id>/...`

原因：

- 对本地工具最简单
- 方便查看原始分析产物
- 适合问题复盘与追问上下文复用

## 5. 目录设计

```text
docs/
  RENDERDOC_WEBUI_TECHNICAL_PLAN.md

app/
  main.py
  config.py
  services/
    analyzer.py
    chat_engine.py
    session_store.py
  templates/
    index.html
  static/
    app.css
    app.js

sessions/
  <session_id>/
    metadata.json
    chat_history.json
    inputs/
      before.rdc
      after.rdc
    analysis/
      analysis.md
      analysis.json
      raw_diff.txt
      run_log.txt

.cursor/skills/renderdoc-compare-diagnose/
  ...
```

## 6. Session 数据模型

### 6.1 metadata.json

建议结构：

```json
{
  "session_id": "20260325-145500-abc123",
  "created_at": "2026-03-25T14:55:00",
  "updated_at": "2026-03-25T14:56:20",
  "status": "completed",
  "inputs": {
    "before_file": "inputs/before.rdc",
    "after_file": "inputs/after.rdc",
    "pass_name": "M_Matcap_Glitter_SSS_Trans SK_OG_069_Wai_001",
    "issue": "面部亮度异常",
    "eid_before": "575",
    "eid_after": "610"
  },
  "artifacts": {
    "analysis_md": "analysis/analysis.md",
    "analysis_json": "analysis/analysis.json",
    "raw_diff": "analysis/raw_diff.txt",
    "run_log": "analysis/run_log.txt"
  },
  "summary": {
    "title": "面部亮度异常诊断",
    "top_cause": "输入插值或采样输入差异",
    "confidence": "medium"
  }
}
```

### 6.2 chat_history.json

建议结构：

```json
[
  {
    "role": "user",
    "content": "为什么只有面部有问题？",
    "created_at": "2026-03-25T15:00:00"
  },
  {
    "role": "assistant",
    "content": "面部使用独立材质路径，且追踪到输入 trace 在首步就已分叉。",
    "created_at": "2026-03-25T15:00:02",
    "sources": [
      "analysis/analysis.json"
    ]
  }
]
```

## 7. 接口设计

### 7.1 `GET /`

- 返回主页面

### 7.2 `GET /api/health`

- 返回环境健康状态
- 包含：
  - `python`
  - `rdc`
  - `renderdoc_python_path`
  - `analysis_script_exists`

### 7.3 `GET /api/sessions`

- 返回 session 列表
- 按更新时间倒序

### 7.4 `GET /api/sessions/{session_id}`

- 返回单个 session 详情
- 包含 metadata、首轮报告、聊天记录

### 7.5 `POST /api/analyze`

表单字段：

- `before_file`：文件，必填
- `after_file`：文件，必填
- `pass_name`：字符串，必填
- `issue`：字符串，必填
- `eid_before`：字符串，可选
- `eid_after`：字符串，可选

行为：

1. 创建 session
2. 保存上传文件
3. 调用分析脚本
4. 生成 metadata
5. 返回 session 详情

### 7.6 `POST /api/sessions/{session_id}/chat`

请求体：

```json
{
  "question": "为什么只有面部异常？"
}
```

行为：

1. 读取当前 session 的 metadata、analysis.json、analysis.md、历史问答
2. 组装上下文
3. 调用 chat engine
4. 返回回答并写入 chat_history

## 8. 分析执行流程

### 8.1 首轮分析

1. 校验输入
2. 保存 `.rdc`
3. 运行：

```powershell
python .cursor/skills/renderdoc-compare-diagnose/scripts/compare_pass_issue.py `
  --before "<before>" `
  --after "<after>" `
  --pass "<pass_name>" `
  --issue "<issue>" `
  --out-dir "<session_analysis_dir>"
```

4. 若用户补充 `eid_before / eid_after`：
   - 写入 metadata
   - 作为后续追问的强上下文

### 8.2 继续追问

追问不直接重新跑整套流程，而是优先：

1. 读取现有分析结果
2. 从问题中识别意图：
   - 原因解释
   - 建议验证
   - 需要补跑 EID 深挖
3. 如有需要，再执行增量命令
4. 生成偏结论建议的回答

## 9. Chat Engine 设计

### 9.1 目标

实现“像现在这样继续问”，但不把系统绑死在单一模型服务上。

### 9.2 设计

提供两层：

1. `PromptBuilder`
   - 读取 session 上下文
   - 组织成标准 prompt

2. `Provider`
   - `LocalFallbackProvider`
   - `OpenAICompatibleProvider`

### 9.3 MVP 策略

MVP 先实现：

- 本地回退 provider
- 基于已有分析结果、关键词和规则模板作答

后续再接：

- 任意 OpenAI-compatible API
- 通过环境变量配置 `BASE_URL / API_KEY / MODEL`

### 9.4 当前接入状态

当前原型已经支持：

- `LocalFallbackProvider`
- `OpenAICompatibleProvider`

运行规则：

1. 若 `RENDERDOC_WEBUI_LLM_PROVIDER=openai_compatible`
2. 且 `OPENAI_BASE_URL / OPENAI_API_KEY / OPENAI_MODEL` 配置完整
3. 则追问走真实远程推理
4. 否则自动回退到本地规则引擎

推荐环境变量：

```powershell
$env:RENDERDOC_WEBUI_LLM_PROVIDER = "openai_compatible"
$env:RENDERDOC_WEBUI_OPENAI_BASE_URL = "https://your-api-host/v1"
$env:RENDERDOC_WEBUI_OPENAI_API_KEY = "your_api_key"
$env:RENDERDOC_WEBUI_OPENAI_MODEL = "your_model_name"
$env:RENDERDOC_WEBUI_OPENAI_TIMEOUT_SECONDS = "60"
```

## 10. UI 设计

### 页面区域

1. 左侧输入区
   - before/after 文件拖拽
   - pass
   - issue
   - eid_before / eid_after
   - 分析按钮

2. 中间结果区
   - session 概览
   - 结论建议
   - Top 根因
   - 原始报告

3. 右侧追问区
   - 历史对话
   - 输入框
   - 发送按钮

### 交互原则

- 首轮分析必须形成独立 session
- 每次追问都绑定当前 session
- 优先展示结论建议，再展示证据

## 11. 错误处理

### 环境类错误

- `rdc` 不存在
- `rdc doctor` 失败
- 分析脚本路径缺失

处理：

- 在 `/api/health` 明确返回
- UI 顶部红色告警

### 输入类错误

- 非 `.rdc` 文件
- 缺失 pass
- 缺失 issue

处理：

- 前端拦截
- 后端二次校验

### 执行类错误

- `rdc diff failed`
- `SaveTexture failed`
- `no color targets`

处理：

- 保存 `run_log.txt`
- 前端展示“分析失败但已保存 session”
- 允许用户继续补充 EID 和追问

## 12. 安全与约束

- 工具仅操作本地用户显式上传的文件
- session 文件默认保存在工作区 `sessions/`
- 不自动上传 `.rdc` 到远端
- 默认不删除原始输入文件

## 13. MVP 交付清单

- 技术方案文档
- Web UI 页面
- 分析 API
- session 管理 API
- 追问 API
- 本地回退问答引擎
- 启动说明

## 14. 后续扩展路线

### Phase 2

- 接入 OpenAI-compatible LLM
- 支持命令级增量执行
- 针对 EID 补跑 pipeline/bindings/shader

### Phase 3

- 支持像素历史自动深挖
- 支持图片导出与差异热区展示
- 支持问题模板库

### Phase 4

- 更完整的 RenderDoc 工作流自动化
- 会话内多轮分析分支管理
- 团队共享报告模板
