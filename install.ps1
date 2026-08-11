param(
    [ValidateSet('Project','Global')]
    [string]$Scope = 'Project',
    [string]$ProjectPath = (Get-Location).Path,
    [switch]$Force
)

$ErrorActionPreference = 'Stop'
$PythonCommand = Get-Command python -ErrorAction SilentlyContinue
if (-not $PythonCommand) { throw "Python 3.10 or newer is required and must be available as 'python'." }
& $PythonCommand.Source -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)"
if ($LASTEXITCODE -ne 0) { throw 'Python 3.10 or newer is required.' }

$SourceSkill = Join-Path $PSScriptRoot '.claude\skills\novel-creator-flash'
$SourceAgents = Join-Path $PSScriptRoot '.claude\agents'
$AgentNames = @('novel-fast-writer-1.md','novel-fast-writer-2.md','novel-fast-writer-3.md','novel-fast-writer-4.md','novel-fast-writer-5.md','novel-fast-reader-flow.md','novel-fast-reader-character.md','novel-fast-reader-hook.md')
$ObsoleteAgentNames = @('novel-style-editor.md')
if (-not (Test-Path -LiteralPath $SourceSkill -PathType Container)) { throw "Skill source not found: $SourceSkill" }
foreach ($Name in $AgentNames) {
    if (-not (Test-Path -LiteralPath (Join-Path $SourceAgents $Name) -PathType Leaf)) { throw "Agent source not found: $Name" }
}
$SourceRoots = Get-Item -LiteralPath $SourceSkill,$SourceAgents -Force
$AllSources = @($SourceRoots) + @(Get-ChildItem -LiteralPath $SourceSkill,$SourceAgents -Recurse -Force)
$LinkedSource = $AllSources | Where-Object { $_.LinkType -or ($_.Attributes -band [IO.FileAttributes]::ReparsePoint) } | Select-Object -First 1
if ($LinkedSource) { throw "Package source contains a link or reparse point: $($LinkedSource.FullName)" }

if ($Scope -eq 'Global') { $ClaudeRoot = Join-Path $HOME '.claude' }
else { $ClaudeRoot = Join-Path (Resolve-Path -LiteralPath $ProjectPath).Path '.claude' }
$SkillsRoot = Join-Path $ClaudeRoot 'skills'
$AgentsRoot = Join-Path $ClaudeRoot 'agents'
$BackupsRoot = Join-Path $ClaudeRoot 'backups'
$TargetSkill = Join-Path $SkillsRoot 'novel-creator-flash'
New-Item -ItemType Directory -Force -Path $SkillsRoot,$AgentsRoot,$BackupsRoot | Out-Null
$Stamp = [DateTime]::UtcNow.ToString('yyyyMMddTHHmmssZ')
$StagingSkill = Join-Path $SkillsRoot ('.novel-fast.install-' + $Stamp + '-' + $PID)
$StagingAgents = Join-Path $AgentsRoot ('.novel-fast-agents-install-' + $Stamp + '-' + $PID)
$BackupRoot = Join-Path $BackupsRoot ('novel-creator-flash-' + $Stamp + '-' + $PID)
$InstalledAgents = New-Object System.Collections.Generic.List[string]
$MovedAgentBackups = New-Object System.Collections.Generic.List[string]
$InstalledSkill = $false
$MovedSkillBackup = $false

