# Database backup: pg_dump (custom format) using credentials from .env.
# Usage:  powershell -ExecutionPolicy Bypass -File scripts\backup_db.ps1
#         [-OutDir C:\backup]  target folder       (default C:\backup)
#         [-Keep 14]           keep N newest dumps (rotation)
# Every agent that needs a dump runs this one script instead of guessing
# credentials. The dump is verified non-empty; on any failure exit code is 1.

param(
    [string]$OutDir = 'C:\backup',
    [int]$Keep = 14
)

$ErrorActionPreference = 'Stop'
$ProjectDir = Split-Path -Parent $PSScriptRoot
$pgBin = 'C:\Program Files\PostgreSQL\14\bin'
$envFile = Join-Path $ProjectDir '.env'

function Get-EnvValue($key) {
    foreach ($line in [System.IO.File]::ReadAllLines($envFile, (New-Object System.Text.UTF8Encoding $false))) {
        $t = $line.Trim()
        if ($t -and $t[0] -ne '#' -and $t -match "^$key=(.*)$") {
            return $matches[1].Trim()
        }
    }
    return $null
}

# ====== Preconditions ======
if (-not (Test-Path $envFile)) { throw ".env not found: $envFile" }
$pgDump = Join-Path $pgBin 'pg_dump.exe'
if (-not (Test-Path $pgDump)) { throw "pg_dump not found: $pgDump" }

$user = Get-EnvValue 'PG_USER'
$pass = Get-EnvValue 'PG_PASSWORD'
$pgHost = Get-EnvValue 'PG_HOST'
$port = Get-EnvValue 'PG_PORT'
$db = Get-EnvValue 'PG_DATABASE'
if (-not $user -or -not $pass) { throw 'PG_USER/PG_PASSWORD are not set in .env' }
if (-not $pgHost) { $pgHost = 'localhost' }
if (-not $port) { $port = '5432' }
if (-not $db) { $db = 'agent_market' }

if (-not (Test-Path $OutDir)) { New-Item -ItemType Directory -Path $OutDir | Out-Null }

# ====== Dump ======
$ts = Get-Date -Format 'yyyyMMdd_HHmmss'
$out = Join-Path $OutDir ("agent_market_$ts.dump")
$env:PGPASSWORD = $pass
& $pgDump -h $pgHost -p $port -U $user -w -F c -f $out $db 2>&1 | ForEach-Object { Write-Output $_ }
$rc = $LASTEXITCODE
Remove-Item Env:\PGPASSWORD -ErrorAction SilentlyContinue

if ($rc -ne 0) { Write-Error "pg_dump failed (exit $rc), no backup written"; exit $rc }
$size = (Get-Item $out).Length
if ($size -eq 0) { Remove-Item $out -Force -ErrorAction SilentlyContinue; throw "pg_dump produced an empty file: $out" }

Write-Output "OK $out ($([math]::Round($size / 1KB, 1)) KB)"

# ====== Rotation: keep $Keep newest agent_market_*.dump ======
Get-ChildItem -LiteralPath $OutDir -Filter 'agent_market_*.dump' -File -ErrorAction SilentlyContinue |
    Sort-Object LastWriteTime -Descending |
    Select-Object -Skip $Keep |
    Remove-Item -Force -ErrorAction SilentlyContinue