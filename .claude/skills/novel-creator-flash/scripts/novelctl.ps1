$ErrorActionPreference = 'Stop'
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Target = Join-Path $ScriptDir 'novelctl.py'
$candidates = @(@{Command='python';Prefix=@()},@{Command='python3';Prefix=@()},@{Command='py';Prefix=@('-3')})
foreach ($candidate in $candidates) { $cmd=Get-Command $candidate.Command -ErrorAction SilentlyContinue; if(-not $cmd){continue}; & $cmd.Source @($candidate.Prefix) -c "import sys; raise SystemExit(0 if sys.version_info >= (3,10) else 1)"; if($LASTEXITCODE -eq 0){ & $cmd.Source @($candidate.Prefix) $Target @args; exit $LASTEXITCODE } }
throw 'Novel Creator requires Python 3.10+ (python, python3, or py -3).'
