"""Testes da fila de candidaturas e do medidor de cota, com worker simulado. Rode: python evals/test_aplicar.py"""
import json
import os
import pathlib
import sys
import tempfile
import time

AQUI = pathlib.Path(__file__).resolve().parent
RAIZ = tempfile.mkdtemp(prefix="aplicar_teste_")
os.environ["AUTOMACAO_SAIDA"] = f"{RAIZ}/saida"
os.environ["AUTOMACAO_VAGAS"] = f"{RAIZ}/vagas.json"
os.environ["AUTOMACAO_APROVADAS"] = f"{RAIZ}/aprovadas.json"
sys.path.insert(0, str(AQUI.parent))
import aplicar_fila as f  # noqa: E402
import cota  # noqa: E402

TRAVESSOS = [chr(0x2014), chr(0x2013)]


def mensagens() -> list:
    ms = [json.loads(p.read_text(encoding="utf-8"))["mensagem"] for p in sorted(f.OUTBOX.glob("*.json"))] if f.OUTBOX.exists() else []
    for p in f.OUTBOX.glob("*.json") if f.OUTBOX.exists() else []:
        p.unlink()
    assert not any(t in m for m in ms for t in TRAVESSOS), "travessão em mensagem"
    return ms


def novo_cenario(cenario: str, **cfg) -> None:
    for pasta in (f.FILA, f.PERG, f.RES, f.DIA):
        for a in pasta.glob("*") if pasta.exists() else []:
            a.unlink()
    for a in (f.COTA, f.AP / "seq.json"):
        a.unlink(missing_ok=True)
    os.environ["FAKE_CENARIO"] = cenario
    f.gravar(f.CONFIG, {"ativo": True, "carencia_s": 0, "comando_worker": [sys.executable, str(AQUI / "fake_worker.py")], **cfg})
    f.gravar(f.VAGAS, [{"id": "gupy-1", "titulo": "Dev Node", "empresa": "La Moda", "url": "https://x", "fonte": "Gupy"}])
    f.gravar(f.ESTADO, {"vagas": {"gupy-1": {"status": "revisao_manual", "candidatura": None}}})
    mensagens()


def estado_pedido(pid=1) -> str:
    return f.carregar(pid)["estado"]


def candidatura() -> str:
    return f.ler(f.ESTADO, {})["vagas"]["gupy-1"]["candidatura"]


# ---------- cota.py ----------
ev = json.loads((AQUI / "fixtures" / "rate_limit_event.json").read_text(encoding="utf-8"))
info = cota.evento(json.dumps(ev))
assert info["unifiedWindows"]["five_hour"]["utilization"] == 0.17
assert cota.evento("lixo") is None and cota.evento('{"type":"result"}') is None
assert cota.bloqueio(info) is None

agora = ev["rate_limit_info"]["resetsAt"] - 3 * 3600
c = {}
av = cota.registrar(c, info, agora)
assert len(av) == 1 and "5 horas 17%" in av[0][1] and "semana 48%" in av[0][1], av  # linha de base, sem degraus antigos
assert c["cinco_horas"]["ultimo_degrau"] == 10 and c["semana"]["ultimo_degrau"] == 40
assert cota.registrar(c, info, agora) == []  # mesma leitura: nada repetido

def com_uso(cinco, semana, volta5=None):
    e = json.loads(json.dumps(info))
    e["unifiedWindows"]["five_hour"].update(utilization=cinco, resetsAt=volta5 or info["unifiedWindows"]["five_hour"]["resetsAt"])
    e["unifiedWindows"]["seven_day"]["utilization"] = semana
    return e

