<#
.SYNOPSIS
    One-time setup: push the collector to CEZ083 using existing SSH key auth.
.DESCRIPTION
    Reads connection details from deploy.conf (gitignored).
    Run from the repo root:  powershell -ExecutionPolicy Bypass -File scripts\setup_git_remote.ps1
#>
param()

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot

# ---------------------------------------------------------------------------
# Parse deploy.conf
# ---------------------------------------------------------------------------
$confPath = Join-Path $root "deploy.conf"
if (-not (Test-Path $confPath)) {
    Write-Error "deploy.conf not found. Copy deploy.conf.example to deploy.conf and fill in values."
    exit 1
}

$conf = @{}
Get-Content $confPath | Where-Object { $_ -match '^\s*[A-Z]' } | ForEach-Object {
    $parts = $_ -split '=', 2
    if ($parts.Count -eq 2) { $conf[$parts[0].Trim()] = $parts[1].Trim() }
}

$sshHost  = $conf['SERVER_HOST']
$sshPort  = $conf['SERVER_PORT']
$sshUser  = $conf['SERVER_USER']
$barerepo = $conf['SERVER_BAREREPO']

if (-not ($sshHost -and $sshPort -and $sshUser -and $barerepo)) {
    Write-Error "deploy.conf is incomplete. Required: SERVER_HOST, SERVER_PORT, SERVER_USER, SERVER_BAREREPO"
    exit 1
}

$sshTarget = "${sshUser}@${sshHost}"
$gitRemote = "ssh://${sshUser}@${sshHost}:${sshPort}${barerepo}"
$sshBatchArgs = @("-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=8", "-p", $sshPort)
$sshInteractiveArgs = @("-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=8", "-p", $sshPort)
$sshArgs = $sshBatchArgs

# ---------------------------------------------------------------------------
# Step 1: SSH authentication preflight
# ---------------------------------------------------------------------------
Write-Host "`n[Step 1] Verifying SSH authentication..."
$prevErrorActionPreference = $ErrorActionPreference
$ErrorActionPreference = "Continue"
& ssh @sshBatchArgs $sshTarget "exit" *> $null 2>$null
$sshProbeExitCode = $LASTEXITCODE
$ErrorActionPreference = $prevErrorActionPreference
if ($sshProbeExitCode -eq 0) {
        Write-Host "  Non-interactive SSH auth OK."
        $sshArgs = $sshBatchArgs
} else {
        Write-Host "  Non-interactive SSH auth unavailable. Falling back to interactive SSH prompts."
        $sshArgs = $sshInteractiveArgs
}

# ---------------------------------------------------------------------------
# Step 1.5: Install system packages required for venv/pip (python3-venv, python3-pip)
# ---------------------------------------------------------------------------
Write-Host "`n[Step 1.5] Ensuring python3-venv and python3-pip are installed on server..."
$prevEAP = $ErrorActionPreference; $ErrorActionPreference = "Continue"
$pkgCount = & ssh @sshArgs $sshTarget "dpkg -l python3-venv python3-pip 2>/dev/null | grep -c '^ii'" 2>$null
$ErrorActionPreference = $prevEAP
if (($pkgCount -as [int]) -ge 2) {
    Write-Host "  Already installed, skipping."
} else {
    Write-Host "  Packages missing (python3-venv / python3-pip). Sudo required."
    $sudoPassSec = Read-Host "  Sudo password for ${sshUser}@${sshHost}" -AsSecureString
    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($sudoPassSec)
    try   { $plain = [Runtime.InteropServices.Marshal]::PtrToStringAuto($bstr) }
    finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) }
    $prevEAP = $ErrorActionPreference; $ErrorActionPreference = "Continue"
    & ssh @sshArgs $sshTarget "echo '$plain' | sudo -S apt-get install -y -qq python3-venv python3-pip 2>&1"
    $aptCode = $LASTEXITCODE
    $ErrorActionPreference = $prevEAP
    $plain = $null
    if ($aptCode -ne 0) { Write-Warning "apt-get install returned exit code $aptCode - continuing." }
    else { Write-Host "  python3-venv and python3-pip installed." }
}

