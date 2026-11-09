$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
if (-not (Test-Path '.venv\Scripts\python.exe')) {
    py -3 -m venv .venv
}
& '.venv\Scripts\python.exe' -m pip install -r requirements.txt
$senha = Read-Host 'Defina a senha de acesso ao painel (minimo 12 caracteres)' -AsSecureString
$cred = [System.Net.NetworkCredential]::new('', $senha)
$env:VEYON_PANEL_PASSWORD = $cred.Password
if ($env:VEYON_PANEL_PASSWORD.Length -lt 12) {throw 'Senha com menos de 12 caracteres.'}
Write-Host 'Acesse http://127.0.0.1:5050'
& '.venv\Scripts\python.exe' app.py
Remove-Item Env:VEYON_PANEL_PASSWORD -ErrorAction SilentlyContinue
