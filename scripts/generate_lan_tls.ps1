<#
Generate a private LAN CA and a Meeting AI HTTPS certificate on Windows.

Use this script on Windows; generate_lan_tls.sh serves macOS and Linux. Both
produce the same file names and .env settings so the application is portable.
#>
[CmdletBinding()]
param(
    [string[]]$Ip = @(),
    [string[]]$Hostname = @("localhost"),
    [ValidateRange(1, 65535)]
    [int]$Port = 5001,
    [string]$OutputDir = "",
    [switch]$WriteEnv,
    [switch]$Force,
    [switch]$RegenerateCa
)

$ErrorActionPreference = "Stop"

function Fail([string]$Message) {
    throw "Error: $Message"
}

function Invoke-OpenSsl([string[]]$Arguments) {
    & $script:OpenSsl @Arguments
    if ($LASTEXITCODE -ne 0) {
        Fail "OpenSSL command failed: $($Arguments -join ' ')"
    }
}

function Test-LanIpv4([string]$Address) {
    try {
        $parsed = [System.Net.IPAddress]::Parse($Address)
        return $parsed.AddressFamily -eq [System.Net.Sockets.AddressFamily]::InterNetwork -and
            -not $parsed.Equals([System.Net.IPAddress]::Any) -and
            -not $parsed.Equals([System.Net.IPAddress]::Loopback) -and
            -not $Address.StartsWith("169.254.")
    } catch {
        return $false
    }
}

function Get-PrimaryLanIpv4 {
    $routes = @(Get-NetRoute -AddressFamily IPv4 -DestinationPrefix "0.0.0.0/0" -ErrorAction SilentlyContinue |
        Sort-Object -Property RouteMetric, InterfaceMetric)
    foreach ($route in $routes) {
        $address = Get-NetIPAddress -InterfaceIndex $route.InterfaceIndex -AddressFamily IPv4 -ErrorAction SilentlyContinue |
            Where-Object { Test-LanIpv4 $_.IPAddress } |
            Select-Object -First 1 -ExpandProperty IPAddress
        if ($address) { return $address }
    }
    return Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
        Where-Object { Test-LanIpv4 $_.IPAddress } |
        Select-Object -First 1 -ExpandProperty IPAddress
}

function Add-Unique([System.Collections.Generic.List[string]]$List, [string]$Value) {
    if (-not $List.Contains($Value)) { [void]$List.Add($Value) }
}

function Set-DotEnvValue([string]$Key, [string]$Value) {
    $lines = if (Test-Path -LiteralPath $script:EnvFile) {
        [System.IO.File]::ReadAllLines($script:EnvFile)
    } else {
        @()
    }
    $safeValue = if ($Value -match '[\s#]') { '"' + $Value.Replace('"', '\"') + '"' } else { $Value }
    $prefix = "$Key="
    $replacement = "$Key=$safeValue"
    $newLines = [System.Collections.Generic.List[string]]::new()
    $replaced = $false
    foreach ($line in $lines) {
        if ($line.StartsWith($prefix, [System.StringComparison]::Ordinal)) {
            if (-not $replaced) { [void]$newLines.Add($replacement) }
            $replaced = $true
        } else {
            [void]$newLines.Add($line)
        }
    }
    if (-not $replaced) { [void]$newLines.Add($replacement) }
    $utf8NoBom = [System.Text.UTF8Encoding]::new($false)
    [System.IO.File]::WriteAllLines($script:EnvFile, $newLines, $utf8NoBom)
}

if ($RegenerateCa -and -not $Force) {
    Fail "-RegenerateCa requires -Force"
}

$OpenSslCommand = Get-Command openssl -ErrorAction SilentlyContinue
if (-not $OpenSslCommand) {
    Fail "OpenSSL is required. Install it (for example: winget install ShiningLight.OpenSSL.Light), open a new PowerShell, then rerun."
}
$script:OpenSsl = $OpenSslCommand.Source
$ProjectDir = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
if (-not $OutputDir) { $OutputDir = Join-Path $ProjectDir ".local\certs" }
$OutputDir = [System.IO.Path]::GetFullPath($OutputDir)
$script:EnvFile = Join-Path $ProjectDir ".env"

$validIps = [System.Collections.Generic.List[string]]::new()
foreach ($address in $Ip) {
    if (-not (Test-LanIpv4 $address)) { Fail "invalid LAN IPv4 address: $address" }
    Add-Unique $validIps $address
}
if ($validIps.Count -eq 0) {
    $detected = Get-PrimaryLanIpv4
    if (-not $detected) { Fail "could not detect a LAN IP; pass -Ip explicitly" }
    Add-Unique $validIps $detected
}
$primaryIp = $validIps[0]

$validHostnames = [System.Collections.Generic.List[string]]::new()
foreach ($name in $Hostname) {
    if ($name -notmatch '^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?$') { Fail "invalid hostname: $name" }
    Add-Unique $validHostnames $name
}

New-Item -ItemType Directory -Force -Path $OutputDir | Out-Null
$caKey = Join-Path $OutputDir "meeting-ai-lan-root-ca.key"
$caCert = Join-Path $OutputDir "meeting-ai-lan-root-ca.crt"
$caCer = Join-Path $OutputDir "meeting-ai-lan-root-ca.cer"
$serverKey = Join-Path $OutputDir "meeting-ai-lan-server.key"
$serverCsr = Join-Path $OutputDir "meeting-ai-lan-server.csr"
$serverCert = Join-Path $OutputDir "meeting-ai-lan-server.crt"
$serverExt = Join-Path $OutputDir "meeting-ai-lan-server.ext"
$caConfig = Join-Path $OutputDir "meeting-ai-lan-ca.conf"
$serial = Join-Path $OutputDir "meeting-ai-lan-root-ca.srl"

