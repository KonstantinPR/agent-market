# Restart the agent_market dev server: stop any running instance, then start
# a fresh one with uvicorn --reload (backend edits apply automatically).
# ASCII-only on purpose: PowerShell 5.1 reads .ps1 as ANSI without a BOM.
$ErrorActionPreference = "SilentlyContinue"
$port = 8000
$appDir = "C:\python_projects\agent_market"
$py = "C:\python_projects\agent_market\venv\Scripts\python.exe"

Write-Host "Stopping agent_market server ..."
# --reload runs a supervisor + worker; stop both.
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
  Where-Object {
    $_.CommandLine -match 'app\.main:app' -or
    $_.CommandLine -match 'agent_market.+app.+main\.py'
  } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
# Fallback: free the port if something still listens.
Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue |
  ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue }
Start-Sleep -Milliseconds 800

Write-Host "Starting agent_market server on port $port (auto-reload) ..."
Start-Process -FilePath $py -WorkingDirectory $appDir -WindowStyle Hidden `
  -ArgumentList "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "$port", "--reload", "--reload-dir", "$appDir\app"

for ($i = 0; $i -lt 60; $i++) {
  if (Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue) {
    Write-Host "Server is up: http://127.0.0.1:$port/"
    exit 0
  }
  Start-Sleep -Seconds 1
}
Write-Host "Server did not start within 60s."
exit 1
