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

$script:InstallLog = Join-Path $env:LOCALAPPDATA "BDEncode\install.log"

function Wait-BeforeExit {
    param([string]$Prompt = "Nyomj Entert a bezáráshoz")

    try {
        [void](Read-Host $Prompt)
    } catch {
        # Nincs interaktív konzol (például automatizált futtatásnál).
    }
}

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
    # A PowerShell here-string Windows alatt CRLF-fel készül, a bash viszont
    # a sorvégi CR karaktert a parancs részének tekintené.
    $normalizedScript = $Script -replace "`r`n", "`n" -replace "`r", "`n"
    $bytes = [Text.Encoding]::UTF8.GetBytes($normalizedScript)
    $payload = [Convert]::ToBase64String($bytes)
    Invoke-Wsl -User $User -Command @(
        "/bin/bash", "-lc", "printf '%s' '$payload' | base64 -d | /bin/bash"
    )
}

function Get-InstalledDistros {
    $previousErrorPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = "Continue"
        $lines = & wsl.exe --list --quiet 2>$null
        $listExitCode = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previousErrorPreference
    }
    if ($listExitCode -ne 0) { return @() }
    return @($lines | ForEach-Object { ($_ -replace "`0", "").Trim() } | Where-Object { $_ })
}

function Get-DistroVersion {
    $previousErrorPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = "Continue"
        $lines = & wsl.exe --list --verbose 2>$null
        $listExitCode = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previousErrorPreference
    }
    if ($listExitCode -ne 0) { return $null }

    $escapedName = [regex]::Escape($DistroName)
    foreach ($line in $lines) {
        $clean = ($line -replace "`0", "").Trim()
        if ($clean -match "^\*?\s*$escapedName\s+.+\s+([12])\s*$") {
            return [int]$Matches[1]
        }
    }
    return $null
}

function Wait-DistroVersion {
    param(
        [Parameter(Mandatory)] [int]$ExpectedVersion,
        [int]$TimeoutSeconds = 120
    )

    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    do {
        if ((Get-DistroVersion) -eq $ExpectedVersion) { return $true }
        Start-Sleep -Seconds 2
    } while ([DateTime]::UtcNow -lt $deadline)
    return $false
}

function Test-LocalHealth {
    param([Parameter(Mandatory)] [string]$Uri)

    Add-Type -AssemblyName System.Net.Http
    $handler = [Net.Http.HttpClientHandler]::new()
    $handler.UseProxy = $false
    $client = [Net.Http.HttpClient]::new($handler)
    $client.Timeout = [TimeSpan]::FromSeconds(3)
    try {
        $body = $client.GetStringAsync($Uri).GetAwaiter().GetResult()
        $health = $body | ConvertFrom-Json
        return $health.status -eq "ok"
    } catch {
        return $false
    } finally {
        $client.Dispose()
        $handler.Dispose()
    }
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
    try {
        $elevated = Start-Process powershell.exe -Verb RunAs -ArgumentList $argumentLine -PassThru -Wait
        exit $elevated.ExitCode
    } catch {
        Write-Host "`nA rendszergazdai indítás nem történt meg." -ForegroundColor Red
        Write-Host "A Windows kérdésénél válaszd az Igen lehetőséget."
        Write-Host "Részletek: $($_.Exception.Message)"
        Wait-BeforeExit
        exit 1
    }
}

$logDirectory = Split-Path -Parent $script:InstallLog
New-Item -ItemType Directory -Path $logDirectory -Force | Out-Null
try {
    Start-Transcript -Path $script:InstallLog -Append -Force | Out-Null
} catch {
    # A telepítést a transcript esetleges hibája nem állíthatja meg.
}

trap {
    Write-Host "`nA BDEncode telepítése hibával leállt." -ForegroundColor Red
    Write-Host $_.Exception.Message -ForegroundColor Red
    Write-Host "`nA részletes napló itt található:"
    Write-Host $script:InstallLog -ForegroundColor Yellow
    try { Stop-Transcript | Out-Null } catch { }
    Wait-BeforeExit
    exit 1
}

Write-Host "BDEncode Windows telepítő" -ForegroundColor Cyan
Write-Host "A kódoló WSL2/Debian alatt fut, a kezelőfelület Windowsból nyílik meg."

$previousErrorPreference = $ErrorActionPreference
try {
    $ErrorActionPreference = "Continue"
    & wsl.exe --status *> $null
    $wslStatusExitCode = $LASTEXITCODE
} finally {
    $ErrorActionPreference = $previousErrorPreference
}
if ($wslStatusExitCode -ne 0) {
    Write-Host "WSL2 telepítése…" -ForegroundColor Yellow
    & wsl.exe --install --no-distribution
    if ($LASTEXITCODE -ne 0) {
        throw "A WSL telepítése nem sikerült (exit=$LASTEXITCODE)."
    }
    Register-ContinuationAfterRestart
    Write-Host "A WSL Windows-összetevői elkészültek. Indítsd újra a gépet; a telepítő bejelentkezés után automatikusan folytatódik." -ForegroundColor Green
    Wait-BeforeExit
    exit 0
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
    & wsl.exe --set-default-version 2
    if ($LASTEXITCODE -ne 0) {
        throw "A WSL2 nem állítható be alapértelmezettként."
    }
    & wsl.exe --install --distribution $DistroName --no-launch --location $WslLocation
    if ($LASTEXITCODE -ne 0) {
        throw "$DistroName telepítése nem sikerült (exit=$LASTEXITCODE)."
    }
}

