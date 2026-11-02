$ErrorActionPreference = 'Stop'
$project = $PSScriptRoot
$buildRoot = Join-Path $project 'work\build'
New-Item -ItemType Directory -Path (Join-Path $buildRoot 'temp') -Force | Out-Null
$env:TEMP = Join-Path $buildRoot 'temp'
$env:TMP = $env:TEMP
$env:PYTHONDONTWRITEBYTECODE = '1'
$target = Join-Path $project 'ZONKRIK Handycam Recovery.exe'
$running = Get-CimInstance Win32_Process | Where-Object { $_.ExecutablePath -eq $target }
if ($running) { throw 'Close ZONKRIK Handycam Recovery before rebuilding the executable.' }
python -m PyInstaller --noconfirm --onedir --windowed --contents-directory runtime --name 'ZONKRIK Handycam Recovery' --distpath (Join-Path $buildRoot 'package') --workpath (Join-Path $buildRoot 'cache') --specpath $buildRoot (Join-Path $project 'app.py')
if ($LASTEXITCODE -ne 0) { throw 'PyInstaller build failed.' }
$package = Join-Path $buildRoot 'package\ZONKRIK Handycam Recovery'
Get-ChildItem -LiteralPath (Join-Path $package 'runtime') | ForEach-Object { Copy-Item -LiteralPath $_.FullName -Destination (Join-Path $project 'runtime') -Recurse -Force }
Copy-Item -LiteralPath (Join-Path $package 'ZONKRIK Handycam Recovery.exe') -Destination $target -Force
Write-Output "Built app: $target"
