<#
.SYNOPSIS
    One-time setup: configure SSH key auth on CEZ083 and push the collector.
.DESCRIPTION
    Reads connection details from deploy.conf (gitignored).
    Run from the repo root:  powershell -ExecutionPolicy Bypass -File scripts\setup_git_remote.ps1
.PARAMETER SkipKeySetup
    Skip SSH key generation and copy (use if keys are already installed).
#>
param([switch]$SkipKeySetup)

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

# ---------------------------------------------------------------------------
# Step 1: SSH key setup (one-time password prompt)
# ---------------------------------------------------------------------------
if (-not $SkipKeySetup) {
    Write-Host "`n[Step 1] SSH key setup"
    $keyFile = "$HOME\.ssh\id_ed25519"
    if (-not (Test-Path "$keyFile.pub")) {
        Write-Host "  Generating ed25519 key pair..."
        New-Item -ItemType Directory -Force -Path "$HOME\.ssh" | Out-Null
        # Feed two newlines to accept empty passphrase and confirmation.
        "`n`n" | ssh-keygen -t ed25519 -f "$keyFile"
        if ($LASTEXITCODE -ne 0) {
            Write-Error "ssh-keygen failed with exit code $LASTEXITCODE"
            exit 1
        }
    } else {
        Write-Host "  Key already exists: $keyFile.pub"
    }
    if (-not (Test-Path "$keyFile.pub")) {
        Write-Error "SSH key generation failed. Public key not found at $keyFile.pub"
        exit 1
    }
    $pubKey = (Get-Content "$keyFile.pub" -Raw).Trim()
    $pubKey = $pubKey -replace "'", ""   # strip any stray quotes for safety
    Write-Host "  Copying public key to server (you will be prompted for the server password once)..."
    ssh -p $sshPort -o StrictHostKeyChecking=accept-new $sshTarget `
        "mkdir -p ~/.ssh && printf '%s\n' '$pubKey' >> ~/.ssh/authorized_keys && sort -u ~/.ssh/authorized_keys -o ~/.ssh/authorized_keys && chmod 700 ~/.ssh && chmod 600 ~/.ssh/authorized_keys"
    Write-Host "  SSH key installed. Future connections will be passwordless."
} else {
    Write-Host "`n[Step 1] SSH key setup skipped (--SkipKeySetup)"
}

# ---------------------------------------------------------------------------
# Step 2: Initialise server (bare repo + hook + linger)
# ---------------------------------------------------------------------------
Write-Host "`n[Step 2] Initialising server bare repo and hook..."
$serverInitScript = Get-Content (Join-Path $PSScriptRoot "server_init.sh") -Raw
$serverInitScript | ssh -p $sshPort -o StrictHostKeyChecking=accept-new $sshTarget "bash -s"

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

    git push -u origin $branch
    Write-Host "  Pushed branch '$branch' to origin."
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
