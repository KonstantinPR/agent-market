# Creates Postgres user agent_user and database agent_market (Windows, PG14).
# Requires admin rights (single UAC). Uses temporary trust, then restores config.

$ErrorActionPreference = 'Stop'
$ProjectDir = 'C:\python_projects\agent_market'
$pgBin = 'C:\Program Files\PostgreSQL\14\bin'
$dataDir = 'C:\Program Files\PostgreSQL\14\data'
$hba = "$dataDir\pg_hba.conf"
$backup = "$dataDir\pg_hba.conf.bak_agent"
$resultFile = "$ProjectDir\pg_setup_result.txt"
$serviceName = 'postgresql-x64-14'
$newUser = 'agent_user'
$newDb = 'agent_market'

function Write-Result($msg) {
    [System.IO.File]::WriteAllText($resultFile, $msg, (New-Object System.Text.UTF8Encoding $false))
}

function Switch-HbaAuth {
    param($from)
    $lines = [System.IO.File]::ReadAllLines($hba)
    $out = New-Object System.Collections.Generic.List[string]
    $changed = $false
    foreach ($line in $lines) {
        $new = $line -replace "^(host\s+all\s+all\s+(127\.0\.0\.1/32|::1/128)\s+)$from$", "`$1trust"
        if ($new -ne $line) { $changed = $true }
        $out.Add($new)
    }
    if (-not $changed) { throw 'pg_hba.conf: target host-methods not found, nothing changed' }
    [System.IO.File]::WriteAllLines($hba, $out, (New-Object System.Text.UTF8Encoding $false))
}

function Test-Trust {
    & "$pgBin\psql.exe" -U postgres -h 127.0.0.1 -p 5432 -d postgres -w -tAc "SELECT 'CONNECTED'"
}

# ====== Elevation check ======
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    try {
        $p = Start-Process powershell -Verb RunAs -Wait -PassThru -ArgumentList @(
            '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', "`"$PSCommandPath`""
        )
    } catch {
        Write-Output ('UAC denied: ' + $_.Exception.Message)
        exit 1
    }
    if (-not (Test-Path $resultFile)) {
        Write-Output 'Elevation failed, result file missing.'
        exit 1
    }
    Get-Content $resultFile
    Remove-Item $resultFile -Force
    exit 0
}

try {
    # 1. Backup pg_hba.conf
    if (-not (Test-Path $backup)) {
        Copy-Item $hba $backup
    }
    if (-not (Test-Path $backup)) { throw 'Could not create pg_hba.conf backup' }

    # 2. trust for local host connections
    Switch-HbaAuth -from 'scram-sha-256'

    # 3. Restart service, verify trust works (with retries while server starts)
    Restart-Service $serviceName -Force
    $c = $null
    for ($i = 0; $i -lt 15; $i++) {
        Start-Sleep -Seconds 1
        $c = Test-Trust
        if ($LASTEXITCODE -eq 0 -and $c -eq 'CONNECTED') { break }
    }
    if ($c -ne 'CONNECTED') { throw 'Trust connection test failed' }

    $pass = -join ((48..57) + (65..90) + (97..122) | Get-Random -Count 20 | ForEach-Object { [char]$_ })
    $psql = "$pgBin\psql.exe"

    # 4. Create role and database (never prompts for password due to -w)
    & $psql -U postgres -h 127.0.0.1 -p 5432 -d postgres -w -v ON_ERROR_STOP=1 -c "CREATE ROLE $newUser LOGIN PASSWORD '$pass' CREATEDB;" 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'CREATE ROLE failed' }
    & $psql -U postgres -h 127.0.0.1 -p 5432 -d postgres -w -v ON_ERROR_STOP=1 -c "CREATE DATABASE $newDb OWNER $newUser;" 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'CREATE DATABASE failed' }
    & $psql -U postgres -h 127.0.0.1 -p 5432 -d postgres -w -v ON_ERROR_STOP=1 -c "ALTER DATABASE $newDb OWNER TO $newUser;" 2>&1 | Out-Null

    # 5. Restore pg_hba.conf
    Copy-Item $backup $hba -Force
    Remove-Item $backup -Force
    Restart-Service $serviceName -Force
    Start-Sleep -Seconds 3

    # 6. Write credentials into .env
    $envPath = "$ProjectDir\.env"
    $envLines = [System.IO.File]::ReadAllLines($envPath)
    $out = New-Object System.Collections.Generic.List[string]
    foreach ($line in $envLines) {
        $l = $line
        if ($l -match '^PG_USER=') { $l = "PG_USER=$newUser" }
        elseif ($l -match '^PG_PASSWORD=') { $l = "PG_PASSWORD=$pass" }
        elseif ($l -match '^PG_DATABASE=') { $l = "PG_DATABASE=$newDb" }
        $out.Add($l)
    }
    [System.IO.File]::WriteAllLines($envPath, $out, (New-Object System.Text.UTF8Encoding $false))

    Write-Result "OK`nPG_USER=$newUser`nPG_PASSWORD=$pass`nPG_DATABASE=$newDb"
    Write-Output "Done. User: $newUser, DB: $newDb"
} catch {
    if (Test-Path $backup) { try { Copy-Item $backup $hba -Force } catch { } }
    try { Restart-Service $serviceName -Force } catch { }
    try { Start-Sleep -Seconds 2 } catch { }
    Write-Result "ERROR`n$($_.Exception.Message)"
    throw
}