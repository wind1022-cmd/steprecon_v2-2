# One-time setup on a new Windows PC.
#   .\setup.ps1                  # check FreeCAD, install python deps into .\vendor, run the tests
#   .\setup.ps1 -InstallFreeCAD  # also install FreeCAD with winget if it is missing
#   .\setup.ps1 -SkipClaude      # local models only: do not install the 'anthropic' package
param([switch] $InstallFreeCAD, [switch] $SkipClaude)
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

function Find-FreeCADPython {
    if ($env:FREECAD_PYTHON -and (Test-Path $env:FREECAD_PYTHON)) { return $env:FREECAD_PYTHON }
    $roots = @("$env:LOCALAPPDATA\Programs", $env:ProgramFiles, ${env:ProgramFiles(x86)}) | Where-Object { $_ }
    foreach ($r in $roots) {
        $hit = Get-ChildItem -Path $r -Filter "FreeCAD*" -Directory -ErrorAction SilentlyContinue |
            Sort-Object Name -Descending |
            ForEach-Object { Join-Path $_.FullName "bin\python.exe" } |
            Where-Object { Test-Path $_ } | Select-Object -First 1
        if ($hit) { return $hit }
    }
    return $null
}

$py = Find-FreeCADPython
if (-not $py -and $InstallFreeCAD) {
    Write-Host "Installing FreeCAD with winget ..."
    winget install --id FreeCAD.FreeCAD -e --accept-package-agreements --accept-source-agreements
    $py = Find-FreeCADPython
}
if (-not $py) {
    Write-Host "FreeCAD 1.x not found. Re-run with -InstallFreeCAD, or install it from https://www.freecad.org" -ForegroundColor Red
    exit 1
}
Write-Host "FreeCAD python: $py"
& $py -c "import sys; print('python', sys.version.split()[0])"

if (-not $SkipClaude) {
    Write-Host "Installing python packages into .\vendor ..."
    & $py -m pip install --quiet --target (Join-Path $PSScriptRoot "vendor") -r requirements.txt
}

Write-Host "Running the test suite (no API key, no GPU, no dataset needed) ..."
& $py run_tests.py
if ($LASTEXITCODE -ne 0) { Write-Host "Some tests failed - see output above." -ForegroundColor Red; exit 1 }

Write-Host @"

Setup OK. Next:
  Claude API :  `$env:ANTHROPIC_API_KEY = 'sk-ant-...'
                .\fc.ps1 run.py part.step
  Local model:  ollama pull qwen3:8b   (set OLLAMA_CONTEXT_LENGTH=16384 first, see README.md)
                .\fc.ps1 run.py part.step --backend openai --model qwen3:8b
"@
