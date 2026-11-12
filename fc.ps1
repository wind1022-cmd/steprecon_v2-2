# Run a project script with FreeCAD's bundled Python (Windows).
#   .\fc.ps1 run.py part.step
#   .\fc.ps1 run_tests.py
# FreeCAD is searched in %LOCALAPPDATA%\Programs, %ProgramFiles%; override with $env:FREECAD_PYTHON.
param([Parameter(ValueFromRemainingArguments = $true)] [string[]] $Rest)

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
if (-not $py) {
    Write-Error "FreeCAD not found. Install it (winget install FreeCAD.FreeCAD) or set `$env:FREECAD_PYTHON to FreeCAD's bin\python.exe"
    exit 1
}
if ($Rest.Count -gt 0 -and -not (Test-Path $Rest[0]) -and (Test-Path (Join-Path $PSScriptRoot $Rest[0]))) {
    $Rest[0] = Join-Path $PSScriptRoot $Rest[0]      # allow calling from any directory
}
& $py @Rest
exit $LASTEXITCODE
