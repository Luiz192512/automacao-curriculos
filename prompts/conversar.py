"""Responde, pelo WhatsApp, as mensagens que o dono manda para o bot.

O bot grava cada mensagem recebida em saida/inbox e chama este script. Aqui só se obedece a quem está em
bot/config.json "donos" (campo do_dono). Comandos fixos são tratados por código; o resto vai para um modelo que
conversa SÓ com o que está no estado (contagens e a lista de revisão) e não executa nada.

Comandos: ajuda, status, revisao, vaga N, descartar N..., aplicar N..., rodar.
"aplicar" anota o pedido (saida/pedidos_aplicar.json) e o entrega a aplicar_fila.py, que só roda um worker
`claude -p` se saida/aplicar/config.json tiver "ativo": true. Nada aqui envia candidatura.
"""

import datetime as dt
import importlib.util
import json
import os
import pathlib
import re
import subprocess
import time
import unicodedata

HERE = pathlib.Path(__file__).resolve().parent
SAIDA = HERE / "saida"
INBOX, OUTBOX = SAIDA / "inbox", SAIDA / "outbox"
ESTADO, VAGAS = SAIDA / "estado.json", HERE / "dados" / "vagas.json"
LISTA = SAIDA / "conversa_lista.json"  # números da última lista mostrada: "aplicar 3" vale para ela
PEDIDOS = SAIDA / "pedidos_aplicar.json"
TRAVESSOS = re.compile(r"\s*[" + chr(0x2014) + chr(0x2013) + r"]\s*")
ORDEM = [("groq", "qwen/qwen3.8-27b"), ("groq", "openai/gpt-oss-120b"), ("gemini", None)]


def carregar_chaves() -> None:
    """A tarefa agendada pode não herdar as variáveis do usuário: lê direto do registro do Windows."""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as chave:
            for nome in ("GROQ_API_KEY", "GEMINI_API_KEY"):
                if not os.environ.get(nome):
                    os.environ[nome] = winreg.QueryValueEx(chave, nome)[0]
    except (ImportError, OSError):
        pass


def ler(caminho: pathlib.Path, padrao):
    return json.loads(caminho.read_text(encoding="utf-8")) if caminho.exists() else padrao


def gravar(caminho: pathlib.Path, dados) -> None:
    caminho.write_text(json.dumps(dados, ensure_ascii=False, indent=2), encoding="utf-8")


def responder(texto: str) -> None:
    OUTBOX.mkdir(parents=True, exist_ok=True)
    texto = TRAVESSOS.sub(", ", texto.strip())[:3400]
    nome = f"{dt.datetime.now():%Y%m%d-%H%M%S-%f}_conversa.json"
    (OUTBOX / nome).write_text(json.dumps({"mensagem": texto, "tipo": "conversa"}, ensure_ascii=False), encoding="utf-8")


