# Executado pelo Agendador de Tarefas do Windows (tarefa "AvaliacaoPromptCandidaturas").
# Le a chave salva com setx no escopo do usuario e roda a avaliacao completa sem esperar horario.
$prompts = Split-Path -Parent $PSScriptRoot
$env:GEMINI_API_KEY = [Environment]::GetEnvironmentVariable('GEMINI_API_KEY', 'User')
$env:PYTHONIOENCODING = 'utf-8'
Set-Location $prompts
python evals/overnight.py --start-now *> evals\overnight_console.txt
