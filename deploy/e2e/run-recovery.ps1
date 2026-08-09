[CmdletBinding()]
param(
    [switch]$KeepOnFailure,
    [switch]$SkipBrowserInstall
)

$ErrorActionPreference = "Stop"
$RepositoryRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$PrototypeRoot = Join-Path $RepositoryRoot "frontend"
$ComposeBase = Join-Path $RepositoryRoot "deploy\compose.yaml"
$ComposeOverride = Join-Path $RepositoryRoot "deploy\e2e\compose.yaml"
$RecoveryScript = Join-Path $RepositoryRoot "tests\e2e\recovery.cjs"
$RunToken = [Guid]::NewGuid().ToString("N").Substring(0, 12)
$ProjectName = "ux09-e2e-$RunToken"
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
    & docker compose -p $ProjectName -f $ComposeBase -f $ComposeOverride @args
    if ($LASTEXITCODE -ne 0) { throw "docker compose failed with exit code $LASTEXITCODE" }
}

function Wait-HttpOk([string]$Uri, [int]$TimeoutSeconds = 90) {
    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    $lastMessage = "not attempted"
    while ([DateTime]::UtcNow -lt $deadline) {
        try {
            $response = Invoke-WebRequest -UseBasicParsing -Uri $Uri -TimeoutSec 3
            if ($response.StatusCode -ge 200 -and $response.StatusCode -lt 300) { return }
            $lastMessage = "HTTP $($response.StatusCode)"
        } catch {
            $lastMessage = $_.Exception.Message
        }
        Start-Sleep -Seconds 1
    }
    throw "Timed out waiting for $Uri ($lastMessage)"
}