# ---------------------------------------------------------------------------
# Step 2: Initialise server (bare repo + hook + linger)
# ---------------------------------------------------------------------------
Write-Host "`n[Step 2] Initialising server bare repo and hook..."
# Write with guaranteed LF endings via .NET, then scp to avoid PowerShell pipe CRLF issues.
$serverInitScript = (Get-Content (Join-Path $PSScriptRoot "server_init.sh") -Raw) -replace "`r`n","`n" -replace "`r","`n"
$tmpInit = [System.IO.Path]::GetTempFileName() + ".sh"
[System.IO.File]::WriteAllBytes($tmpInit, [System.Text.Encoding]::UTF8.GetBytes($serverInitScript))
$scpArgs = @("-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=8", "-P", $sshPort)
$prevErrorActionPreference = $ErrorActionPreference
$ErrorActionPreference = "Continue"
& scp @scpArgs $tmpInit "${sshTarget}:/tmp/hkust_server_init_$PID.sh"
$scpExitCode = $LASTEXITCODE
$ErrorActionPreference = $prevErrorActionPreference
Remove-Item $tmpInit -Force -ErrorAction SilentlyContinue
if ($scpExitCode -ne 0) {
    Write-Error "scp of server_init.sh failed with exit code $scpExitCode"
    exit 1
}
$prevErrorActionPreference = $ErrorActionPreference
$ErrorActionPreference = "Continue"
& ssh @sshArgs $sshTarget "bash /tmp/hkust_server_init_$PID.sh; rm -f /tmp/hkust_server_init_$PID.sh"
$sshInitExitCode = $LASTEXITCODE
$ErrorActionPreference = $prevErrorActionPreference
if ($sshInitExitCode -ne 0) {
    Write-Error "Server initialization failed with exit code $sshInitExitCode"
    exit 1
}

# ---------------------------------------------------------------------------
# Step 3: Add / update git remote
# ---------------------------------------------------------------------------
Write-Host "`n[Step 3] Configuring git remote 'origin'..."
Push-Location $root
try {
    $remotes = (git remote 2>&1) -join "`n"
    if ($remotes -match '\borigin\b') {
        $currentUrl = (git remote get-url origin 2>&1)
        if ($currentUrl -ne $gitRemote) {
            git remote set-url origin $gitRemote
            Write-Host "  Updated remote 'origin' -> $gitRemote"
        } else {
            Write-Host "  Remote 'origin' already correct."
        }
    } else {
        git remote add origin $gitRemote
        Write-Host "  Added remote 'origin' -> $gitRemote"
    }

    # ---------------------------------------------------------------------------
    # Step 4: Ensure at least one commit exists, then push
    # ---------------------------------------------------------------------------
    Write-Host "`n[Step 4] Committing local changes and pushing..."
    cmd /c "git rev-parse --verify HEAD >NUL 2>NUL"
    $hasHead = ($LASTEXITCODE -eq 0)

    if (-not $hasHead) {
        git add -A
        $staged = (git diff --cached --name-only)
        if (-not $staged) {
            Write-Error "No files to commit for initial push."
            exit 1
        }
        git commit -m "Initial commit"
    } else {
        $pending = (git status --porcelain)
        if ($pending) {
            git add -A
            git commit -m "Pre-push commit"
        }
    }

    $branch = (git branch --show-current).Trim()
    if (-not $branch) {
        $branch = "main"
        git branch -M $branch
    }

    $prevErrorActionPreference = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    git push -u origin $branch
    $pushExitCode = $LASTEXITCODE
    $ErrorActionPreference = $prevErrorActionPreference
    if ($pushExitCode -ne 0) {
        Write-Warning "git push exited $pushExitCode - hook errors shown above, but ref was updated."
    } else {
        Write-Host "  Pushed branch '$branch' to origin."
    }
} finally {
    Pop-Location
}

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
Write-Host "`n============================================================"
Write-Host " Setup complete."
Write-Host " Remote  : $gitRemote"
Write-Host " Push cmd: git push origin $branch"
Write-Host " Service : ssh -p $sshPort ${sshTarget} 'systemctl --user status ev-collector'"
Write-Host " Edit env: ssh -p $sshPort ${sshTarget} 'nano ~/hkust-ev-collector/.env'"
Write-Host "============================================================`n"
