"""Medidor da cota do Claude (janela de 5 horas e semana), lido do evento rate_limit_event do stream-json.

Fonte verificada em 17/11/2026: `claude -p ... --output-format stream-json --verbose` emite
{"type":"rate_limit_event","rate_limit_info":{"unifiedWindows":{"five_hour":{"utilization":0.17,"resetsAt":epoch,"status":"allowed"},"seven_day":{...}}}}.
O valor de `status` quando o limite estoura NÃO foi verificado: aqui vale "cheio" se utilization >= 1 ou status que não comece com "allowed".
Desenho: saida/analise_aplicar_whatsapp.md (seção 7).
"""

import datetime as dt
import json
import os
import pathlib
import shutil
import subprocess
import time

JANELAS = (("cinco_horas", "five_hour", "5 horas"), ("semana", "seven_day", "semana"))


def evento(linha: str):
    """Devolve rate_limit_info se a linha do stream for um rate_limit_event, senão None."""
    try:
        obj = json.loads(linha)
    except ValueError:
        return None
    if isinstance(obj, dict) and obj.get("type") == "rate_limit_event":
        return obj.get("rate_limit_info") or {}
    return None


def bloqueio(info: dict):
    """None se há folga. Se alguma janela está cheia: {"janela": rótulo, "volta": epoch ou None}."""
    pior = None
    for _, chave, rotulo in JANELAS:
        j = (info.get("unifiedWindows") or {}).get(chave) or {}
        cheia = float(j.get("utilization") or 0) >= 1 or (j.get("status") and not str(j["status"]).startswith("allowed"))
        if cheia:
            volta = j.get("resetsAt") or None
            if pior is None or (volta or 0) > (pior["volta"] or 0):
                pior = {"janela": rotulo, "volta": volta}
    return pior


def hora(epoch: float, agora: float) -> str:
    """'às 21:40 (em 2h13)' ou '23/11 às 05:00 (em 5 dias)'."""
    alvo = dt.datetime.fromtimestamp(epoch)
    falta = epoch - agora
    if falta <= 0:
        rel = "agora"
    elif falta < 3600:
        rel = f"em {int(falta // 60)} min"
    elif falta < 86400:
        rel = f"em {int(falta // 3600)}h{int(falta % 3600 // 60):02d}"
    else:
        rel = f"em {round(falta / 86400)} dias"
    dia = "" if alvo.date() == dt.datetime.fromtimestamp(agora).date() else f"{alvo:%d/%m} "
    return f"{dia}às {alvo:%H:%M} ({rel})"


def registrar(cota: dict, info: dict, agora: float) -> list:
    """Atualiza `cota` com a leitura e devolve os avisos novos: lista de (prioridade, texto).

    Cada degrau de 10% é anunciado uma vez por janela; se o uso pular vários degraus, anuncia o mais alto.
    Primeira leitura: uma linha de base, sem anunciar os degraus que já passaram.
    """
    avisos, base = [], []
    for nome, chave, rotulo in JANELAS:
        j = (info.get("unifiedWindows") or {}).get(chave)
        if not j or j.get("utilization") is None:
            continue
        uso, volta = float(j["utilization"]), j.get("resetsAt")
        c = cota.setdefault(nome, {})
        degrau = min(100, int(uso * 10 + 1e-9) * 10)
        anterior = c.get("utilizacao")
        if "ultimo_degrau" not in c:
            base.append((rotulo, uso, volta))
        elif volta and c.get("resets_at") and volta > c["resets_at"]:
            avisos.append((3, f"Cota de {rotulo} renovada. Uso agora: {round(uso * 100)}%."))
        elif degrau > c["ultimo_degrau"]:
            salto = f" (subiu de {round(anterior * 100)}% para {round(uso * 100)}% desde a última leitura)" if anterior is not None and degrau - c["ultimo_degrau"] > 10 else ""
            outra = "semana" if nome == "cinco_horas" else "5 horas"
            o = cota.get("semana" if nome == "cinco_horas" else "cinco_horas", {})
            resto = f" {outra.capitalize()}: {round(o['utilizacao'] * 100)}%." if o.get("utilizacao") is not None else ""
            avisos.append((2, f"Uso ({rotulo}): {round(uso * 100)}%{salto}. Volta {hora(volta, agora) if volta else 'em horário não informado'}.{resto}"))
        c.update(utilizacao=uso, resets_at=volta, ultimo_degrau=degrau)
    cota["lido_em"], cota["fonte"] = agora, "rate_limit_event"
    if base:
        partes = [f"{r} {round(u * 100)}%" + (f", volta {hora(v, agora)}" if v else "") for r, u, v in base]
        avisos.insert(0, (2, "Uso agora: " + "; ".join(partes) + "."))
    return avisos


def resumo(cota: dict, agora: float) -> str:
    partes = []
    for nome, _, rotulo in JANELAS:
        c = cota.get(nome) or {}
        if c.get("utilizacao") is not None:
            partes.append(f"{rotulo} {round(c['utilizacao'] * 100)}%" + (f", volta {hora(c['resets_at'], agora)}" if c.get("resets_at") else ""))
    if not partes:
        return "Ainda não tenho uma leitura de uso. Ela aparece na próxima execução do robô."
    idade = int((agora - cota.get("lido_em", agora)) // 60)
    return "Uso: " + "; ".join(partes) + f". Leitura de {idade} min atrás ({cota.get('fonte', 'rate_limit_event')})."


def carregar(caminho: pathlib.Path) -> dict:
    try:
        return json.loads(caminho.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def salvar(caminho: pathlib.Path, cota: dict) -> None:
    caminho.parent.mkdir(parents=True, exist_ok=True)
    tmp = caminho.with_name(caminho.name + ".tmp")
    tmp.write_text(json.dumps(cota, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, caminho)


def sonda(timeout: int = 90):
    """Uma chamada mínima só para ler o medidor. CONSOME COTA: o custo real ainda não foi medido (risco R5)."""
    exe = shutil.which("claude")
    if not exe:
        return None
    cmd = [exe, "-p", "ok", "--model", "haiku", "--no-session-persistence", "--output-format", "stream-json", "--verbose",
           "--disable-slash-commands", "--strict-mcp-config"]
    try:
        saida = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, encoding="utf-8", errors="replace",
                               creationflags=0x08000000 if os.name == "nt" else 0).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    achado = None
    for linha in saida.splitlines():
        achado = evento(linha) or achado
    return achado
