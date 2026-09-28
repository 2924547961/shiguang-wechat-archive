$ErrorActionPreference = 'Stop'
$project = Split-Path -Parent $MyInvocation.MyCommand.Path
$localPython = Join-Path $project '.venv\Scripts\python.exe'
$condaPython = if ($env:CONDA_PREFIX) { Join-Path $env:CONDA_PREFIX 'python.exe' } else { '' }
if (Test-Path -LiteralPath $localPython) { $python = $localPython }
elseif ($condaPython -and (Test-Path -LiteralPath $condaPython)) { $python = $condaPython }
else { $python = (Get-Command python.exe -ErrorAction Stop).Source }
& $python -m PyInstaller --noconfirm --clean --distpath (Join-Path $project 'dist') --workpath (Join-Path $project 'build') (Join-Path $project '拾光.spec')
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed with exit code $LASTEXITCODE" }
Write-Output (Join-Path $project 'dist\拾光-轻量版.exe')
