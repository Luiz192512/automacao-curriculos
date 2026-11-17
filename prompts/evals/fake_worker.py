"""Worker de mentira para testar a fila sem navegador nem cota. Cenário em FAKE_CENARIO.
Uso: fake_worker.py <id> [retomar]"""
import json
import os
import pathlib
import sys

pid, retomar = sys.argv[1], len(sys.argv) > 2
ap = pathlib.Path(os.environ["AUTOMACAO_SAIDA"]) / "aplicar"
cenario = os.environ.get("FAKE_CENARIO", "ok")
fixture = pathlib.Path(__file__).parent / "fixtures" / "rate_limit_event.json"


def evento(cheio: bool) -> None:
    ev = json.loads(fixture.read_text(encoding="utf-8"))
    if cheio:
        ev["rate_limit_info"]["unifiedWindows"]["five_hour"].update(utilization=1.0, status="rejected")
    print(json.dumps(ev), flush=True)


def resultado(estado: str, **extra) -> None:
    (ap / "resultados").mkdir(parents=True, exist_ok=True)
    dados = {"id": int(pid), "vaga_id": "gupy-1", "estado": estado, "confirmacao_tela": estado == "concluida_confirmada",
             "enviado": estado.startswith("concluida"), "cv_usado": "CV_PT", **extra}
    (ap / "resultados" / f"{pid}.json").write_text(json.dumps(dados), encoding="utf-8")


def clicar() -> None:
    (ap / "diario").mkdir(parents=True, exist_ok=True)
    (ap / "diario" / f"{pid}.antes_de_enviar").write_text("x")


if cenario == "ok":
    evento(False)
    resultado("concluida_confirmada", evidencia="Candidatura finalizada!", respostas_enviadas={"pretensao": "4500"})
elif cenario == "pergunta":
    evento(False)
    if retomar:
        resultado("concluida_confirmada", evidencia="ok", respostas_enviadas={"salario": os.environ.get("FAKE_RESP", "?")})
    else:
        (ap / "perguntas").mkdir(parents=True, exist_ok=True)
        (ap / "perguntas" / f"{pid}.json").write_text(json.dumps({"id": int(pid), "tipo": "campo_aberto", "pergunta": "Pretensão salarial (R$)", "chave": "salario"}), encoding="utf-8")
        resultado("aguardando_dono")
elif cenario == "limite":
    evento(True)
    sys.exit(1)
elif cenario == "limite_depois_de_clicar":
    clicar()
    evento(True)
    sys.exit(1)
elif cenario == "crash":
    sys.exit(1)
elif cenario == "clicou_e_caiu":
    clicar()
    sys.exit(1)