def sem_acento(texto: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", texto.lower()) if unicodedata.category(c) != "Mn").strip()


def pendentes_de_revisao(estado: dict) -> list:
    vagas = {v["id"]: v for v in ler(VAGAS, [])}
    saida = []
    for vid, reg in estado["vagas"].items():
        if reg.get("status") == "revisao_manual" and not reg.get("candidatura") and not reg.get("decisao_dono"):
            tri = reg.get("triagem") or {}
            saida.append({"id": vid, "titulo": vagas.get(vid, {}).get("titulo", vid), "empresa": vagas.get(vid, {}).get("empresa", ""),
                          "url": vagas.get(vid, {}).get("url", ""), "score": tri.get("score_compatibilidade"),
                          "motivo": str(tri.get("justificativa", ""))[:160], "lacunas": tri.get("lacunas") or []})
    return saida


def mostrar_lista(estado: dict) -> str:
    itens = pendentes_de_revisao(estado)
    gravar(LISTA, [i["id"] for i in itens])
    if not itens:
        return "Nenhuma vaga esperando revisão."
    linhas = [f"{n}) {', '.join(p for p in (i['titulo'], i['empresa']) if p)} (nota {i['score']})" for n, i in enumerate(itens, 1)]
    return f"Vagas para revisão ({len(itens)}):\n" + "\n".join(linhas) + "\nResponda: aplicar 1 4 / descartar 2 3 / vaga 5 (detalhe)"


def numeros(texto: str, ids: list):
    escolhidos = [int(n) for n in re.findall(r"\d+", texto)]
    ruins = [n for n in escolhidos if not 1 <= n <= len(ids)]
    return ([ids[n - 1] for n in escolhidos if n not in ruins], ruins)


def decidir(estado: dict, texto: str, acao: str) -> str:
    ids = ler(LISTA, [])
    literais = re.findall(r"\b(?:gupy|jobicy|remotive|greenhouse)-[\w-]+", texto)
    if literais and acao == "aplicar":  # "aplicar gupy-12785519" vale sem a lista
        ids = literais
        texto = " ".join(str(n) for n in range(1, len(ids) + 1))
    if not ids:
        return "Peça primeiro a lista: mande 'revisao'."
    escolhidas, ruins = numeros(texto, ids)
    if ruins or not escolhidas:
        return f"Números inválidos {ruins or ''}. A lista atual vai de 1 a {len(ids)}. Mande 'revisao' para ver de novo."
    desconhecidas = [v for v in escolhidas if v not in estado["vagas"]]
    for vid in escolhidas:
        reg = estado["vagas"].get(vid)
        if reg is None:
            continue
        if acao == "descartar":
            reg["status"], reg["motivo"] = "descartada", "descartada pelo dono via WhatsApp"
        else:
            reg["decisao_dono"] = "aplicar"
    gravar(ESTADO, estado)
    if acao == "aplicar":
        pedidos = ler(PEDIDOS, [])
        gravar(PEDIDOS, pedidos + [{"id": v, "pedido_em": dt.datetime.now().isoformat(timespec="seconds")} for v in escolhidas if v not in [p["id"] for p in pedidos]])
        import aplicar_fila  # fila de candidaturas: valida, avisa e o bot roda o worker pelo tick
        return aplicar_fila.enfileirar(escolhidas, forcar="forcar" in texto)
    return f"Descartei {len(escolhidas)} vaga(s)."


def status(estado: dict) -> str:
    cont = {}
    for reg in estado["vagas"].values():
        cont[reg.get("status")] = cont.get(reg.get("status"), 0) + 1
    enviadas = sum(1 for r in estado["vagas"].values() if r.get("candidatura") == "enviada")
    return (f"Vagas no estado: {len(estado['vagas'])}. Prontas {cont.get('pronta_fonte_unica', 0) + cont.get('pronta', 0)}, "
            f"em revisão {len(pendentes_de_revisao(estado))}, descartadas {cont.get('descartada', 0)}. "
            f"Candidaturas enviadas: {enviadas}. Pedidos para aplicar anotados: {len(ler(PEDIDOS, []))}.")


def detalhe(estado: dict, texto: str) -> str:
    ids = ler(LISTA, [])
    escolhidas, ruins = numeros(texto, ids)
    if ruins or not escolhidas:
        return "Mande 'revisao' e depois 'vaga N' com um número da lista."
    item = next(i for i in pendentes_de_revisao(estado) if i["id"] == escolhidas[0])
    lacunas = "; ".join(str(g.get("requisito", g)) if isinstance(g, dict) else str(g) for g in item["lacunas"][:3]) or "nenhuma listada"
    return f"{item['titulo']}, {item['empresa']}\nNota: {item['score']}\nPor quê: {item['motivo']}\nLacunas: {lacunas}\n{item['url']}"


def rodar_agora() -> str:
    resultado = subprocess.run(["powershell", "-NoProfile", "-Command", "Start-ScheduledTask -TaskName AutomacaoCurriculos-Rodada"],
                               capture_output=True, text=True, creationflags=0x08000000)
    return "Rodada iniciada. Aviso se aparecer vaga nova." if resultado.returncode == 0 else "Não consegui iniciar a rodada."


def conversar_com_modelo(texto: str, estado: dict) -> str:
    spec = importlib.util.spec_from_file_location("run_evals", HERE / "evals" / "run_evals.py")
    avaliador = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(avaliador)
    contexto = {"resumo": status(estado), "revisao": [{k: i[k] for k in ("titulo", "empresa", "score", "motivo")} for i in pendentes_de_revisao(estado)]}
    system = ("You answer in Brazilian Portuguese, in at most 600 characters, no markdown, no em dashes. You are the WhatsApp assistant of a "
              "job-application automation. Use ONLY the data in <estado>. If the user asks for something you cannot do from here, say so and list "
              "the commands: ajuda, status, revisao, vaga N, descartar N, aplicar N, rodar. Never claim an application was sent. "
              'Return JSON: {"resposta": "..."}.')
    usuario = f"<estado>{json.dumps(contexto, ensure_ascii=False)}</estado>\n<mensagem>{texto[:600]}</mensagem>"
    for provedor, modelo in ORDEM:
        bruto, erro = avaliador.call_with_retry(provedor, modelo or avaliador.PROVIDER_DEFAULTS[provedor]["model"], system, usuario)
        if erro:
            continue
        try:
            resposta = json.loads(bruto).get("resposta")
        except (json.JSONDecodeError, AttributeError):
            continue
        if isinstance(resposta, str) and resposta.strip():
            return resposta
    return "Não consegui pensar agora. Comandos: ajuda, status, revisao, vaga N, descartar N, aplicar N, rodar."


AJUDA = ("Comandos:\nstatus: resumo\nrevisao: lista das vagas em revisão\nvaga 3: detalhe da vaga 3 da lista\n"
         "aplicar 1 4: pede a candidatura (aplicar forcar 3 ignora descarte e envio anterior)\ndescartar 2 3: descarta\n"
         "fila / uso: pedidos em andamento / cota do Claude\ncancelar #7, pular #7, manual #7, de novo #7\n"
         "#7 resposta: responde a uma pergunta do robô\nrodar: inicia uma rodada agora\nOu pergunte em texto livre.")


def tratar(texto: str) -> str:
    import aplicar_fila
    resposta = aplicar_fila.responder(texto)  # fila, perguntas abertas, uso, cancelar, pular...
    if resposta is not None:
        return resposta
    estado = ler(ESTADO, {"vagas": {}})
    cmd = sem_acento(texto)
    if cmd in ("ajuda", "help", "?", "oi", "ola"):
        return AJUDA
    if cmd in ("status", "resumo"):
        return status(estado)
    if cmd in ("revisao", "lista", "revisar"):
        return mostrar_lista(estado)
    if cmd.startswith("vaga "):
        return detalhe(estado, cmd)
    if cmd.startswith(("aplicar ", "descartar ")):
        return decidir(estado, cmd, cmd.split()[0])
    if cmd in ("rodar", "rodar agora"):
        return rodar_agora()
    return conversar_com_modelo(texto, estado)


def pendentes() -> list:
    arquivos = sorted(INBOX.glob("*.json")) if INBOX.exists() else []
    dados = [(a, ler(a, {})) for a in arquivos]
    return [(a, d) for a, d in dados if d.get("do_dono") and not d.get("processada")]


def main() -> None:
    trava = SAIDA / "conversar.lock"
    if trava.exists() and time.time() - trava.stat().st_mtime < 120:
        return  # outra instância está tratando; ela relê a inbox antes de sair
    trava.write_text(str(os.getpid()))
    try:
        carregar_chaves()
        for _ in range(3):  # relê depois de liberar, para não perder mensagem que chegou no fim
            fila = pendentes()
            if not fila:
                break
            for arquivo, dados in fila:
                dados["processada"] = True  # antes de responder: uma falha não vira resposta repetida
                gravar(arquivo, dados)
                try:
                    resposta = tratar(dados["texto"])
                    enviada = dt.datetime.fromisoformat(dados["enviada_em"]).astimezone() if dados.get("enviada_em") else None
                    if enviada and time.time() - enviada.timestamp() > 120:  # o PC estava desligado quando o dono escreveu
                        resposta = f"Voltei. Recebi seu pedido das {enviada:%H:%M}.\n{resposta}"
                    responder(resposta)
                except Exception as erro:  # noqa: BLE001
                    responder(f"Deu erro ao tratar a mensagem: {type(erro).__name__}. Tente 'ajuda'.")
    finally:
        trava.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
