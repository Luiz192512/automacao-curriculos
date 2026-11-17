# Roda o pipeline e repete enquanto houver vagas pendentes por falta de cota ou instabilidade.
# O estado fica em saida\estado.json, então cada repetição continua de onde a anterior parou.
# As chaves são lidas das variáveis do usuário (setx) e nunca são impressas.
# Uso: powershell -File rodar_pipeline.ps1 [-Vagas dados\vagas.json] [-Horas 6] [-EsperaMinutos 20]
param(
    [string]$Vagas = 'dados\vagas.json',
    [double]$Horas = 6,
    [int]$EsperaMinutos = 20,
    [string]$Saida = 'saida',
    [switch]$SoGroq
)
$pasta = $PSScriptRoot
Set-Location $pasta
$env:GROQ_API_KEY = [Environment]::GetEnvironmentVariable('GROQ_API_KEY', 'User')
$env:GEMINI_API_KEY = [Environment]::GetEnvironmentVariable('GEMINI_API_KEY', 'User')
$env:PYTHONIOENCODING = 'utf-8'
$limite = (Get-Date).AddHours($Horas)
$rodada = 0
while ($true) {
    $rodada++
    Write-Output ("--- rodada {0} as {1} ---" -f $rodada, (Get-Date -Format 'HH:mm:ss'))
    if ($SoGroq) { python pipeline.py --vagas $Vagas --saida $Saida --so-groq } else { python pipeline.py --vagas $Vagas --saida $Saida }
    $codigo = $LASTEXITCODE
    if ($codigo -ne 75) { Write-Output ("pipeline terminou com codigo {0}" -f $codigo); exit $codigo }
    if ((Get-Date) -gt $limite) { Write-Output 'Prazo maximo atingido com vagas ainda pendentes.'; exit 75 }
    Write-Output ("Vagas pendentes. Nova tentativa em {0} minutos." -f $EsperaMinutos)
    Start-Sleep -Seconds ($EsperaMinutos * 60)
}