if ($RegenerateCa) {
    Remove-Item -Force -ErrorAction SilentlyContinue $caKey, $caCert, $caCer, $serverKey, $serverCsr, $serverCert, $serverExt, $caConfig, $serial
}
if ((Test-Path $serverKey) -or (Test-Path $serverCert)) {
    if (-not $Force) { Fail "server certificate already exists; use -Force to replace it" }
    Remove-Item -Force -ErrorAction SilentlyContinue $serverKey, $serverCsr, $serverCert, $serverExt
}

$hasAnyCa = (Test-Path $caKey) -or (Test-Path $caCert) -or (Test-Path $caCer)
if ($hasAnyCa) {
    if (-not ((Test-Path $caKey) -and (Test-Path $caCert) -and (Test-Path $caCer))) {
        Fail "incomplete CA files; use -Force -RegenerateCa to replace them"
    }
    Write-Host "Reusing existing local root CA: $caCert"
} else {
    Write-Host "Generating private local root CA…"
    Invoke-OpenSsl @("genrsa", "-out", $caKey, "4096")
    $caConfigText = @'
[req]
distinguished_name = distinguished_name
prompt = no
x509_extensions = v3_ca

[distinguished_name]
C = CN
O = Meeting AI Local
OU = LAN
CN = Meeting AI Local LAN Root CA

[v3_ca]
basicConstraints = critical, CA:true, pathlen:0
keyUsage = critical, keyCertSign, cRLSign
subjectKeyIdentifier = hash
'@
    Set-Content -LiteralPath $caConfig -Value $caConfigText -Encoding ascii
    Invoke-OpenSsl @("req", "-x509", "-new", "-sha256", "-key", $caKey, "-out", $caCert, "-days", "3650", "-config", $caConfig)
    Invoke-OpenSsl @("x509", "-in", $caCert, "-outform", "der", "-out", $caCer)
}

$extensionLines = [System.Collections.Generic.List[string]]::new()
@(
    "authorityKeyIdentifier = keyid,issuer",
    "basicConstraints = critical, CA:false",
    "keyUsage = critical, digitalSignature, keyEncipherment",
    "extendedKeyUsage = serverAuth",
    "subjectAltName = @alternative_names",
    "",
    "[alternative_names]"
) | ForEach-Object { [void]$extensionLines.Add($_) }
$index = 1
foreach ($name in $validHostnames) { [void]$extensionLines.Add("DNS.$index = $name"); $index++ }
[void]$extensionLines.Add("IP.$index = 127.0.0.1"); $index++
foreach ($address in $validIps) { [void]$extensionLines.Add("IP.$index = $address"); $index++ }
$extensionLines | Set-Content -LiteralPath $serverExt -Encoding ascii

Write-Host "Generating HTTPS server certificate for $primaryIp…"
Invoke-OpenSsl @("genrsa", "-out", $serverKey, "2048")
Invoke-OpenSsl @("req", "-new", "-sha256", "-key", $serverKey, "-out", $serverCsr, "-subj", "/C=CN/O=Meeting AI Local/OU=LAN/CN=$primaryIp")
Invoke-OpenSsl @("x509", "-req", "-sha256", "-in", $serverCsr, "-CA", $caCert, "-CAkey", $caKey, "-CAcreateserial", "-out", $serverCert, "-days", "825", "-extfile", $serverExt)
Invoke-OpenSsl @("verify", "-CAfile", $caCert, $serverCert)

# The project services usually run as the current Windows user. Restrict keys
# to that user; do not apply this to public certificates or the phone .cer.
try {
    & icacls $caKey /inheritance:r /grant:r "${env:USERNAME}:(F)" | Out-Null
    & icacls $serverKey /inheritance:r /grant:r "${env:USERNAME}:(F)" | Out-Null
} catch {
    Write-Warning "Could not tighten key ACLs; restrict access to $OutputDir manually."
}

if ($WriteEnv) {
    if (-not (Test-Path -LiteralPath $script:EnvFile)) {
        Fail "-WriteEnv requires an existing .env; copy .env.example and set its database/secret values first"
    }
    $envCert = $serverCert.Replace("\", "/")
    $envKey = $serverKey.Replace("\", "/")
    $envCa = $caCert.Replace("\", "/")
    Set-DotEnvValue "HOST" "0.0.0.0"
    Set-DotEnvValue "TLS_CERT_FILE" $envCert
    Set-DotEnvValue "TLS_KEY_FILE" $envKey
    Set-DotEnvValue "TLS_CA_CERT_FILE" $envCa
    Set-DotEnvValue "SESSION_COOKIE_SECURE" "1"
    Set-DotEnvValue "INTERNAL_LIVE_HOST" "127.0.0.1"
    Set-DotEnvValue "INTERNAL_LIVE_SCHEME" "https"
    Set-DotEnvValue "INTERNAL_LIVE_CA_FILE" $envCa
    Write-Host "Updated HTTPS settings in $script:EnvFile"
}

Write-Host ""
Write-Host "Ready."
Write-Host "Phone URL: https://${primaryIp}:$Port"
Write-Host "Install and trust on each phone: $caCer"
Write-Host "Server certificate: $serverCert"
Write-Host "Private keys stay only on this computer: $caKey, $serverKey"
if (-not $WriteEnv) { Write-Host "Run again with -WriteEnv to configure this project automatically." }
Write-Host "Restart the Web and speech Worker after changing .env."