av = cota.registrar(c, com_uso(0.25, 0.48), agora)
assert len(av) == 1 and "Uso (5 horas): 25%" in av[0][1] and "Semana: 48%" in av[0][1], av
av = cota.registrar(c, com_uso(0.29, 0.48), agora)
assert av == []  # ainda no degrau 20
av = cota.registrar(c, com_uso(0.71, 0.48), agora)
assert len(av) == 1 and "70%" not in av[0][1] and "71%" in av[0][1] and "subiu de 29% para 71%" in av[0][1], av
av = cota.registrar(c, com_uso(0.05, 0.48, info["unifiedWindows"]["five_hour"]["resetsAt"] + 18000), agora)
assert len(av) == 1 and "renovada" in av[0][1], av  # nova janela: zera sem anunciar degraus
assert c["cinco_horas"]["ultimo_degrau"] == 0
av = cota.registrar(c, com_uso(0.12, 0.48, info["unifiedWindows"]["five_hour"]["resetsAt"] + 18000), agora)
assert len(av) == 1 and "12%" in av[0][1]

cheio = json.loads(json.dumps(info))
cheio["unifiedWindows"]["five_hour"].update(utilization=1.0, status="rejected")
b = cota.bloqueio(cheio)
assert b == {"janela": "5 horas", "volta": info["unifiedWindows"]["five_hour"]["resetsAt"]}, b
sem_hora = json.loads(json.dumps(cheio))
del sem_hora["unifiedWindows"]["five_hour"]["resetsAt"]
assert cota.bloqueio(sem_hora)["volta"] is None
assert "17%" in cota.resumo(c := {} or cota.carregar(pathlib.Path(RAIZ) / "nada.json") or {"cinco_horas": {"utilizacao": 0.17, "resets_at": agora + 3600}, "lido_em": agora}, agora)
print("cota ok")

# ---------- fila ----------
# desligada: nada roda
novo_cenario("ok", ativo=False)
assert "desligada" in f.enfileirar(["gupy-1"])
f.tick()
assert estado_pedido() == "na_fila"

# validações do pedido
novo_cenario("ok")
assert "Não achei a vaga zzz" in f.enfileirar(["zzz"])
f.gravar(f.ESTADO, {"vagas": {"gupy-1": {"status": "revisao_manual", "candidatura": "enviada"}}})
assert "Não vou repetir" in f.enfileirar(["gupy-1"])
assert "#1" in f.enfileirar(["gupy-1"], forcar=True)
assert "Não dupliquei" in f.enfileirar(["gupy-1"])
f.gravar(f.ESTADO, {"vagas": {"gupy-1": {"status": "descartada", "candidatura": None}}})
f.carregar(1)["estado"]
f.salvar({**f.carregar(1), "estado": "cancelada"})
assert "descartada" in f.enfileirar(["gupy-1"])

# caminho feliz
novo_cenario("ok")
f.enfileirar(["gupy-1"])
f.tick()
assert estado_pedido() == "concluida_confirmada" and candidatura() == "enviada"
ms = mensagens()
assert any("enviada e confirmada: Dev Node na La Moda (#1)" in m and "Confirmada na tela" in m for m in ms), ms
assert any(m.startswith("Uso agora:") for m in ms), ms  # linha de base do medidor veio do stream

# pergunta ao dono e retomada, com "lembrar"
novo_cenario("pergunta")
f.enfileirar(["gupy-1"])
f.tick()
assert estado_pedido() == "aguardando_dono"
ms = mensagens()
assert any("#1 Dev Node na La Moda: Pretensão salarial" in m and "#1 salario <valor>" in m for m in ms), ms
assert f.responder("oi tudo bem") is None  # texto livre não vira resposta
assert "Recebi" in f.responder("#1 salario 4500 lembrar")
assert estado_pedido() == "retomando" and f.ler(f.APROVADAS, {}) == {"salario": "4500"}
os.environ["FAKE_RESP"] = "4500"
f.tick()
assert estado_pedido() == "concluida_confirmada" and candidatura() == "enviada"

# pular uma pergunta
novo_cenario("pergunta")
f.enfileirar(["gupy-1"])
f.tick()
mensagens()
assert "Pulei o #1" in f.responder("pular #1") and estado_pedido() == "pulada"

