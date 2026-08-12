$ErrorActionPreference = 'Stop'
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Candidates = @(
  @{ Name = 'python'; Args = @() },
  @{ Name = 'python3'; Args = @() },
  @{ Name = 'py'; Args = @('-3') }
)
foreach ($candidate in $Candidates) {
  $cmd = Get-Command $candidate.Name -ErrorAction SilentlyContinue
  if (-not $cmd) { continue }
  & $candidate.Name @($candidate.Args) -c "import sys; raise SystemExit(0 if sys.version_info >= (3,10) else 1)" | Out-Null
  if ($LASTEXITCODE -eq 0) {
    & $candidate.Name @($candidate.Args) (Join-Path $ScriptDir 'novelctl_skill.py') @args
    exit $LASTEXITCODE
  }
}
Write-Error 'Novel Creator requires Python 3.10+ (python, python3, or py -3).'
exit 127
