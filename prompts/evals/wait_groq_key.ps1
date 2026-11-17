# Espera a chave GROQ_API_KEY ser salva com setx e lista os modelos disponiveis na conta.
# O valor da chave nunca e impresso.
$out = Join-Path $PSScriptRoot 'groq_modelos.txt'
$deadline = (Get-Date).AddHours(3)

while (-not [Environment]::GetEnvironmentVariable('GROQ_API_KEY', 'User')) {
    if ((Get-Date) -gt $deadline) {
        Write-Output 'TIMEOUT: a chave do Groq nao foi salva em 3 horas.'
        exit 2
    }
    Start-Sleep -Seconds 20
}

$key = [Environment]::GetEnvironmentVariable('GROQ_API_KEY', 'User')
try {
    $response = Invoke-RestMethod -Uri 'https://api.groq.com/openai/v1/models' -Headers @{ Authorization = "Bearer $key"; 'User-Agent' = 'run-evals/1.0' } -TimeoutSec 60
    $response.data | Sort-Object id | ForEach-Object { "{0} | contexto={1} | ativo={2}" -f $_.id, $_.context_window, $_.active } | Set-Content -Path $out -Encoding utf8
    Write-Output ("Chave do Groq valida. {0} modelos listados em {1}" -f @($response.data).Count, $out)
    exit 0
} catch {
    Write-Output ("Chave do Groq salva, mas a listagem falhou: HTTP {0}" -f $_.Exception.Response.StatusCode.value__)
    exit 3
}
