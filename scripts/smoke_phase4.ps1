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
        Write-Host "Health OK on port $port"
        break
    } catch {}
}

if (-not $foundPort) {
    Write-Host "FAIL: Could not find service on any port"
    Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue
    exit 1
}

$base = "http://127.0.0.1:$foundPort"

# Test one-click convert endpoint
try {
    $body = @{
        glsl_source = @"
#version 310 es
precision highp float;
layout(location = 0) in vec2 texCoord0;
uniform sampler2D sam_diffuse;
layout(location = 0) out vec4 fragColor;
void main() {
    fragColor = texture(sam_diffuse, texCoord0);
}
"@
    }
    $r = Invoke-RestMethod -Uri "$base/api/oneclick-convert/run" -Method POST -Body $body -TimeoutSec 10
    if ($r.success) {
        Write-Host "One-click convert: PASS (success=true)"
    } else {
        Write-Host "One-click convert: FAIL (success=false, error=$($r.error))"
    }
} catch {
    Write-Host "One-click convert: FAIL ($($_.Exception.Message))"
}

# Test patterns endpoint
try {
    $r = Invoke-RestMethod -Uri "$base/api/oneclick-convert/patterns" -TimeoutSec 5
    Write-Host "Patterns endpoint: PASS (pattern_count=$($r.pattern_count))"
} catch {
    Write-Host "Patterns endpoint: FAIL ($($_.Exception.Message))"
}

Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue
Write-Host "Phase 4 smoke test PASSED"
