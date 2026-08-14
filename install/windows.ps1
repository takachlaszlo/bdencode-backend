[CmdletBinding()]
param(
    [string]$SourcePath,
    [ValidateRange(1024, 65535)]
    [int]$Port = 8787,
    [string]$DistroName = "Debian",
    [string]$LinuxUser,
    [string]$WslLocation = (Join-Path $env:LOCALAPPDATA "BDEncodeWSL\Debian"),
    [string]$Repository = "https://github.com/takachlaszlo/bdencode-backend.git",
    [string]$Branch = "codex/frontend",
    [switch]$AllowExistingDistro
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

function Test-Administrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [Security.Principal.WindowsPrincipal]::new($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Invoke-Wsl {
    param(
        [Parameter(Mandatory)] [string]$User,
        [Parameter(Mandatory)] [string[]]$Command
    )
    & wsl.exe --distribution $DistroName --user $User --exec @Command
    if ($LASTEXITCODE -ne 0) {
        throw "A WSL parancs hibával állt le (exit=$LASTEXITCODE): $($Command -join ' ')"
    }
}

function Invoke-WslScript {
    param(
        [Parameter(Mandatory)] [string]$User,
        [Parameter(Mandatory)] [string]$Script
    )
    $bytes = [Text.Encoding]::UTF8.GetBytes($Script)
    $payload = [Convert]::ToBase64String($bytes)
    Invoke-Wsl -User $User -Command @(
        "/bin/bash", "-lc", "printf '%s' '$payload' | base64 -d | /bin/bash"
    )
}

function Get-InstalledDistros {
    $lines = & wsl.exe --list --quiet 2>$null
    if ($LASTEXITCODE -ne 0) { return @() }
    return @($lines | ForEach-Object { ($_ -replace "`0", "").Trim() } | Where-Object { $_ })
}

function Register-ContinuationAfterRestart {
    $powerShell = "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe"
    $command = '"{0}" -NoProfile -ExecutionPolicy Bypass -File "{1}"' -f $powerShell, $PSCommandPath
    $runOnce = "HKCU:\Software\Microsoft\Windows\CurrentVersion\RunOnce"
    New-Item -Path $runOnce -Force | Out-Null
    New-ItemProperty -Path $runOnce -Name "BDEncodeInstall" -Value $command -PropertyType String -Force | Out-Null
}

if (-not (Test-Administrator)) {
    $argumentLine = '-NoProfile -ExecutionPolicy Bypass -File "{0}"' -f $PSCommandPath
    Start-Process powershell.exe -Verb RunAs -ArgumentList $argumentLine
    exit 0
}

Write-Host "BDEncode Windows telepítő" -ForegroundColor Cyan
Write-Host "A kódoló WSL2/Debian alatt fut, a kezelőfelület Windowsból nyílik meg."

& wsl.exe --status *> $null
if ($LASTEXITCODE -ne 0) {
    Write-Host "WSL2 telepítése…" -ForegroundColor Yellow
    & wsl.exe --install --no-distribution
    if ($LASTEXITCODE -ne 0) {
        throw "A WSL telepítése nem sikerült (exit=$LASTEXITCODE)."
    }
    Register-ContinuationAfterRestart
    Write-Host "A WSL Windows-összetevői elkészültek. Indítsd újra a gépet; a telepítő bejelentkezés után automatikusan folytatódik." -ForegroundColor Green
    Read-Host "Nyomj Entert a bezáráshoz"
    exit 3010
}

& wsl.exe --update
if ($LASTEXITCODE -ne 0) {
    throw "A WSL frissítése nem sikerült."
}

$installed = Get-InstalledDistros
$newDistro = $DistroName -notin $installed
if ($newDistro) {
    Write-Host "$DistroName WSL2 környezet telepítése…" -ForegroundColor Yellow
    New-Item -ItemType Directory -Path $WslLocation -Force | Out-Null
    & wsl.exe --install --distribution $DistroName --no-launch --location $WslLocation
    if ($LASTEXITCODE -ne 0) {
        throw "$DistroName telepítése nem sikerült (exit=$LASTEXITCODE)."
    }
    & wsl.exe --set-version $DistroName 2
    if ($LASTEXITCODE -ne 0) { throw "A disztribúció nem állítható WSL2 módba." }
} elseif (-not $AllowExistingDistro) {
    & wsl.exe --distribution $DistroName --user root --exec /usr/bin/test -f /etc/bdencode/windows-managed
    if ($LASTEXITCODE -ne 0) {
        throw "A '$DistroName' disztribúció már létezik és nem a BDEncode kezelése alatt áll. Használd a -AllowExistingDistro kapcsolót, ha tudatosan ebbe szeretnél telepíteni."
    }
}

if (-not $LinuxUser) {
    $LinuxUser = ($env:USERNAME.ToLowerInvariant() -replace '[^a-z0-9_-]', '')
    if (-not $LinuxUser) { $LinuxUser = "bdencode" }
    if ($LinuxUser[0] -match '[0-9-]') { $LinuxUser = "u$LinuxUser" }
}
if ($LinuxUser -notmatch '^[a-z_][a-z0-9_-]{0,30}$') {
    throw "Érvénytelen Linux felhasználónév: $LinuxUser"
}

$rootBootstrap = @"
set -Eeuo pipefail
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends sudo git ca-certificates curl
if ! id -u '$LinuxUser' >/dev/null 2>&1; then
    useradd --create-home --shell /bin/bash '$LinuxUser'
fi
usermod -aG sudo '$LinuxUser'
printf '%s ALL=(ALL) NOPASSWD:ALL\n' '$LinuxUser' >/etc/sudoers.d/bdencode-wsl
chmod 0440 /etc/sudoers.d/bdencode-wsl
install -d -m 0755 /etc/bdencode
touch /etc/bdencode/windows-managed
printf '[boot]\nsystemd=true\n\n[user]\ndefault=$LinuxUser\n' >/etc/wsl.conf
"@
Invoke-WslScript -User "root" -Script $rootBootstrap

& wsl.exe --terminate $DistroName
if ($LASTEXITCODE -ne 0) { throw "A WSL újraindítása nem sikerült." }
Start-Sleep -Seconds 2
Invoke-Wsl -User "root" -Command @("/bin/systemctl", "show-environment")

if (-not $SourcePath) {
    Add-Type -AssemblyName System.Windows.Forms
    $dialog = [Windows.Forms.FolderBrowserDialog]::new()
    $dialog.Description = "Válaszd ki azt a Windows-mappát vagy meghajtót, amely alatt a BDMV források találhatók"
    $dialog.ShowNewFolderButton = $false
    if ($dialog.ShowDialog() -ne [Windows.Forms.DialogResult]::OK) {
        throw "A forrásmappa kiválasztása megszakadt."
    }
    $SourcePath = $dialog.SelectedPath
}
$sourceItem = Get-Item -LiteralPath $SourcePath -ErrorAction Stop
if (-not $sourceItem.PSIsContainer) { throw "A forrás csak mappa lehet: $SourcePath" }
$sourceFull = $sourceItem.FullName.TrimEnd('\')
if ($sourceFull -notmatch '^([A-Za-z]):(?:\\(.*))?$') {
    throw "Jelenleg csak meghajtóbetűjeles helyi Windows-mappa támogatott (például D:\\Filmek)."
}
$drive = $Matches[1].ToLowerInvariant()
$relative = if ($Matches[2]) { $Matches[2] -replace '\\', '/' } else { "" }
$wslSource = if ($relative) { "/mnt/$drive/$relative" } else { "/mnt/$drive" }
if ($wslSource.IndexOfAny([char[]]@('"', '&', "`r", "`n")) -ge 0) {
    throw "A kiválasztott útvonal idézőjelet, & jelet vagy sortörést tartalmaz; válassz egyszerűbb mappanevet."
}
$sourcePayload = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($wslSource))

$mountScript = @"
set -Eeuo pipefail
install -d -m 0755 '/mnt/$drive'
if ! mountpoint -q '/mnt/$drive'; then
    mount -t drvfs '$($drive.ToUpperInvariant()):' '/mnt/$drive'
fi
source_path=`$(printf '%s' '$sourcePayload' | base64 -d)
test -d "`$source_path"
"@
Invoke-WslScript -User "root" -Script $mountScript

$stamp = Get-Date -Format "yyyyMMddHHmmss"
$checkout = "/home/$LinuxUser/.cache/bdencode-windows-installer-$stamp"
Invoke-Wsl -User $LinuxUser -Command @("/bin/mkdir", "-p", "/home/$LinuxUser/.cache")
Invoke-Wsl -User $LinuxUser -Command @(
    "/usr/bin/git", "clone", "--quiet", "--single-branch", "--branch", $Branch,
    $Repository, $checkout
)

try {
    Write-Host "A backend, a médiaeszközök és a webes felület telepítése. Ez az első alkalommal hosszabb ideig tarthat…" -ForegroundColor Yellow
    Invoke-Wsl -User $LinuxUser -Command @(
        "/usr/bin/env",
        "BDENCODE_SOURCE_ROOT=$wslSource",
        "BDENCODE_WINDOWS_PORT=$Port",
        "BDENCODE_CPU_PERCENT=80",
        "/bin/bash", "$checkout/install/wsl-install.sh"
    )
} finally {
    Invoke-Wsl -User $LinuxUser -Command @("/bin/rm", "-rf", "--", $checkout)
}

$startup = [Environment]::GetFolderPath("Startup")
$keepAlivePath = Join-Path $startup "BDEncode-WSL.vbs"
$keepAliveCommand = 'wsl.exe --distribution "{0}" --user "{1}" --exec /bin/sleep infinity' -f $DistroName, $LinuxUser
$vbs = 'CreateObject("WScript.Shell").Run "{0}", 0, False' -f ($keepAliveCommand -replace '"', '""')
[IO.File]::WriteAllText($keepAlivePath, $vbs, [Text.Encoding]::ASCII)
Start-Process wscript.exe -ArgumentList ('"{0}"' -f $keepAlivePath) -WindowStyle Hidden

$desktop = [Environment]::GetFolderPath("Desktop")
$urlPath = Join-Path $desktop "BDEncode.url"
$url = "http://localhost:$Port/encoder/"
[IO.File]::WriteAllText($urlPath, "[InternetShortcut]`r`nURL=$url`r`n", [Text.Encoding]::ASCII)

$shell = New-Object -ComObject WScript.Shell
$completedShortcut = $shell.CreateShortcut((Join-Path $desktop "BDEncode elkészült filmek.lnk"))
$completedShortcut.TargetPath = "explorer.exe"
$completedShortcut.Arguments = '"\\wsl.localhost\{0}\home\{1}\encode\completed"' -f $DistroName, $LinuxUser
$completedShortcut.Save()

$healthy = $false
for ($attempt = 0; $attempt -lt 30; $attempt++) {
    try {
        $health = Invoke-RestMethod -Uri "http://localhost:$Port/encoder/api/v1/health" -TimeoutSec 2
        if ($health.status -eq "ok") { $healthy = $true; break }
    } catch {
        Start-Sleep -Seconds 1
    }
}
if (-not $healthy) { throw "A telepítés elkészült, de a Windows felől nem érhető el a helyi weboldal." }

Write-Host "`nA BDEncode használatra kész." -ForegroundColor Green
Write-Host "Weboldal: $url"
Write-Host "Forrás: $sourceFull -> $wslSource"
Write-Host "Kész fájlok: \\wsl.localhost\$DistroName\home\$LinuxUser\encode\completed"
Start-Process $url
Read-Host "Nyomj Entert a bezáráshoz"
