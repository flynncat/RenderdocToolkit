# RenderDoc 工具集

一个面向本地使用的 RenderDoc 桌面工具集，用来帮助图形程序、TA 和技术美术更高效地分析 `.rdc` 抓帧、比较差异、查看性能热点，以及批量导出资产。

项目当前以 Windows 本地桌面模式为主，界面运行在内嵌窗口中，直接读写本机文件，不依赖远程上传大型抓帧文件。

![应用首页总览](docs/images/overview-home.png)

## 功能总览

工具当前提供四个主要功能页：

| 功能 | 说明 |
| --- | --- |
| `问题诊断` | 对比两份 `.rdc`，结合目标 Pass、问题描述、可选 EID，生成首轮诊断报告，并支持继续追问 |
| `性能 Diff` | 基于 `renderdoc_cmp` 对两份抓帧做性能差异分析，并在界面内查看 HTML 报告 |
| `性能` | 针对单个 `.rdc` 做 Pass 级性能分析，支持多维排序、热点提示和绘制预览 |
| `资产批量导出` | 扫描 Pass、批量导出资产、检查 CSV 列映射，并将 CSV 转换为 `FBX` / `OBJ` |

## 适用场景

- 比较两次抓帧的渲染差异，快速定位“亮度不一致”“材质异常”“某个部位表现异常”等问题
- 复盘单帧性能，快速找出高开销 Pass 和热点绘制项
- 从抓帧中批量导出网格和贴图，并完成 CSV 到模型格式的转换
- 在本地持续保存分析记录、历史任务和结果文件，便于回查

## 快速开始

### 运行前准备

- Windows 环境
- 已安装 RenderDoc
- 可用的 Python 环境

如果仓库后续提供发布包，优先使用 Release 中的绿色包；如果是从源码运行，请按下面步骤安装依赖并启动。

安装依赖：

```powershell
python -m pip install -r requirements.txt
```

启动桌面工具：

```powershell
python launcher.py
```

程序会启动本地服务，并以内嵌桌面窗口打开界面。

### 首次配置

首次使用时，建议在界面右上角的 `环境设置` 中确认以下项目：

- `RenderDoc Python Path`
- `LLM Provider`
- `OpenAI-compatible Base URL`
- `OpenAI-compatible API Key`
- `OpenAI-compatible Model`
- `renderdoc_cmp 根目录`

如果不配置在线模型，工具会使用本地回退逻辑完成基础问答与分析流程。

## 使用文档

- [用户使用指南](docs/USER_GUIDE.md)
- [截图清单与拍摄规范](docs/SCREENSHOT_CHECKLIST.md)

## 常见工作流

### 1. 问题诊断

1. 打开 `问题诊断`
2. 选择 `Before` 和 `After` 两份 `.rdc`
3. 输入目标 Pass 和问题描述
4. 如有需要，补充 `EID Before / EID After`
5. 点击 `开始分析`
6. 在当前 Session 中继续做 `EID 深挖`、`UE 源码扫描` 和追问

### 2. 性能 Diff

1. 打开 `性能 Diff`
2. 选择两份待比较的 `.rdc`
3. 按需填写 `RenderDoc` / `Malioc` 路径和附加选项
4. 点击 `执行性能 Diff`
5. 在中间区域查看内嵌报告和运行日志

![性能 Diff 界面](docs/images/cmp-report.png)

### 3. 单帧性能分析

1. 打开 `性能`
2. 选择单个 `.rdc`
3. 点击 `执行性能分析`
4. 使用排序维度和排序方向查看热点
5. 结合图表、日志和绘制预览定位高开销 Pass

### 4. 资产导出与 CSV 转模型

1. 打开 `资产批量导出`
2. 读取 Pass 列表并选择导出范围
3. 按需启用 `FBX` / `OBJ`
4. 导出前确认批量映射
5. 在 `CSV 列映射预览` 中检查或调整列映射
6. 对单个 CSV、多份散点 CSV 或整个目录执行转换

![资产批量导出与 CSV 转换](docs/images/asset-export.png)

## 已知限制

- 当前主要面向 Windows 本地桌面环境
- 依赖本机安装 RenderDoc
- 不同图形 API、不同抓帧来源下，部分计数器或命名信息可能存在差异
- 某些移动端 / 真机抓帧的表现与桌面抓帧不同，分析结果需要结合实际 RenderDoc 视图交叉确认

## 项目结构

```text
app/                     FastAPI 应用与前端模板
docs/                    用户文档与技术方案文档
config/                  本地配置
launcher.py              桌面入口
build_portable.ps1       绿色包打包脚本
RenderdocDiffTools.spec  PyInstaller 打包配置
```

## 延伸阅读

这些文档更偏技术方案与设计背景，适合希望了解内部实现的读者：

- [RenderDoc Web UI 技术方案](docs/RENDERDOC_WEBUI_TECHNICAL_PLAN.md)
- [RenderDoc 与 UE 自动诊断方案](docs/RENDERDOC_UE_AUTOMATION_PLAN.md)
