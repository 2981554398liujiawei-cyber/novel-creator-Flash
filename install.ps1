param(
    [Parameter(Position=0)][string]$ProjectPath = (Get-Location).Path,
    [ValidateSet('Project','Global')][string]$Scope = 'Project',
    [switch]$Force,
    [switch]$Migrate
)
if ($Scope -eq 'Global' -and $PSBoundParameters.ContainsKey('ProjectPath')) { throw 'A positional or named ProjectPath cannot be used with -Scope Global.' }
$ErrorActionPreference='Stop'
$PythonExe=$null; $PythonPrefix=@()
foreach($candidate in @('python','python3','py')) {
  $cmd=Get-Command $candidate -ErrorAction SilentlyContinue; if(-not $cmd){continue}
  $prefix=if($candidate -eq 'py'){@('-3')}else{@()}
  & $cmd.Source @prefix -c "import sys; raise SystemExit(0 if sys.version_info >= (3,10) else 1)"
  if($LASTEXITCODE -eq 0){ $PythonExe=$cmd.Source; $PythonPrefix=$prefix; break }
}
if(-not $PythonExe){ throw 'Python 3.10+ is required (python, python3, or py -3).' }
$SourceSkill=Join-Path $PSScriptRoot '.claude\skills\novel-creator-flash'; $SourceAgents=Join-Path $PSScriptRoot '.claude\agents'
$AgentNames=@('novel-fast-writer-1.md','novel-fast-writer-2.md','novel-fast-writer-3.md','novel-fast-writer-4.md','novel-fast-writer-5.md','novel-fast-writer-6.md','novel-fast-writer-7.md','novel-fast-writer-8.md','novel-fast-writer-9.md','novel-fast-writer-10.md','novel-fast-reader-flow.md','novel-fast-reader-character.md','novel-fast-reader-hook.md','novel-fast-pure-reader.md','novel-fast-continuity-reviewer.md'); $LegacySkillNames=@('novel-creator-fast-production'); $LegacyAgentNames=@('novel-style-editor.md')
if(-not(Test-Path -LiteralPath $SourceSkill -PathType Container)){throw "Skill source not found: $SourceSkill"}
foreach($Name in $AgentNames){if(-not(Test-Path -LiteralPath (Join-Path $SourceAgents $Name)-PathType Leaf)){throw "Agent source not found: $Name"}}
$SourceRoots=Get-Item -LiteralPath $SourceSkill,$SourceAgents -Force; $AllSources=@($SourceRoots)+@(Get-ChildItem -LiteralPath $SourceSkill,$SourceAgents -Recurse -Force); $Linked=$AllSources|Where-Object{$_.LinkType -or ($_.Attributes -band [IO.FileAttributes]::ReparsePoint)}|Select-Object -First 1; if($Linked){throw "Package source contains a link: $($Linked.FullName)"}
$ClaudeRoot=if($Scope -eq 'Global'){Join-Path $HOME '.claude'}else{Join-Path (Resolve-Path -LiteralPath $ProjectPath).Path '.claude'}
$SkillsRoot=Join-Path $ClaudeRoot 'skills'; $AgentsRoot=Join-Path $ClaudeRoot 'agents'; $BackupsRoot=Join-Path $ClaudeRoot 'backups'; New-Item -ItemType Directory -Force -Path $SkillsRoot,$AgentsRoot,$BackupsRoot|Out-Null
$TargetSkill=Join-Path $SkillsRoot 'novel-creator-flash'; $Token=[DateTime]::UtcNow.ToString('yyyyMMddTHHmmssZ')+'-'+$PID; $BackupRoot=Join-Path $BackupsRoot ('novel-creator-flash-'+$Token)
$LegacyFound=New-Object System.Collections.Generic.List[string]; foreach($Name in $LegacySkillNames){if(Test-Path -LiteralPath (Join-Path $SkillsRoot $Name)){$LegacyFound.Add('skill:'+$Name)}}; foreach($Name in $LegacyAgentNames){if(Test-Path -LiteralPath (Join-Path $AgentsRoot $Name)){$LegacyFound.Add('agent:'+$Name)}}
if($LegacyFound.Count -gt 0 -and -not $Migrate){Write-Warning ('Legacy components detected and left untouched: '+($LegacyFound -join ', ')+'. Re-run with -Migrate to back them up.') }
$Conflicts=New-Object System.Collections.Generic.List[string]; if(Test-Path -LiteralPath $TargetSkill){$Conflicts.Add($TargetSkill)}; foreach($Name in $AgentNames){$p=Join-Path $AgentsRoot $Name;if(Test-Path -LiteralPath $p){$Conflicts.Add($p)}}
if($Conflicts.Count -gt 0 -and -not $Force){throw "Targets exist; use -Force to replace with backup:`n$($Conflicts -join "`n")"}
$StagingSkill=Join-Path $SkillsRoot ('.novel-creator-flash.install-'+$Token); $StagingAgents=Join-Path $AgentsRoot ('.novel-creator-flash-agents-install-'+$Token)

$InstalledSkill=$false
$InstalledAgents=New-Object System.Collections.Generic.List[string]
$MigratedSkills=New-Object System.Collections.Generic.List[string]
$MigratedAgents=New-Object System.Collections.Generic.List[string]
try{

 Copy-Item -LiteralPath $SourceSkill -Destination $StagingSkill -Recurse; New-Item -ItemType Directory -Path $StagingAgents|Out-Null; foreach($Name in $AgentNames){Copy-Item -LiteralPath (Join-Path $SourceAgents $Name)-Destination (Join-Path $StagingAgents $Name)}
 & $PythonExe @PythonPrefix -m compileall -q (Join-Path $StagingSkill 'scripts'); if($LASTEXITCODE -ne 0){throw 'Python source validation failed.'}
 Get-ChildItem -LiteralPath $StagingSkill -Directory -Recurse -Force|Where-Object{$_.Name -eq '__pycache__'}|Remove-Item -Recurse -Force; Get-ChildItem -LiteralPath $StagingSkill -File -Recurse -Force|Where-Object{$_.Extension -in @('.pyc','.pyo')}|Remove-Item -Force
 New-Item -ItemType Directory -Force -Path (Join-Path $BackupRoot 'agents'),(Join-Path $BackupRoot 'legacy-skills'),(Join-Path $BackupRoot 'legacy-agents')|Out-Null
 if(Test-Path -LiteralPath $TargetSkill){Move-Item -LiteralPath $TargetSkill -Destination (Join-Path $BackupRoot 'skill')}; foreach($Name in $AgentNames){$p=Join-Path $AgentsRoot $Name;if(Test-Path -LiteralPath $p){Move-Item -LiteralPath $p -Destination (Join-Path (Join-Path $BackupRoot 'agents') $Name)}}
 Move-Item -LiteralPath $StagingSkill -Destination $TargetSkill; $InstalledSkill=$true
 if($Scope -eq 'Project'){ $MarkerPath=Join-Path $TargetSkill '.project-root'; $MarkerValue=((Resolve-Path -LiteralPath $ProjectPath).Path)+[Environment]::NewLine; [IO.File]::WriteAllText($MarkerPath,$MarkerValue,(New-Object Text.UTF8Encoding($false))) }
 foreach($Name in $AgentNames){Move-Item -LiteralPath (Join-Path $StagingAgents $Name)-Destination (Join-Path $AgentsRoot $Name); $InstalledAgents.Add($Name)}; Remove-Item -LiteralPath $StagingAgents -Force
 if($Migrate){
   foreach($Name in $LegacySkillNames){$p=Join-Path $SkillsRoot $Name;if(Test-Path -LiteralPath $p){Move-Item -LiteralPath $p -Destination (Join-Path (Join-Path $BackupRoot 'legacy-skills') $Name); $MigratedSkills.Add($Name)}}
   foreach($Name in $LegacyAgentNames){$p=Join-Path $AgentsRoot $Name;if(Test-Path -LiteralPath $p){Move-Item -LiteralPath $p -Destination (Join-Path (Join-Path $BackupRoot 'legacy-agents') $Name); $MigratedAgents.Add($Name)}}
 }
}catch{
 if($InstalledSkill -and (Test-Path -LiteralPath $TargetSkill)){Remove-Item -LiteralPath $TargetSkill -Recurse -Force -ErrorAction SilentlyContinue}
 foreach($Name in $InstalledAgents){$p=Join-Path $AgentsRoot $Name;if(Test-Path -LiteralPath $p){Remove-Item -LiteralPath $p -Force -ErrorAction SilentlyContinue}}
 $BackupSkill=Join-Path $BackupRoot 'skill'; if(Test-Path -LiteralPath $BackupSkill){Move-Item -LiteralPath $BackupSkill -Destination $TargetSkill -Force}
 $BackupAgents=Join-Path $BackupRoot 'agents'; if(Test-Path -LiteralPath $BackupAgents){foreach($item in Get-ChildItem -LiteralPath $BackupAgents -File -Force){Move-Item -LiteralPath $item.FullName -Destination (Join-Path $AgentsRoot $item.Name) -Force}}
 foreach($Name in $MigratedSkills){$b=Join-Path (Join-Path $BackupRoot 'legacy-skills') $Name;if(Test-Path -LiteralPath $b){Move-Item -LiteralPath $b -Destination (Join-Path $SkillsRoot $Name) -Force}}
 foreach($Name in $MigratedAgents){$b=Join-Path (Join-Path $BackupRoot 'legacy-agents') $Name;if(Test-Path -LiteralPath $b){Move-Item -LiteralPath $b -Destination (Join-Path $AgentsRoot $Name) -Force}}
 throw
}finally{
 if(Test-Path -LiteralPath $StagingSkill){Remove-Item -LiteralPath $StagingSkill -Recurse -Force -ErrorAction SilentlyContinue}
 if(Test-Path -LiteralPath $StagingAgents){Remove-Item -LiteralPath $StagingAgents -Recurse -Force -ErrorAction SilentlyContinue}
}
Write-Host "Installed novel-creator-flash Skill to $TargetSkill"; Write-Host "Installed $($AgentNames.Count) subagents to $AgentsRoot"; if($Migrate -and $LegacyFound.Count -gt 0){Write-Host "Legacy components moved to backup: $BackupRoot"}; Write-Host "Python launcher validated: $PythonExe $($PythonPrefix -join ' ')"; Write-Host 'Restart Claude Code so project/user subagents are reloaded.'
