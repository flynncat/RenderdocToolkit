param(
    [string]$RenderDocPythonPath = "C:\Program Files\RenderDoc",
    [switch]$EnableVulkanCapture
)

$ErrorActionPreference = "Stop"

Write-Host "== RenderDoc Compare 环境初始化 ==" -ForegroundColor Cyan

if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    throw "未检测到 python，请先安装 Python 3.10+ 并加入 PATH。"
}

Write-Host "[1/4] 安装 rdc-cli..." -ForegroundColor Yellow
python -m pip install --upgrade pip | Out-Host
python -m pip install rdc-cli | Out-Host

Write-Host "[2/4] 设置 RENDERDOC_PYTHON_PATH..." -ForegroundColor Yellow
[Environment]::SetEnvironmentVariable("RENDERDOC_PYTHON_PATH", $RenderDocPythonPath, "User")
$env:RENDERDOC_PYTHON_PATH = $RenderDocPythonPath
Write-Host "RENDERDOC_PYTHON_PATH=$RenderDocPythonPath"

if ($EnableVulkanCapture) {
    Write-Host "[3/4] 启用 Vulkan capture 环境变量..." -ForegroundColor Yellow
    [Environment]::SetEnvironmentVariable("ENABLE_VULKAN_RENDERDOC_CAPTURE", "1", "User")
    $env:ENABLE_VULKAN_RENDERDOC_CAPTURE = "1"
    Write-Host "ENABLE_VULKAN_RENDERDOC_CAPTURE=1"
} else {
    Write-Host "[3/4] 跳过 Vulkan capture 环境变量（未指定 -EnableVulkanCapture）" -ForegroundColor DarkYellow
}

Write-Host "[4/4] 运行 rdc doctor..." -ForegroundColor Yellow
if (Get-Command rdc -ErrorAction SilentlyContinue) {
    rdc doctor | Out-Host
} else {
    Write-Warning "rdc 命令未在当前会话可见。请重开终端后执行：rdc doctor"
}

Write-Host "初始化完成。若刚设置环境变量，请重启终端后再运行分析脚本。" -ForegroundColor Green