try {
    Copy-Item -LiteralPath $SourceSkill -Destination $StagingSkill -Recurse
    New-Item -ItemType Directory -Path $StagingAgents | Out-Null
    foreach ($Name in $AgentNames) { Copy-Item -LiteralPath (Join-Path $SourceAgents $Name) -Destination (Join-Path $StagingAgents $Name) }
    Get-ChildItem -LiteralPath $StagingSkill -Directory -Recurse -Force | Where-Object { $_.Name -eq '__pycache__' } | Remove-Item -Recurse -Force
    Get-ChildItem -LiteralPath $StagingSkill -File -Recurse -Force | Where-Object { $_.Extension -in @('.pyc','.pyo') } | Remove-Item -Force
    & $PythonCommand.Source -m compileall -q (Join-Path $StagingSkill 'scripts')
    if ($LASTEXITCODE -ne 0) { throw 'Python source validation failed.' }
    Get-ChildItem -LiteralPath $StagingSkill -Directory -Recurse -Force | Where-Object { $_.Name -eq '__pycache__' } | Remove-Item -Recurse -Force
    Get-ChildItem -LiteralPath $StagingSkill -File -Recurse -Force | Where-Object { $_.Extension -in @('.pyc','.pyo') } | Remove-Item -Force

    $Conflicts = New-Object System.Collections.Generic.List[string]
    if (Test-Path -LiteralPath $TargetSkill) { $Conflicts.Add($TargetSkill) }
    foreach ($Name in $AgentNames) { if (Test-Path -LiteralPath (Join-Path $AgentsRoot $Name)) { $Conflicts.Add((Join-Path $AgentsRoot $Name)) } }
    foreach ($Name in $ObsoleteAgentNames) { if (Test-Path -LiteralPath (Join-Path $AgentsRoot $Name)) { $Conflicts.Add((Join-Path $AgentsRoot $Name)) } }
    if ($Conflicts.Count -gt 0 -and -not $Force) { throw ("Targets exist; use -Force to replace with backup:`n" + ($Conflicts -join "`n")) }

    $NeedBackup = (Test-Path -LiteralPath $TargetSkill)
    foreach ($Name in $AgentNames) { if (Test-Path -LiteralPath (Join-Path $AgentsRoot $Name)) { $NeedBackup = $true } }
    foreach ($Name in $ObsoleteAgentNames) { if (Test-Path -LiteralPath (Join-Path $AgentsRoot $Name)) { $NeedBackup = $true } }
    if ($NeedBackup) { New-Item -ItemType Directory -Force -Path (Join-Path $BackupRoot 'agents') | Out-Null }
    if (Test-Path -LiteralPath $TargetSkill) {
        Move-Item -LiteralPath $TargetSkill -Destination (Join-Path $BackupRoot 'skill')
        $MovedSkillBackup = $true
    }
    foreach ($Name in $AgentNames) {
        $TargetAgent = Join-Path $AgentsRoot $Name
        if (Test-Path -LiteralPath $TargetAgent) {
            Move-Item -LiteralPath $TargetAgent -Destination (Join-Path (Join-Path $BackupRoot 'agents') $Name)
            $MovedAgentBackups.Add($Name)
        }
    }
    foreach ($Name in $ObsoleteAgentNames) {
        $TargetAgent = Join-Path $AgentsRoot $Name
        if (Test-Path -LiteralPath $TargetAgent) {
            Move-Item -LiteralPath $TargetAgent -Destination (Join-Path (Join-Path $BackupRoot 'agents') $Name)
            $MovedAgentBackups.Add($Name)
        }
    }

    Move-Item -LiteralPath $StagingSkill -Destination $TargetSkill
    $InstalledSkill = $true
    foreach ($Name in $AgentNames) {
        Move-Item -LiteralPath (Join-Path $StagingAgents $Name) -Destination (Join-Path $AgentsRoot $Name)
        $InstalledAgents.Add($Name)
    }
    Remove-Item -LiteralPath $StagingAgents -Force
} catch {
    if ($InstalledSkill -and (Test-Path -LiteralPath $TargetSkill)) { Remove-Item -LiteralPath $TargetSkill -Recurse -Force -ErrorAction SilentlyContinue }
    foreach ($Name in $InstalledAgents) { $P = Join-Path $AgentsRoot $Name; if (Test-Path -LiteralPath $P) { Remove-Item -LiteralPath $P -Force -ErrorAction SilentlyContinue } }
    if ($MovedSkillBackup) { $P = Join-Path $BackupRoot 'skill'; if ((Test-Path -LiteralPath $P) -and -not (Test-Path -LiteralPath $TargetSkill)) { Move-Item -LiteralPath $P -Destination $TargetSkill } }
    foreach ($Name in $MovedAgentBackups) { $P = Join-Path (Join-Path $BackupRoot 'agents') $Name; $T = Join-Path $AgentsRoot $Name; if ((Test-Path -LiteralPath $P) -and -not (Test-Path -LiteralPath $T)) { Move-Item -LiteralPath $P -Destination $T } }
    if (Test-Path -LiteralPath $StagingSkill) { Remove-Item -LiteralPath $StagingSkill -Recurse -Force -ErrorAction SilentlyContinue }
    if (Test-Path -LiteralPath $StagingAgents) { Remove-Item -LiteralPath $StagingAgents -Recurse -Force -ErrorAction SilentlyContinue }
    throw
}

Write-Host "Installed novel-creator-flash Skill to $TargetSkill"
Write-Host "Installed 8 rapid-production subagents to $AgentsRoot"
if ($MovedSkillBackup -or $MovedAgentBackups.Count -gt 0) { Write-Host "Previous files backup: $BackupRoot" }
Write-Host 'Restart Claude Code so the project or user subagents are loaded.'