if (-not $newDistro -and -not $AllowExistingDistro) {
    & wsl.exe --distribution $DistroName --user root --exec /usr/bin/test -f /etc/bdencode/windows-managed
    $hasManagedMarker = $LASTEXITCODE -eq 0
    $hasInstallerDisk = Test-Path -LiteralPath (Join-Path $WslLocation "ext4.vhdx")
    if (-not $hasManagedMarker -and -not $hasInstallerDisk) {
        throw "A '$DistroName' disztribúció már létezik és nem a BDEncode kezelése alatt áll. Használd a -AllowExistingDistro kapcsolót, ha tudatosan ebbe szeretnél telepíteni."
    }
    if (-not $hasManagedMarker) {
        Write-Host "A korábban félbeszakadt BDEncode Debian telepítés folytatása…" -ForegroundColor Yellow
    }
}

$distroVersion = Get-DistroVersion
if ($distroVersion -ne 2) {
    & wsl.exe --set-version $DistroName 2
    $setVersionExitCode = $LASTEXITCODE
    if (-not (Wait-DistroVersion -ExpectedVersion 2)) {
        throw "A disztribúció nem állítható WSL2 módba (exit=$setVersionExitCode)."
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
apt-get install -y --no-install-recommends sudo git ca-certificates curl python3
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
$legacyKeepAlivePath = Join-Path $startup "BDEncode-WSL.vbs"
Remove-Item -LiteralPath $legacyKeepAlivePath -Force -ErrorAction SilentlyContinue

$taskName = "BDEncode WSL"
$taskUser = "$env:USERDOMAIN\$env:USERNAME"
$keepAliveScriptPath = Join-Path $logDirectory "keepalive.ps1"
$safeDistroLiteral = $DistroName.Replace("'", "''")
$safeUserLiteral = $LinuxUser.Replace("'", "''")
$keepAliveScriptTemplate = @'
$arguments = @(
    '--distribution', '@DISTRO@',
    '--user', '@LINUX_USER@',
    '--exec', '/bin/sleep', 'infinity'
)
while ($true) {
    $process = Start-Process `
        -FilePath "$env:SystemRoot\System32\wsl.exe" `
        -ArgumentList $arguments `
        -WindowStyle Hidden `
        -PassThru
    $process.WaitForExit()
    Start-Sleep -Seconds 2
}
'@
$keepAliveScript = $keepAliveScriptTemplate.Replace("@DISTRO@", $safeDistroLiteral).Replace("@LINUX_USER@", $safeUserLiteral)
[IO.File]::WriteAllText($keepAliveScriptPath, $keepAliveScript, [Text.UTF8Encoding]::new($true))
$taskArguments = '-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "{0}"' -f $keepAliveScriptPath
$taskAction = New-ScheduledTaskAction -Execute "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe" -Argument $taskArguments
$taskTrigger = New-ScheduledTaskTrigger -AtLogOn -User $taskUser
$taskPrincipal = New-ScheduledTaskPrincipal -UserId $taskUser -LogonType Interactive -RunLevel Limited
$taskSettingsParameters = @{
    AllowStartIfOnBatteries = $true
    DontStopIfGoingOnBatteries = $true
    ExecutionTimeLimit = [TimeSpan]::Zero
    MultipleInstances = "IgnoreNew"
    RestartCount = 3
    RestartInterval = New-TimeSpan -Minutes 1
}
$taskSettings = New-ScheduledTaskSettingsSet @taskSettingsParameters
$registerTaskParameters = @{
    TaskName = $taskName
    Action = $taskAction
    Trigger = $taskTrigger
    Principal = $taskPrincipal
    Settings = $taskSettings
    Description = "Keeps the local BDEncode WSL services available while the user is signed in."
    Force = $true
}
Register-ScheduledTask @registerTaskParameters | Out-Null
Start-ScheduledTask -TaskName $taskName

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
    if (Test-LocalHealth -Uri "http://127.0.0.1:$Port/encoder/api/v1/health") {
        $healthy = $true
        break
    }
    Start-Sleep -Seconds 1
}
if (-not $healthy) { throw "A telepítés elkészült, de a Windows felől nem érhető el a helyi weboldal." }

Write-Host "`nA BDEncode használatra kész." -ForegroundColor Green
Write-Host "Weboldal: $url"
Write-Host "Forrás: $sourceFull -> $wslSource"
Write-Host "Kész fájlok: \\wsl.localhost\$DistroName\home\$LinuxUser\encode\completed"
Start-Process $url
try { Stop-Transcript | Out-Null } catch { }
Wait-BeforeExit
