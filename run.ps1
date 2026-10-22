# Script de lancement rapide du Mandataire Immobilier Virtuel (.venv local)
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if (-not (Test-Path ".\.venv\Scripts\streamlit.exe")) {
    Write-Host "Installation des dépendances dans .venv..." -ForegroundColor Cyan
    .\.venv\Scripts\pip.exe install -r requirements.txt
}

Write-Host "Démarrage du Cockpit Streamlit..." -ForegroundColor Green
.\.venv\Scripts\streamlit.exe run app.py
