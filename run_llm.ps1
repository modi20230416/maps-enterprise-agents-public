param(
    [ValidateSet('rush_order', 'future_downtime', 'receipt_delay', 'no_change', 'material_shortage', 'high_order', 'emergency_review', 'material_conflict')]
    [string]$Scenario = 'receipt_delay',
    [switch]$Framework,
    [switch]$Monitor,
    [switch]$Pause,
    [string]$PythonExecutable = ''
)

if ($Monitor -and $Framework) { throw 'Select Monitor or Framework, not both.' }
if ($Framework -and -not $PSBoundParameters.ContainsKey('Scenario')) {
    $Scenario = 'high_order'
}
$allowedScenarios = if ($Framework) {
    @('high_order', 'emergency_review', 'material_conflict', 'no_change')
} else {
    @('rush_order', 'future_downtime', 'receipt_delay', 'no_change', 'material_shortage')
}
if (-not $Monitor -and $Scenario -notin $allowedScenarios) {
    throw 'Scenario does not match the selected framework or legacy mode.'
}

# Reads only this provider's key. Does not persist credentials or print them.
$previousLocation = Get-Location
$originalKey = $env:MAPS_LLM_API_KEY
$enteredKey = $null
try {
    Set-Location -LiteralPath $PSScriptRoot
    if (-not $PythonExecutable) {
        $bundledPython = Join-Path $env:USERPROFILE '.cache/codex-runtimes/codex-primary-runtime/dependencies/python/python.exe'
        if (Test-Path -LiteralPath $bundledPython) { $PythonExecutable = $bundledPython }
        else { $PythonExecutable = (Get-Command python -ErrorAction Stop).Source }
    }
    $env:PYTHONUTF8 = '1'
    $env:PYTHONIOENCODING = 'utf-8'
    if ([string]::IsNullOrWhiteSpace($env:MAPS_LLM_API_KEY)) {
        $enteredKey = Read-Host 'LLM API key (hidden input)' -AsSecureString
        $env:MAPS_LLM_API_KEY = [System.Net.NetworkCredential]::new('', $enteredKey).Password
    }
    if ([string]::IsNullOrWhiteSpace($env:MAPS_LLM_API_KEY)) {
        throw 'No API key provided.'
    }
    if ($Monitor) {
        $monitorArgs = @('-m', 'enterprise_agent', 'monitor-demo', '--prediction', 'llm')
        if ($Pause) { $monitorArgs += '--pause' }
        & $PythonExecutable @monitorArgs
    } elseif ($Framework) {
        & $PythonExecutable -m enterprise_agent framework-demo --agent llm --scenario $Scenario --output artifacts/maps-framework-live.json
    } else {
        & $PythonExecutable -m enterprise_agent demo --agent llm --scenario $Scenario --output artifacts/llm-live.json --summary artifacts/llm-live.md
    }
    $runExitCode = $LASTEXITCODE
}
finally {
    $env:MAPS_LLM_API_KEY = $originalKey
    if ($null -ne $enteredKey) { $enteredKey.Dispose() }
    Set-Location -LiteralPath $previousLocation.Path
}
exit $runExitCode
