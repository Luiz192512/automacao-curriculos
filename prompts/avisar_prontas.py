"""Avisa no WhatsApp (pela outbox do bot) as vagas que o pipeline aprovou e que ainda não foram avisadas.

Uso (a partir da pasta prompts/):
    python avisar_prontas.py

Só considera vagas com status "pronta" ou "pronta_fonte_unica" que ainda não têm candidatura feita
(campo "candidatura" no estado) e que ainda não foram avisadas (campo "avisada"). A mensagem é montada por
código, sem modelo, com no máximo 3 vagas e 900 caracteres, sem pergunta e sem travessão. Cada vaga é avisada
uma única vez. A candidatura em si continua sendo feita à parte.
"""

import datetime as dt
import json
import pathlib
import re
import sys

HERE = pathlib.Path(__file__).resolve().parent
ESTADO = HERE / "saida" / "estado.json"
VAGAS = HERE / "dados" / "vagas.json"
OUTBOX = HERE / "saida" / "outbox"
STATUS_PRONTOS = {"pronta", "pronta_fonte_unica"}
MAX_POR_MENSAGEM = 3
TRAVESSOS = re.compile(r"\s*[" + chr(0x2014) + chr(0x2013) + r"]\s*")


def montar_mensagem(itens: list, total: int) -> str:
    linhas = [f"Novas vagas aprovadas aguardando candidatura: {total}"]
    for posicao, (vaga, registro) in enumerate(itens, 1):
        triagem = registro.get("triagem", {})
        fonte_unica = " (sem segunda opinião)" if registro.get("status") == "pronta_fonte_unica" else ""
        linhas.append(f"{posicao}. {vaga['titulo']} na {vaga.get('empresa') or 'empresa não informada'}")
        linhas.append(f"Compatibilidade {triagem.get('score_compatibilidade')} de 100, fonte {vaga.get('fonte', 'não informada')}{fonte_unica}")
        if vaga.get("url"):
            linhas.append(vaga["url"])
    if total > len(itens):
        linhas.append(f"e mais {total - len(itens)}. Veja prompts/saida/fila_revisao.md")
    return TRAVESSOS.sub(", ", "\n".join(linhas))[:900]


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    estado = json.loads(ESTADO.read_text(encoding="utf-8"))
    vagas = {vaga["id"]: vaga for vaga in json.loads(VAGAS.read_text(encoding="utf-8"))}
    pendentes = [
        (vagas[identificador], registro)
        for identificador, registro in estado["vagas"].items()
        if registro.get("status") in STATUS_PRONTOS and not registro.get("candidatura") and not registro.get("avisada") and identificador in vagas
    ]
    if not pendentes:
        print("Nenhuma vaga aprovada nova para avisar.")
        return 0
    pendentes.sort(key=lambda item: -(item[1].get("triagem", {}).get("score_compatibilidade") or 0))
    escolhidas = pendentes[:MAX_POR_MENSAGEM]
    mensagem = montar_mensagem(escolhidas, len(pendentes))
    OUTBOX.mkdir(parents=True, exist_ok=True)
    arquivo = OUTBOX / f"{dt.datetime.now().strftime('%Y%m%d-%H%M%S-%f')}_vagas_prontas.json"
    arquivo.write_text(json.dumps({"tipo": "vagas_prontas", "mensagem": mensagem, "alertas": [], "origem": "codigo"}, ensure_ascii=False, indent=2), encoding="utf-8")
    for vaga, registro in escolhidas:
        registro["avisada"] = dt.datetime.now().isoformat(timespec="seconds")
    ESTADO.write_text(json.dumps(estado, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"{len(escolhidas)} vaga(s) avisadas na outbox: {arquivo.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
