[CmdletBinding()]
param(
    [switch]$KeepOnFailure,
    [switch]$SkipBrowserInstall
)

$ErrorActionPreference = "Stop"
if ($env:DISPOSABLE_E2E_CONFIRMED -ne "1") {
    throw "Refusing to start: set DISPOSABLE_E2E_CONFIRMED=1 for this disposable run"
}

$RepositoryRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$PrototypeRoot = Join-Path $RepositoryRoot "frontend"
$ComposeBase = Join-Path $RepositoryRoot "deploy\compose.yaml"
$ComposeFinal = Join-Path $RepositoryRoot "deploy\e2e\compose-final.yaml"
$BrowserRunner = Join-Path $RepositoryRoot "tests\e2e\f01-f06.cjs"
$RunToken = [Guid]::NewGuid().ToString("N").Substring(0, 12)
$ProjectName = "ux09-final-e2e-$RunToken"
if ($ProjectName -notmatch '^ux09-final-e2e-[0-9a-f]{12}$' -or $ProjectName -match '(?i)prod|production|staging|shared') {
    throw "Refusing production-like or non-disposable Compose project name"
}

$ViteProcess = $null
$Succeeded = $false
$Failure = $null

function New-FreeTcpPort {
    $listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, 0)
    $listener.Start()
    try { return ([System.Net.IPEndPoint]$listener.LocalEndpoint).Port }
    finally { $listener.Stop() }
}

