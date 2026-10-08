$ErrorActionPreference = 'Stop'
$recallRoot = Split-Path -Parent $PSScriptRoot
$recallPython = Join-Path $recallRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $recallPython)) {
    throw 'Run uv sync --locked from the repository first.'
}
$recallData = Join-Path $recallRoot 'data'
New-Item -ItemType Directory -Force -Path $recallData | Out-Null
$recallToken = Join-Path $recallData 'mcp.token'
if (-not (Test-Path -LiteralPath $recallToken)) {
    & $recallPython -c 'import pathlib,secrets,sys; pathlib.Path(sys.argv[1]).write_text(secrets.token_urlsafe(32), encoding="utf-8")' $recallToken
    if ($LASTEXITCODE -ne 0) { throw 'Token creation failed.' }
}
$env:HA_RECALL_TOKEN_FILE = $recallToken
$env:HA_RECALL_DB = Join-Path $recallData 'memory.sqlite3'
$env:HA_RECALL_HOST = '127.0.0.1'
Push-Location $recallRoot
try { & $recallPython -m ha_recall.server }
finally { Pop-Location }
