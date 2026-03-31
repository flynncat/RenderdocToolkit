param(
    [string]$OutputRoot = "G:\RenderdocDiffTools",
    [switch]$SkipSmokeTest
)

$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$DistRoot = Join-Path $ProjectRoot "dist"
$BuildRoot = Join-Path $ProjectRoot "build"
$SpecPath = Join-Path $ProjectRoot "RenderdocDiffTools.spec"
$PortableDir = Join-Path $OutputRoot "RenderdocDiffPortable"
$SourceSettingsPath = Join-Path $ProjectRoot "config\settings.json"
$UserSettingsPath = Join-Path $ProjectRoot "user_data\config\settings.json"

function Read-SettingsFile([string]$Path) {
    if (-not (Test-Path $Path)) {
        return $null
    }
    try {
        return Get-Content $Path -Raw -Encoding UTF8 | ConvertFrom-Json
    } catch {
        return $null
    }
}

function Get-FirstNonEmptyValue([object[]]$Values) {
    foreach ($Value in $Values) {
        if ($null -eq $Value) {
            continue
        }
        $Text = [string]$Value
        if (-not [string]::IsNullOrWhiteSpace($Text)) {
            return $Text.Trim()
        }
    }
    return ""
}

function Resolve-ExistingPath([object[]]$Candidates) {
    foreach ($Candidate in $Candidates) {
        $Text = Get-FirstNonEmptyValue @($Candidate)
        if ($Text -and (Test-Path $Text)) {
            try {
                return (Resolve-Path $Text).Path
            } catch {
                return $Text
            }
        }
    }
    return ""
}

Write-Host "== Build RenderdocDiffTools portable ==" -ForegroundColor Cyan

if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    throw "Python not found."
}

New-Item -ItemType Directory -Force -Path $OutputRoot | Out-Null

$SourceSettings = Read-SettingsFile $SourceSettingsPath
$UserSettings = Read-SettingsFile $UserSettingsPath

$PortableExe = Join-Path $PortableDir "RenderdocDiffTools.exe"
$PortableProcesses = @(Get-CimInstance Win32_Process -Filter "Name = 'RenderdocDiffTools.exe'" |
    Where-Object { $_.ExecutablePath -and $_.ExecutablePath -like "$PortableDir*" } |
    ForEach-Object {
        Write-Host "Stopping running portable instance: $($_.ProcessId)" -ForegroundColor Yellow
        Stop-Process -Id $_.ProcessId -Force
        $_
    })

if ($PortableProcesses.Count -gt 0) {
    Start-Sleep -Seconds 2
}

if (Test-Path $DistRoot) {
    Remove-Item $DistRoot -Recurse -Force
}
if (Test-Path $BuildRoot) {
    Remove-Item $BuildRoot -Recurse -Force
}
if (Test-Path $PortableDir) {
    $Removed = $false
    for ($Attempt = 1; $Attempt -le 5; $Attempt++) {
        try {
            Remove-Item $PortableDir -Recurse -Force
            $Removed = $true
            break
        } catch {
            if ($Attempt -eq 5) {
                throw
            }
            Write-Host "Portable dir is still busy, retry $Attempt/5..." -ForegroundColor Yellow
            Start-Sleep -Seconds 2
        }
    }
}

python -m PyInstaller --noconfirm $SpecPath

$BuiltDir = Join-Path $DistRoot "RenderdocDiffTools"
if (-not (Test-Path $BuiltDir)) {
    throw "PyInstaller output folder not found: $BuiltDir"
}

Copy-Item $BuiltDir $PortableDir -Recurse -Force

$UserDataConfigDir = Join-Path $PortableDir "user_data\config"
New-Item -ItemType Directory -Force -Path $UserDataConfigDir | Out-Null

