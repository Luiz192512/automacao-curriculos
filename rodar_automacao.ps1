# Uma rodada da automação: coleta vagas, tria com os modelos, avisa no WhatsApp as aprovadas e registra tudo.
# Chamado pela tarefa agendada do Windows. As chaves (GROQ_API_KEY, GEMINI_API_KEY) vêm das variáveis do usuário.
# Nada é enviado a empresas: a candidatura em si continua sendo feita à parte.
$raiz = $PSScriptRoot
$prompts = Join-Path $raiz 'prompts'
$log = Join-Path $prompts 'saida\automacao.log'
New-Item -ItemType Directory -Force -Path (Split-Path $log) | Out-Null
$env:PYTHONIOENCODING = 'utf-8'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$env:GROQ_API_KEY = [Environment]::GetEnvironmentVariable('GROQ_API_KEY', 'User')
$env:GEMINI_API_KEY = [Environment]::GetEnvironmentVariable('GEMINI_API_KEY', 'User')

function Registrar([string]$texto) { "{0} {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $texto | Add-Content -Encoding utf8 -Path $log }

Set-Location $prompts
Registrar '--- inicio da rodada ---'
# O redirecionamento "*>>" do Windows PowerShell 5.1 grava em UTF-16; por isso a saída passa por Add-Content em UTF-8.
function Gravar-Saida { process { Add-Content -Encoding utf8 -Path $log -Value "$_" } }

Registrar 'coleta'
python coletor.py --saida dados/vagas.json --max 80 2>&1 | Gravar-Saida
Registrar ('coleta terminou com codigo {0}' -f $LASTEXITCODE)
python pipeline.py --vagas dados/vagas.json --saida saida 2>&1 | Gravar-Saida
Registrar ('pipeline terminou com codigo {0} (75 significa vagas pendentes por falta de cota)' -f $LASTEXITCODE)
python avisar_prontas.py 2>&1 | Gravar-Saida
Registrar '--- fim da rodada ---'
