# Human-in-the-loop reproduction loop (PowerShell twin of hitl-loop.template.sh).
# Copy this file, edit the steps below, and run it.
# The agent runs the script; the user follows prompts in their terminal.
#
# Usage:
#   powershell -File hitl-loop.template.ps1   (or pwsh -File ...)
#
# Two helpers:
#   Step "<instruction>"          -> show instruction, wait for Enter
#   Capture NAME "<question>"     -> show question, record response
#
# At the end, captured values are printed as KEY=VALUE for the agent to parse.

$ErrorActionPreference = 'Stop'
$script:Captured = [ordered]@{}

function Step {
    param([string]$Instruction)
    Write-Host ''
    Write-Host ('>>> ' + $Instruction)
    Read-Host '    [Enter when done]' | Out-Null
}

function Capture {
    param([string]$Name, [string]$Question)
    Write-Host ''
    Write-Host ('>>> ' + $Question)
    $script:Captured[$Name] = Read-Host '    >'
}

# --- edit below ---------------------------------------------------------

Step "Open the app at http://localhost:3000 and sign in."

Capture ERRORED "Click the 'Export' button. Did it throw an error? (y/n)"

Capture ERROR_MSG "Paste the error message (or 'none'):"

# --- edit above ---------------------------------------------------------

Write-Host ''
Write-Host '--- Captured ---'
foreach ($key in $script:Captured.Keys) {
    Write-Host ('{0}={1}' -f $key, $script:Captured[$key])
}
