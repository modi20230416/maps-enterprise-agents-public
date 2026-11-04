param(
    [ValidateSet('api','ui','test')][string]$Role = 'api',
    [switch]$WithFixtures
)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$platformPython = Join-Path $PSScriptRoot '.venv/Scripts/python.exe'
if (-not (Test-Path -LiteralPath $platformPython)) {
    throw 'Please create .venv and install .[web,web-test] first. See docs/13-platform-runtime.md.'
}
$env:PYTHONUTF8 = '1'
if ($Role -eq 'test') {
    & $platformPython -m unittest discover -s tests -p 'test_platform*.py' -v
} elseif ($Role -eq 'ui') {
    & $platformPython -m enterprise_agent platform-ui
} elseif ($WithFixtures) {
    & $platformPython -m enterprise_agent platform-api --allow-protocol-fixture
} else {
    & $platformPython -m enterprise_agent platform-api
}
exit $LASTEXITCODE
