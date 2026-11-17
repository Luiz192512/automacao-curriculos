# Roda v3 e v4 no Groq usando a chave salva com setx (escopo do usuario). O valor da chave nunca e impresso.
# Uso: powershell -File run_groq_task.ps1 [modelo]
param(
    [string]$Model = 'openai/gpt-oss-120b',
    [string]$Out = 'evals\resultados_groq.json',
    [string[]]$Prompts = @('automacao-candidaturas-rag.v3.md', 'automacao-candidaturas-rag.md')
)
$promptsDir = Split-Path -Parent $PSScriptRoot
$env:GROQ_API_KEY = [Environment]::GetEnvironmentVariable('GROQ_API_KEY', 'User')
$env:PYTHONIOENCODING = 'utf-8'
Set-Location $promptsDir
python evals/run_evals.py --provider groq --model $Model --prompts @Prompts --out $Out --resume *> evals\groq_console.txt
exit $LASTEXITCODE
