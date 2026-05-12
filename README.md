# RenderDoc 工具集

一个面向本地使用的 RenderDoc 桌面工具集，用来帮助图形程序、TA 和技术美术更高效地分析单帧性能、比较性能差异，以及批量导出资产。

项目当前以 Windows 本地桌面模式为主，界面运行在内嵌窗口中，直接读写本机文件，不依赖远程上传大型抓帧文件。

![应用首页总览](docs/images/overview-home.png)

## 功能总览

工具当前提供三个主要功能页：

| 功能 | 说明 |
| --- | --- |
| `性能` | 针对单个 `.rdc` 做 Pass 级性能分析，支持多维排序、热点提示和绘制预览 |
| `性能 Diff` | 基于 `renderdoc_cmp` 对两份抓帧做性能差异分析，并在界面内查看 HTML 报告 |
| `资产批量导出` | 扫描 Pass、批量导出资产、导出当前 draw 的 shader/参数、检查 CSV 列映射，并将 CSV 转换为 `FBX` / `OBJ` |

## 适用场景

- 复盘单帧性能，快速找出高开销 Pass 和热点绘制项
- 比较两次抓帧的性能差异，并直接查看 `renderdoc_cmp` HTML 报告
- 从抓帧中批量导出网格、贴图、shader 与参数，并完成 CSV 到模型格式的转换
- 在本地持续保存性能与导出任务记录，便于回查

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

如果需要手动安装打包依赖，也可以执行：

```powershell
python -m pip install pyinstaller
```

启动桌面工具：

```powershell
python launcher.py
```

程序会启动本地服务，并以内嵌桌面窗口打开界面。

### 打包绿色运行包

项目根目录提供了一键打包脚本：

```bat
build_portable.bat
```

它会自动调用：

```powershell
powershell -ExecutionPolicy Bypass -File .\build_portable.ps1
```

默认输出目录为：

```text
G:\RenderdocDiffTools\RenderdocDiffPortable
```

如果你想指定输出根目录，可以直接给 `.bat` 传参，参数会透传给 `build_portable.ps1`：

```bat
build_portable.bat -OutputRoot "D:\YourOutput"
```

打包脚本会自动完成以下步骤：

- 关闭正在运行的旧绿色包进程
- 清理项目内的 `build/` 和 `dist/`
- 调用 `PyInstaller` 重新打包
- 复制绿色包到目标输出目录
- 打包仓库内置的 `external_tools/renderdoccmp` 运行时
- 生成绿色包内的 `user_data/config/settings.json`
- 自动执行绿色包冒烟测试
- 检查 `RenderdocDiffPortable\RenderdocDiffTools.exe` 是否生成成功，并在成功后自动打开输出文件夹

打包完成后，直接运行以下文件即可：

```text
RenderdocDiffPortable\RenderdocDiffTools.exe
```

### 首次配置

首次使用时，建议在界面右上角的 `环境设置` 中确认以下项目：

- `RenderDoc Python Path`
- `renderdoc_cmp 根目录`

当前仓库已经内置 `renderdoc_cmp` 运行时，默认打包和绿色包运行不再依赖单独下载外部 `renderdoc_cmp` 仓库；只有当你想显式覆盖内置版本时，才需要额外填写 `renderdoc_cmp 根目录`。

当前桌面界面的 `环境设置` 已精简为与现有产品能力直接相关的项目；如果后续确实需要扩展或手工覆盖更高级的配置，可直接编辑绿色包内的 `user_data/config/settings.json`。

## 使用文档

- [用户使用指南](docs/USER_GUIDE.md)
- [截图清单与拍摄规范](docs/SCREENSHOT_CHECKLIST.md)

## 常见工作流

### 1. 单帧性能分析

1. 打开 `性能`
2. 选择单个 `.rdc`
3. 点击 `执行性能分析`
4. 使用排序维度和排序方向查看热点
5. 结合图表、日志和绘制预览定位高开销 Pass

### 2. 性能 Diff

1. 打开 `性能 Diff`
2. 选择两份待比较的 `.rdc`
3. 按需填写 `RenderDoc` / `Malioc` 路径和附加选项
4. 点击 `执行性能 Diff`
5. 在中间区域查看内嵌报告和运行日志

![性能 Diff 界面](docs/images/cmp-report.png)

### 3. 资产导出与 CSV 转模型

1. 打开 `资产批量导出`
2. 读取 Pass 列表并选择导出范围，或直接手动填写单个 `EID`
3. 按需启用 `FBX` / `OBJ`
4. 点击 `确认范围并准备批量映射`
5. 在批量映射确认窗口中检查样本映射，并开始导出
6. 如需手工转换 CSV，可在 `CSV 列映射预览` 中点击 `按当前映射开始批量转换`

关于“批量映射确认”的行为说明：

- 只要本次导出启用了 `FBX` 或 `OBJ`，正式导出前都会先弹出一次批量映射确认窗口。
- 如果当前选择的是 `Pass` 区间或手动 `EID` 范围，窗口也只会弹出一次，不会要求你对范围内的每个 Pass 逐个确认。
- 这个窗口会从当前导出范围里挑一个样本 draw / `EID`，展示自动识别出来的顶点列映射，方便你快速确认这一批数据的大致结构。
- 你在窗口里确认的映射，会作为这一次批量导出的统一优先覆盖。
- 真正执行导出时，工具仍会对每个 draw 的 CSV 单独做自动识别；如果某个 draw 不存在你手动指定的列，会自动回退到该 draw 的自动识别结果，不会再次弹窗。
- 如果本次不导出 `FBX` / `OBJ`，则不会出现这个批量映射确认步骤。

关于“CSV 列映射预览 / 手工 CSV 转换”的行为说明：

- 批量选择多个 CSV 或目录时，预览面板只会展示一个样本 CSV 的自动识别结果。
- 真正执行 `按当前映射开始批量转换` 时，工具会对每个 CSV 单独自动识别。
- 你在面板里手动指定的列，会作为这一批转换的统一优先覆盖。
- 如果某个 CSV 不存在你手动指定的列，工具会自动回退到该文件自己的自动识别结果，并在任务结果里记录回退说明。
- 手工 CSV 转换完成后，界面不再自动打开输出目录，避免额外的系统弹窗干扰当前操作。
- 导出任务摘要里会提供显式的 `打开输出目录` 按钮，便于你在需要时手动打开结果位置。

资产导出完成后，默认会在导出目录中生成以下内容：

- `csv/`：当前 draw 的 VS 输入导出结果
- `models/`：`FBX` / `OBJ` 模型文件
- `textures/`：当前 draw 绑定的贴图导出
- `shaders/`：当前 draw 的 shader 与参数文件

其中 `shaders/` 目录下会包含：

- `*_vs.glsl`：顶点阶段文本
- `*_fs.glsl`：片元阶段文本
- `*_shader_params.json`：常量块、资源绑定、参数值等反射信息

注意：

- 如果填写了“手动单个 EID”，它会优先于下拉框中的 Pass 选择。
- 下拉框中的同名材质或 marker 会按实际 draw 出现顺序拆成多项，例如同一材质先后出现在 `EID 291` 和 `EID 447`，会显示为两条独立项，避免误导性地合并到同一个 Pass。
- 某些抓帧里 RenderDoc 不一定暴露原始 `GLSL` 文本；此时工具仍会导出当前可用的最佳 shader 文本表示，文件名保持为 `*_vs.glsl` / `*_fs.glsl` 以便统一整理。

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
- [RenderDoc Comparison Tool](https://git.woa.com/xinhou/renderdoc_cmp/tree/v1.x/renderdoccmp)
