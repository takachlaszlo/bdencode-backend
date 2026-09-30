#requires -Version 5.1
<#
Builds a *synthetic* script from the real install/windows.ps1 so its
argument-handling paths can be executed without touching WSL, the registry or
UAC.  The pieces are cut out of the real file with the PowerShell parser:

  * the param block (real parameter names, defaults and validation),
  * the $script:InstallArguments capture and the reserved-port guard,
  * the argument-forwarding helper functions,
  * the top-level "not administrator" relaunch block  (-Mode Elevation), or
  * Register-ContinuationAfterRestart                  (-Mode RunOnce).

System-touching commands are replaced by recorders that write a UTF-8 JSON
result file (never the console, whose code page differs between Windows
PowerShell 5.1 and PowerShell 7).
#>
param(
    [Parameter(Mandatory)] [string]$Source,
    [Parameter(Mandatory)] [string]$Out,
    [Parameter(Mandatory)] [ValidateSet("Elevation", "RunOnce", "ParseOnly")] [string]$Mode
)

$tokens = $null
$errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($Source, [ref]$tokens, [ref]$errors)
if ($errors.Count -gt 0) {
    foreach ($item in $errors) { [Console]::Error.WriteLine("parse error: " + $item.Message) }
    exit 2
}
if ($Mode -eq "ParseOnly") {
    [System.IO.File]::WriteAllText($Out, "ok", (New-Object System.Text.UTF8Encoding($false)))
    exit 0
}

function Get-FunctionText([string]$Name) {
    $found = $ast.FindAll({
            param($node)
            $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq $Name
        }, $false)
    if ($found.Count -ne 1) { throw "function $Name not found exactly once" }
    return $found[0].Extent.Text
}

$statements = @($ast.EndBlock.Statements)
$capture = @($statements | Where-Object {
        $_ -is [System.Management.Automation.Language.AssignmentStatementAst] -and
        $_.Left.Extent.Text -eq '$script:InstallArguments'
    })
$guard = @($statements | Where-Object {
        $_ -is [System.Management.Automation.Language.IfStatementAst] -and
        $_.Clauses[0].Item1.Extent.Text -match '^\$Port\s+-eq\s+8796$'
    })
$elevation = @($statements | Where-Object {
        $_ -is [System.Management.Automation.Language.IfStatementAst] -and
        $_.Clauses[0].Item1.Extent.Text -match 'Test-Administrator'
    })
if ($capture.Count -ne 1) { throw "InstallArguments capture not found exactly once" }
if ($guard.Count -ne 1) { throw "reserved-port guard not found exactly once" }
if ($elevation.Count -ne 1) { throw "elevation block not found exactly once" }
if ($null -eq $ast.ParamBlock) { throw "param block not found" }

$recorder = @'

function Write-TestResult($Record) {
    $json = $Record | ConvertTo-Json -Compress -Depth 6
    [System.IO.File]::WriteAllText($env:BDENCODE_TEST_RESULT, $json, (New-Object System.Text.UTF8Encoding($false)))
}
'@

$parts = New-Object System.Collections.Generic.List[string]
# The attribute list ([CmdletBinding()]) is not part of the ParamBlockAst extent.
$attributeText = ($ast.ParamBlock.Attributes | ForEach-Object { $_.Extent.Text }) -join [Environment]::NewLine
$parts.Add($attributeText + [Environment]::NewLine + $ast.ParamBlock.Extent.Text)
$parts.Add('Set-StrictMode -Version Latest')
$parts.Add('$ErrorActionPreference = "Stop"')
$parts.Add($capture[0].Extent.Text)
$parts.Add($guard[0].Extent.Text)
$parts.Add($recorder)
$parts.Add((Get-FunctionText "Wait-BeforeExit"))
$parts.Add((Get-FunctionText "ConvertTo-CommandLineArgument"))
$parts.Add((Get-FunctionText "Get-ForwardedArgumentLine"))

if ($Mode -eq "Elevation") {
    $parts.Add(@'
function Test-Administrator { return $false }
function Start-Process {
    param(
        [Parameter(Position = 0)] [string]$FilePath,
        [string]$ArgumentList,
        [string]$Verb,
        [switch]$PassThru,
        [switch]$Wait
    )
    $bound = [ordered]@{}
    foreach ($key in $script:InstallArguments.Keys) {
        $value = $script:InstallArguments[$key]
        if ($value -is [System.Management.Automation.SwitchParameter]) {
            $bound[$key] = [bool]$value.IsPresent
        } else {
            $bound[$key] = $value
        }
    }
    Write-TestResult ([ordered]@{
            file_path     = $FilePath
            verb          = $Verb
            argument_list = $ArgumentList
            pass_thru     = [bool]$PassThru
            wait          = [bool]$Wait
            script_path   = $PSCommandPath
            bound         = $bound
        })
    if ($env:BDENCODE_TEST_ELEVATION_REFUSED -eq "1") { throw "The operation was canceled by the user." }
    return [pscustomobject]@{ ExitCode = 7 }
}
'@)
    $parts.Add($elevation[0].Extent.Text)
    # Reaching this line means the relaunch block did not exit: a bug.
    $parts.Add('Write-TestResult ([ordered]@{ fell_through = $true })')
    $parts.Add('exit 99')
} else {
    $parts.Add((Get-FunctionText "Register-ContinuationAfterRestart"))
    $parts.Add(@'
$calls = New-Object System.Collections.Generic.List[object]
function New-Item {
    param([string]$Path, [string]$ItemType, [switch]$Force)
    $calls.Add([ordered]@{ command = "New-Item"; path = $Path })
}
function New-ItemProperty {
    param([string]$Path, [string]$Name, [string]$Value, [string]$PropertyType, [switch]$Force)
    $calls.Add([ordered]@{ command = "New-ItemProperty"; path = $Path; name = $Name; value = $Value; type = $PropertyType })
}
$env:SystemRoot = 'C:\Windows'
Register-ContinuationAfterRestart
Write-TestResult ([ordered]@{ calls = $calls.ToArray(); script_path = $PSCommandPath })
exit 0
'@)
}

# UTF-8 with BOM: Windows PowerShell 5.1 otherwise reads the accents as ANSI.
[System.IO.File]::WriteAllText($Out, ($parts -join [Environment]::NewLine), (New-Object System.Text.UTF8Encoding($true)))
exit 0
