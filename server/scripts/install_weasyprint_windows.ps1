$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$weasyVersion = "69.0"
$archiveSha256 = "330101ff3ea50ebde4abf805283b6d703d5f3d71c77c983db94357ec4524a3ef"
$executableSha256 = "f9bc7d33fca891929aeeedcdf3a553fb1a26a7a45d9ee868fe6ec13dd10c514e"
$fontSha256 = "2c76254f6fc379fddfce0a7e84fb5385bb135d3e399294f6eeb6680d0365b74b"
$fontCommit = "523d033d6cb47f4a80c58a35753646f5c3608a78"
$repositoryRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$runtimeDirectory = Join-Path $repositoryRoot ".tmp\weasyprint-runtime\v$weasyVersion"
$archivePath = Join-Path $runtimeDirectory "weasyprint-windows.zip"
$executablePath = Join-Path $runtimeDirectory "weasyprint.exe"
$expandedExecutablePath = Join-Path $runtimeDirectory "dist\weasyprint.exe"
$fontPath = Join-Path $runtimeDirectory "NotoSansCJKsc-Regular.otf"

function Assert-Sha256([string] $path, [string] $expected) {
    $actual = (Get-FileHash -Algorithm SHA256 -LiteralPath $path).Hash.ToLowerInvariant()
    if ($actual -ne $expected) {
        throw "SHA-256 mismatch for $path"
    }
}

if ((Test-Path -LiteralPath $executablePath) -and (Test-Path -LiteralPath $fontPath)) {
    Assert-Sha256 $executablePath $executableSha256
    Assert-Sha256 $fontPath $fontSha256
    Write-Output "WeasyPrint Windows runtime v$weasyVersion is already verified."
    exit 0
}

New-Item -ItemType Directory -Force -Path $runtimeDirectory | Out-Null
Invoke-WebRequest `
    -Uri "https://github.com/Kozea/WeasyPrint/releases/download/v$weasyVersion/weasyprint-windows.zip" `
    -OutFile $archivePath
Assert-Sha256 $archivePath $archiveSha256
Expand-Archive -LiteralPath $archivePath -DestinationPath $runtimeDirectory -Force
Assert-Sha256 $expandedExecutablePath $executableSha256
Copy-Item -LiteralPath $expandedExecutablePath -Destination $executablePath -Force

Invoke-WebRequest `
    -Uri "https://raw.githubusercontent.com/notofonts/noto-cjk/$fontCommit/Sans/OTF/SimplifiedChinese/NotoSansCJKsc-Regular.otf" `
    -OutFile $fontPath
Assert-Sha256 $fontPath $fontSha256
Write-Output "Installed and verified WeasyPrint Windows runtime v$weasyVersion and Noto Sans CJK SC."