try {
    Write-Host "[phase3-e2e] project=$ProjectName"
    & docker info --format "{{.ServerVersion}}" | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Docker Desktop server is unavailable" }
    if (-not (Get-Command node -ErrorAction SilentlyContinue)) { throw "node is required" }
    if (-not (Get-Command npm.cmd -ErrorAction SilentlyContinue)) { throw "npm.cmd is required" }

    $ApiPort = New-FreeTcpPort
    do { $WebPort = New-FreeTcpPort } while ($WebPort -eq $ApiPort)
    $ProfilePath = Join-Path ([System.IO.Path]::GetTempPath()) "$ProjectName-browser"
    $ViteOut = Join-Path ([System.IO.Path]::GetTempPath()) "$ProjectName-vite.out.log"
    $ViteErr = Join-Path ([System.IO.Path]::GetTempPath()) "$ProjectName-vite.err.log"

    $env:E2E_API_PORT = [string]$ApiPort
    $env:E2E_APP_IMAGE = "$ProjectName-app"
    $env:E2E_ADMIN_EMAIL = "$RunToken@example.test"
    $env:E2E_ADMIN_PASSWORD = "E2e-$([Guid]::NewGuid().ToString('N'))"
    $env:E2E_JOB_TITLE = "Phase 3 Recovery Engineer"
    $env:E2E_PROJECT_NAME = $ProjectName
    $env:E2E_COMPOSE_BASE = $ComposeBase
    $env:E2E_COMPOSE_OVERRIDE = $ComposeOverride
    $env:E2E_BROWSER_PROFILE = $ProfilePath
    $env:E2E_BASE_URL = "http://localhost:$ApiPort"
    $env:E2E_WEB_URL = "http://localhost:$WebPort"
    $env:POSTGRES_DB = "ux09e2e"
    $env:POSTGRES_USER = "ux09e2e"
    $env:POSTGRES_PASSWORD = "db-$([Guid]::NewGuid().ToString('N'))"
    $env:MINIO_ROOT_USER = "minio$RunToken"
    $env:MINIO_ROOT_PASSWORD = "minio-$([Guid]::NewGuid().ToString('N'))"
    $env:OBJECT_STORAGE_BUCKET = "resumes"
    $env:CONTACT_ENCRYPTION_KEY = New-Base64UrlKey
    # ContactCipher currently consumes this value as raw bytes in development.
    $env:CONTACT_LOOKUP_SECRET = [Guid]::NewGuid().ToString("N")
    $env:LLM_CONFIG_ENCRYPTION_KEY = New-Base64UrlKey
    $env:FEISHU_CONFIG_ENCRYPTION_KEY = New-Base64UrlKey
    $env:EMAIL_ENCRYPTION_KEY = New-Base64UrlKey
    $env:LLM_PROVIDER_ALLOWLIST_JSON = "{}"
    $env:CORS_ORIGINS = "[`"http://localhost:$WebPort`"]"
    $env:OFFER_PUBLIC_BASE_URL = "http://localhost:$WebPort"
    $env:VITE_API_PROXY_TARGET = "http://localhost:$ApiPort"
    $env:NODE_PATH = Join-Path $PrototypeRoot "node_modules"

    Write-Host "[phase3-e2e] validating merged Compose"
    Invoke-Compose config --quiet

    Write-Host "[phase3-e2e] migrating, provisioning bucket, and seeding synthetic fixtures"
    Invoke-Compose up --build --abort-on-container-exit --exit-code-from e2e-prepare e2e-prepare

    Write-Host "[phase3-e2e] starting api, worker, proxy, and dependencies"
    Invoke-Compose up -d api worker proxy
    Wait-HttpOk "$($env:E2E_BASE_URL)/health/ready"

    Push-Location $PrototypeRoot
    try {
        if (-not (Test-Path (Join-Path $PrototypeRoot "node_modules\playwright"))) {
            Write-Host "[phase3-e2e] installing locked frontend dependencies"
            & npm.cmd ci
            if ($LASTEXITCODE -ne 0) { throw "npm ci failed with exit code $LASTEXITCODE" }
        }
        if (-not $SkipBrowserInstall) {
            Write-Host "[phase3-e2e] ensuring Chromium is available"
            & npm.cmd exec -- playwright install chromium
            if ($LASTEXITCODE -ne 0) { throw "Playwright Chromium installation failed with exit code $LASTEXITCODE" }
        }
        $ViteProcess = Start-Process -FilePath (Get-Command npm.cmd).Source `
            -ArgumentList @("run", "dev", "--", "--host", "127.0.0.1", "--port", [string]$WebPort, "--strictPort") `
            -WorkingDirectory $PrototypeRoot -WindowStyle Hidden -PassThru `
            -RedirectStandardOutput $ViteOut -RedirectStandardError $ViteErr
    } finally {
        Pop-Location
    }

    Wait-HttpOk $env:E2E_WEB_URL
    Write-Host "[phase3-e2e] running queued-worker-stop, browser-close, and api-restart recovery"
    & node $RecoveryScript
    if ($LASTEXITCODE -ne 0) { throw "Playwright recovery scenario failed with exit code $LASTEXITCODE" }
    $Succeeded = $true
    Write-Host "[phase3-e2e] PASS"
} catch {
    $Failure = $_
    Write-Host "[phase3-e2e] FAIL: $($_.Exception.Message)" -ForegroundColor Red
    try { Invoke-Compose ps } catch { Write-Host "[phase3-e2e] compose ps unavailable" }
    try { Invoke-Compose logs --no-color --tail 100 e2e-prepare api worker } catch { Write-Host "[phase3-e2e] service logs unavailable" }
} finally {
    if ($ViteProcess -and -not $ViteProcess.HasExited) {
        Stop-Process -Id $ViteProcess.Id -Force -ErrorAction SilentlyContinue
    }
    if ($Succeeded -or -not $KeepOnFailure) {
        try { Invoke-Compose down --volumes --remove-orphans } catch { Write-Host "[phase3-e2e] cleanup failed" }
        if ($env:E2E_APP_IMAGE) { & docker image rm $env:E2E_APP_IMAGE 2>$null | Out-Null }
        Remove-Item -LiteralPath $env:E2E_BROWSER_PROFILE -Recurse -Force -ErrorAction SilentlyContinue
    } else {
        Write-Host "[phase3-e2e] resources retained; inspect with:"
        Write-Host "docker compose -p $ProjectName -f `"$ComposeBase`" -f `"$ComposeOverride`" ps"
    }
}

if (-not $Succeeded) { throw $Failure }
