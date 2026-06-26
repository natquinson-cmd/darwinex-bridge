# Crée la tâche planifiée du pont sur le VPS.
# Garanties : démarrage à l'ouverture de session, UNE SEULE instance,
# redémarrage automatique en cas de plantage, sans fenêtre (pythonw).

$ErrorActionPreference = "Stop"
$dir = $PSScriptRoot

# pythonw.exe = Python sans console (invisible). Repli sur python.exe si absent.
$pythonw = (Get-Command pythonw.exe -ErrorAction SilentlyContinue).Source
if (-not $pythonw) { $pythonw = (Get-Command python.exe).Source }

$action  = New-ScheduledTaskAction -Execute $pythonw -Argument "bridge_ig_mt5.py" -WorkingDirectory $dir
$trigger = New-ScheduledTaskTrigger -AtLogOn
$settings = New-ScheduledTaskSettingsSet `
    -MultipleInstances IgnoreNew `
    -RestartCount 99 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable

Register-ScheduledTask -TaskName "PontDarwinex" `
    -Action $action -Trigger $trigger -Settings $settings `
    -Description "Pont IG -> Darwinex Zero (replication des trades)" -Force | Out-Null

Write-Host ""
Write-Host "Tache planifiee 'PontDarwinex' creee :" -ForegroundColor Green
Write-Host "  - demarre a chaque ouverture de session"
Write-Host "  - UNE seule instance (IgnoreNew)"
Write-Host "  - redemarrage auto toutes les 1 min si plantage"
Write-Host "  - sans fenetre (pythonw)"
Write-Host ""
Write-Host "Pour la lancer maintenant sans attendre :" -ForegroundColor Yellow
Write-Host "  Start-ScheduledTask -TaskName PontDarwinex"
