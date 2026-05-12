$exe = "G:\RenderdocDiffTools\RenderdocDiffPortable\RenderdocDiffTools.exe"
$env:RENDERDOC_PORTABLE_HEADLESS = "1"
$p = Start-Process -FilePath $exe -PassThru
Start-Sleep -Seconds 12

$ports = @(8010, 8011, 8012, 8013)
$foundPort = $null
foreach ($port in $ports) {
    try {
        $h = Invoke-RestMethod -Uri "http://127.0.0.1:$port/api/health" -TimeoutSec 3
        $foundPort = $port
        Write-Host "Health OK on port $port : $($h.status)"
        break
    } catch {
        # try next
    }
}

if (-not $foundPort) {
    Write-Host "Could not find service on any port"
    Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue
    exit 1
}

try {
    $t = Invoke-RestMethod -Uri "http://127.0.0.1:$foundPort/api/shader-compiler/tools" -TimeoutSec 5
    Write-Host "Shader compiler tools endpoint OK"
    $t | Format-List
} catch {
    Write-Host "Shader tools check failed: $($_.Exception.Message)"
}

Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue
Write-Host "Smoke test PASSED"
