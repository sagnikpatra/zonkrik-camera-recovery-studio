$ErrorActionPreference = 'Stop'
$base = $PSScriptRoot
$exe = Join-Path $base 'ZONKRIK Handycam Recovery.exe'
if (Test-Path -LiteralPath $exe) {
    Start-Process -FilePath $exe -WorkingDirectory $base
    exit
}
$python = Get-Command pythonw.exe -ErrorAction SilentlyContinue
if (-not $python) { throw 'The packaged executable is missing. Rebuild with build.ps1 or install Python with Tkinter.' }
$env:PYTHONDONTWRITEBYTECODE = '1'
Start-Process -FilePath $python.Source -ArgumentList ('"' + (Join-Path $base 'app.py') + '"') -WorkingDirectory $base -WindowStyle Hidden
