param(
    [ValidateSet('high_order', 'emergency_review', 'material_conflict', 'no_change')]
    [string]$Scenario = 'high_order'
)

# Configure the provider/model first; credentials remain in the child environment.
$previousLocation = Get-Location
$originalKey = $env:MAPS_LLM_API_KEY
$enteredKey = $null
try {
    Set-Location -LiteralPath $PSScriptRoot
    if ([string]::IsNullOrWhiteSpace($env:MAPS_LLM_API_KEY)) {
        $enteredKey = Read-Host 'LLM API key (hidden input)' -AsSecureString
        $env:MAPS_LLM_API_KEY = [System.Net.NetworkCredential]::new('', $enteredKey).Password
    }
    if ([string]::IsNullOrWhiteSpace($env:MAPS_LLM_API_KEY)) {
        throw 'No API key provided.'
    }
    python -m enterprise_agent framework-demo --agent llm --scenario $Scenario --output artifacts/maps-framework-live.json
    $runExitCode = $LASTEXITCODE
}
finally {
    $env:MAPS_LLM_API_KEY = $originalKey
    if ($null -ne $enteredKey) { $enteredKey.Dispose() }
    Set-Location -LiteralPath $previousLocation.Path
}
exit $runExitCode