function New-Base64UrlKey {
    $bytes = New-Object byte[] 32
    [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    return [Convert]::ToBase64String($bytes).Replace("+", "-").Replace("/", "_")
}

function Invoke-Compose {
    & docker compose -p $ProjectName -f $ComposeBase -f $ComposeFinal @args
    if ($LASTEXITCODE -ne 0) { throw "docker compose failed with exit code $LASTEXITCODE" }
}

function Wait-HttpOk([string]$Uri, [int]$TimeoutSeconds = 120) {
    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    $lastMessage = "not attempted"
    while ([DateTime]::UtcNow -lt $deadline) {
        try {
            $response = Invoke-WebRequest -UseBasicParsing -Uri $Uri -TimeoutSec 3
            if ($response.StatusCode -ge 200 -and $response.StatusCode -lt 300) { return }
            $lastMessage = "HTTP $($response.StatusCode)"
        } catch { $lastMessage = $_.Exception.Message }
        Start-Sleep -Milliseconds 250
    }
    throw "Timed out waiting for $Uri ($lastMessage)"
}

try {
    & docker info --format "{{.ServerVersion}}" | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Docker Desktop server is unavailable" }
    if (-not (Get-Command node -ErrorAction SilentlyContinue)) { throw "node is required" }
    if (-not (Get-Command npm.cmd -ErrorAction SilentlyContinue)) { throw "npm.cmd is required" }

    $ApiPort = New-FreeTcpPort
    do { $WebPort = New-FreeTcpPort } while ($WebPort -eq $ApiPort)
    $ArtifactDir = Join-Path $RepositoryRoot ".tmp\e2e-artifacts\$ProjectName"
    $ProfilePath = Join-Path ([System.IO.Path]::GetTempPath()) "$ProjectName-browser"
    $ViteOut = Join-Path ([System.IO.Path]::GetTempPath()) "$ProjectName-vite.out.log"
    $ViteErr = Join-Path ([System.IO.Path]::GetTempPath()) "$ProjectName-vite.err.log"

    $env:E2E_API_PORT = [string]$ApiPort
    $env:E2E_APP_IMAGE = "$ProjectName-app"
    $env:DEFAULT_ORGANIZATION_SLUG = "final-e2e"
    $env:DEFAULT_ORGANIZATION_NAME = "Final E2E Recruiting"
    $env:E2E_ADMIN_EMAIL = "admin-$RunToken@example.test"
    $env:E2E_ADMIN_PASSWORD = "Admin-$([Guid]::NewGuid().ToString('N'))"
    $env:E2E_INTERVIEWER_EMAIL = "interviewer-$RunToken@example.test"
    $env:E2E_INTERVIEWER_PASSWORD = "Interviewer-$([Guid]::NewGuid().ToString('N'))"
    $env:E2E_UNASSIGNED_INTERVIEWER_EMAIL = "unassigned-$RunToken@example.test"
    $env:E2E_UNASSIGNED_INTERVIEWER_PASSWORD = "Unassigned-$([Guid]::NewGuid().ToString('N'))"
    $env:E2E_RECRUITER_EMAIL = "recruiter-$RunToken@example.test"
    $env:E2E_RECRUITER_PASSWORD = "Recruiter-$([Guid]::NewGuid().ToString('N'))"
    $env:E2E_JOB_TITLE = "Final E2E Source $RunToken"
    $env:E2E_PROJECT_NAME = $ProjectName
    $env:E2E_BROWSER_PROFILE = $ProfilePath
    $env:E2E_ARTIFACT_DIR = $ArtifactDir
    $env:E2E_BASE_URL = "http://localhost:$ApiPort"
    $env:E2E_WEB_URL = "http://localhost:$WebPort"
    $env:POSTGRES_DB = "ux09finale2e"
    $env:POSTGRES_USER = "ux09finale2e"
    $env:POSTGRES_PASSWORD = "db-$([Guid]::NewGuid().ToString('N'))"
    $env:APP_DB_USER = "app_$RunToken"
    $env:APP_DB_PASSWORD = "app-db-$([Guid]::NewGuid().ToString('N'))"
    $env:GOVERNANCE_DB_USER = "gov_$RunToken"
    $env:GOVERNANCE_DB_PASSWORD = "gov-db-$([Guid]::NewGuid().ToString('N'))"
    $env:MINIO_ROOT_USER = "minio$RunToken"
    $env:MINIO_ROOT_PASSWORD = "minio-$([Guid]::NewGuid().ToString('N'))"
    $env:APP_OBJECT_STORAGE_ACCESS_KEY = "app$RunToken"
    $env:APP_OBJECT_STORAGE_SECRET_KEY = "app-minio-$([Guid]::NewGuid().ToString('N'))"
    $env:GOVERNANCE_DELETE_ACCESS_KEY = "delete$RunToken"
    $env:GOVERNANCE_DELETE_SECRET_KEY = "delete-minio-$([Guid]::NewGuid().ToString('N'))"
    $env:GOVERNANCE_LEDGER_ACCESS_KEY = "ledger$RunToken"
    $env:GOVERNANCE_LEDGER_SECRET_KEY = "ledger-minio-$([Guid]::NewGuid().ToString('N'))"
    $env:GOVERNANCE_LEDGER_SIGNING_KEY = New-Base64UrlKey
    $env:OBJECT_STORAGE_BUCKET = "resumes-$RunToken"
    $env:CONTACT_ENCRYPTION_KEY = New-Base64UrlKey
    $env:CONTACT_LOOKUP_SECRET = [Guid]::NewGuid().ToString("N")
    $env:LLM_CONFIG_ENCRYPTION_KEY = New-Base64UrlKey
    $env:FEISHU_CONFIG_ENCRYPTION_KEY = New-Base64UrlKey
    $env:LLM_PROVIDER_ALLOWLIST_JSON = "{}"
    $env:CORS_ORIGINS = "[`"http://localhost:$WebPort`"]"
    $env:VITE_API_PROXY_TARGET = "http://localhost:$ApiPort"
    $env:NODE_PATH = Join-Path $PrototypeRoot "node_modules"

    Write-Host "[final-e2e] project=$ProjectName artifacts=$ArtifactDir"
    Invoke-Compose config --quiet
    Invoke-Compose up --build --abort-on-container-exit --exit-code-from e2e-final-prepare e2e-final-prepare
    Invoke-Compose exec -T postgres sh /docker-entrypoint-initdb.d/10-provision-app-role.sh
    Invoke-Compose up -d api worker proxy
    Wait-HttpOk "$($env:E2E_BASE_URL)/health/ready"

    Push-Location $PrototypeRoot
    try {
        if (-not (Test-Path (Join-Path $PrototypeRoot "node_modules\playwright"))) {
            & npm.cmd ci
            if ($LASTEXITCODE -ne 0) { throw "npm ci failed with exit code $LASTEXITCODE" }
        }
        if (-not $SkipBrowserInstall) {
            & npm.cmd exec -- playwright install chromium
            if ($LASTEXITCODE -ne 0) { throw "Playwright Chromium installation failed with exit code $LASTEXITCODE" }
        }
        $ViteProcess = Start-Process -FilePath (Get-Command npm.cmd).Source -ArgumentList @("run", "dev", "--", "--host", "127.0.0.1", "--port", [string]$WebPort, "--strictPort") -WorkingDirectory $PrototypeRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput $ViteOut -RedirectStandardError $ViteErr
    } finally { Pop-Location }

    Wait-HttpOk $env:E2E_WEB_URL
    & node $BrowserRunner
    if ($LASTEXITCODE -ne 0) { throw "Playwright F-01 through F-06 gate was incomplete (exit $LASTEXITCODE)" }
    $Succeeded = $true
    Write-Host "[final-e2e] PASS"
} catch {
    $Failure = $_
    Write-Host "[final-e2e] INCOMPLETE: $($_.Exception.Message)" -ForegroundColor Red
    try { Invoke-Compose ps } catch { Write-Host "[final-e2e] compose ps unavailable" }
    try { Invoke-Compose logs --no-color --tail 100 e2e-final-prepare api worker } catch { Write-Host "[final-e2e] service logs unavailable" }
} finally {
    if ($ViteProcess -and -not $ViteProcess.HasExited) { Stop-Process -Id $ViteProcess.Id -Force -ErrorAction SilentlyContinue }
    if ($Succeeded -or -not $KeepOnFailure) {
        try { Invoke-Compose down --volumes --remove-orphans } catch { Write-Host "[final-e2e] cleanup failed" }
        if ($env:E2E_APP_IMAGE) {
            $previousPreference = $ErrorActionPreference
            $ErrorActionPreference = "SilentlyContinue"
            & docker image inspect $env:E2E_APP_IMAGE 2>$null | Out-Null
            if ($LASTEXITCODE -eq 0) { & docker image rm $env:E2E_APP_IMAGE 2>$null | Out-Null }
            $ErrorActionPreference = $previousPreference
        }
        Remove-Item -LiteralPath $ProfilePath -Recurse -Force -ErrorAction SilentlyContinue
    } else {
        Write-Host "[final-e2e] isolated resources retained by -KeepOnFailure; project=$ProjectName"
    }
}

if (-not $Succeeded) { throw $Failure }