$SourceCmpRoot = ""
if ($null -ne $SourceSettings) {
    $SourceCmpRoot = [string]$SourceSettings.renderdoc_cmp_root
}
$UserCmpRoot = ""
if ($null -ne $UserSettings) {
    $UserCmpRoot = [string]$UserSettings.renderdoc_cmp_root
}
$SourceRenderdocPythonPath = ""
if ($null -ne $SourceSettings) {
    $SourceRenderdocPythonPath = [string]$SourceSettings.renderdoc_python_path
}
$UserRenderdocPythonPath = ""
if ($null -ne $UserSettings) {
    $UserRenderdocPythonPath = [string]$UserSettings.renderdoc_python_path
}

$DetectedCmpRoot = Resolve-ExistingPath @(
    $env:RENDERDOC_WEBUI_CMP_ROOT,
    $SourceCmpRoot,
    $UserCmpRoot,
    "G:\UGit\renderdoc_cmp\renderdoccmp",
    "G:\UGit\renderdoc_cmp",
    "D:\UGit\renderdoc_cmp\renderdoccmp",
    "D:\UGit\renderdoc_cmp",
    "C:\UGit\renderdoc_cmp\renderdoccmp",
    "C:\UGit\renderdoc_cmp"
)
$DetectedRenderdocPythonPath = Get-FirstNonEmptyValue @(
    $env:RENDERDOC_PYTHON_PATH,
    $SourceRenderdocPythonPath,
    $UserRenderdocPythonPath
)

$Settings = @{
    host = "127.0.0.1"
    port = 8010
    llm_provider = if ($env:RENDERDOC_WEBUI_LLM_PROVIDER) { $env:RENDERDOC_WEBUI_LLM_PROVIDER } else { "local" }
    openai_base_url = if ($env:RENDERDOC_WEBUI_OPENAI_BASE_URL) { $env:RENDERDOC_WEBUI_OPENAI_BASE_URL } else { "" }
    openai_api_key = if ($env:RENDERDOC_WEBUI_OPENAI_API_KEY) { $env:RENDERDOC_WEBUI_OPENAI_API_KEY } else { "" }
    openai_model = if ($env:RENDERDOC_WEBUI_OPENAI_MODEL) { $env:RENDERDOC_WEBUI_OPENAI_MODEL } else { "" }
    openai_timeout_seconds = if ($env:RENDERDOC_WEBUI_OPENAI_TIMEOUT_SECONDS) { [double]$env:RENDERDOC_WEBUI_OPENAI_TIMEOUT_SECONDS } else { 60 }
    llm_max_context_chars = 24000
    renderdoc_python_path = $DetectedRenderdocPythonPath
    renderdoc_cmp_root = $DetectedCmpRoot
    setup_completed = $true
}

$Settings | ConvertTo-Json -Depth 5 | Set-Content -Path (Join-Path $UserDataConfigDir "settings.json") -Encoding UTF8

$ReadmeLines = @(
    "RenderdocDiffTools portable package",
    "",
    "Start:",
    "1. Double-click RenderdocDiffTools.exe",
    "2. The app will start an embedded desktop window",
    "",
    "Data folders:",
    "- user_data\config\settings.json",
    "- user_data\sessions\",
    "- user_data\logs\",
    "",
    "Notes:",
    "- settings.json is pre-generated and can be edited directly",
    "- Install RenderDoc first if the target machine does not have it",
    "- The desktop window still hosts a local loopback service internally, but it no longer depends on an external browser"
)
$Readme = [string]::Join([Environment]::NewLine, $ReadmeLines)

Set-Content -Path (Join-Path $PortableDir "README_PORTABLE.txt") -Value $Readme -Encoding UTF8

if (-not $SkipSmokeTest) {
    $SmokeTestScript = Join-Path $ProjectRoot "smoke_test_packaged.py"
    if (-not (Test-Path $SmokeTestScript)) {
        throw "Smoke test script not found: $SmokeTestScript"
    }
    Write-Host "== Run packaged regression ==" -ForegroundColor Cyan
    python $SmokeTestScript
    if ($LASTEXITCODE -ne 0) {
        throw "Packaged regression failed."
    }
}

Write-Host "Portable build created at: $PortableDir" -ForegroundColor Green