# cota estoura antes de clicar: espera, não repete, e volta sozinha
novo_cenario("limite")
f.enfileirar(["gupy-1"])
f.tick()
assert estado_pedido() == "aguardando_cota"
c = f.ler(f.COTA, {})
assert c["bloqueado_ate"] > time.time() and candidatura() is None
ms = mensagens()
assert any("Limite de 5 horas atingido" in m and "tentar de novo sozinho" in m for m in ms), ms
f.tick()  # ainda bloqueado: não roda nada
assert estado_pedido() == "aguardando_cota" and mensagens() == []
c["bloqueado_ate"] = time.time() - 1
f.gravar(f.COTA, c)
os.environ["FAKE_CENARIO"] = "ok"
f.tick()
assert estado_pedido() == "concluida_confirmada" and candidatura() == "enviada"
assert any("Cota voltou" in m for m in mensagens())

# cota estoura depois de clicar: nunca reenvia
novo_cenario("limite_depois_de_clicar")
f.enfileirar(["gupy-1"])
f.tick()
assert estado_pedido() == "concluida_sem_confirmacao" and candidatura() == "enviada_sem_confirmacao"
assert any("não vou reenviar" in m for m in mensagens())
assert "Registrei" in f.responder("conferi enviou #1") and candidatura() == "enviada"

# retries esgotados pedem decisão
novo_cenario("limite")
f.enfileirar(["gupy-1"])
for _ in range(5):
    f.tick()
    c = f.ler(f.COTA, {})
    c["bloqueado_ate"] = time.time() - 1 if c.get("bloqueado_ate") else None
    f.gravar(f.COTA, c)
    if estado_pedido() == "aguardando_dono":
        break
assert estado_pedido() == "aguardando_dono", estado_pedido()
assert any("limite estourou de novo" in m for m in mensagens())
assert "tento de novo" in f.responder("#1 1") and estado_pedido() == "na_fila"

# crash: uma nova tentativa, depois falha; nunca marca enviada
novo_cenario("crash")
f.enfileirar(["gupy-1"])
f.tick()
assert estado_pedido() == "na_fila"
p = f.carregar(1)
p["inicia_apos"] = 0
f.salvar(p)
f.tick()
assert estado_pedido() == "falhou" and candidatura() == "falhou:worker"
assert any("Nada foi enviado" in m for m in mensagens())

# travou depois de clicar
novo_cenario("clicou_e_caiu")
f.enfileirar(["gupy-1"])
f.tick()
assert estado_pedido() == "concluida_sem_confirmacao"

# órfão: bot reiniciou com pedido executando
novo_cenario("ok")
f.enfileirar(["gupy-1"])
p = f.carregar(1)
p["estado"] = "executando"
f.salvar(p)
f.recuperar_orfaos()
assert estado_pedido() == "na_fila" and any("foi interrompido" in m for m in mensagens())
p = f.carregar(1)
p["estado"] = "executando"
f.salvar(p)
(f.DIA).mkdir(parents=True, exist_ok=True)
(f.DIA / "1.antes_de_enviar").write_text("x")
f.recuperar_orfaos()
assert estado_pedido() == "concluida_sem_confirmacao"

# cancelar e comandos
novo_cenario("ok")
f.enfileirar(["gupy-1"])
assert "Cancelei o #1" in f.responder("cancelar #1") and estado_pedido() == "cancelada"
assert "já terminou" in f.responder("cancelar #1")
assert f.responder("fila") == "Fila vazia."
assert "Ainda não tenho" in f.responder("uso")

# configuração real incompleta: falha clara, sem abrir nada
novo_cenario("ok", comando_worker=None)
f.enfileirar(["gupy-1"])
f.tick()
assert estado_pedido() == "falhou" and any("falta configurar o navegador" in m or "não encontrei o comando claude" in m for m in mensagens())
print("fila ok")
