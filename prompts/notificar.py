"""Gera o resumo de um evento de candidatura e o coloca na outbox do bot de WhatsApp.

Uso (a partir da pasta prompts/):
    python notificar.py --evento evento.json [--log "linhas do log"]
    python notificar.py --evento evento.json --seco      # só mostra a mensagem, não grava na outbox

O evento é um JSON como os do prompt notificacao-whatsapp-candidaturas.md (tipo, vaga, empresa, fonte, url,
score, respostas, confirmacao_tela, ...). O resumo vem de um modelo (Groq, depois Gemini). Se o modelo falhar
ou devolver algo fora das regras, entra um texto montado por código, para que nenhuma notificação se perca.

Chaves: GROQ_API_KEY e GEMINI_API_KEY (variáveis de ambiente). Nada aqui envia WhatsApp: quem envia é o bot em
bot/whatsapp_bot.mjs, que lê a pasta saida/outbox.
"""

import argparse
import datetime as dt
import importlib.util
import json
import pathlib
import re
import sys

HERE = pathlib.Path(__file__).resolve().parent
OUTBOX = HERE / "saida" / "outbox"
PROMPT = HERE / "notificacao-whatsapp-candidaturas.md"
ORDEM = [("groq", "qwen/qwen3.8-27b"), ("groq", "openai/gpt-oss-120b"), ("gemini", None)]
TIPOS = {"candidatura_enviada", "candidatura_pulada", "candidatura_falhou", "resumo_diario"}
CAMPOS = ["alertas", "campos_usados", "mensagem", "tipo"]
TRAVESSOS = re.compile(r"\s*[" + chr(0x2014) + chr(0x2013) + r"]\s*")
DADO_PESSOAL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+|\(\d{2}\)\s?\d|\+55|\b\d{4,5}-\d{4}\b|\b\d{3}\.\d{3}\.\d{3}-\d{2}\b|senha|password|token|cookie", re.I)


def carregar_avaliador():
    spec = importlib.util.spec_from_file_location("run_evals", HERE / "evals" / "run_evals.py")
    modulo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modulo)
    return modulo


avaliador = carregar_avaliador()


def quebrar_linhas(texto: str, limite: int = 120) -> str:
    saida = []
    for linha in texto.split("\n"):
        while len(linha) > limite:
            corte = linha.rfind(" ", 0, limite)
            if corte <= 0:
                break  # link ou palavra longa: não quebra
            saida.append(linha[:corte])
            linha = linha[corte + 1:]
        saida.append(linha)
    return "\n".join(saida)


def sanear(mensagem: str) -> str:
    return quebrar_linhas(TRAVESSOS.sub(", ", mensagem.strip()))


def problemas_da_mensagem(mensagem: str) -> list:
    problemas = []
    if not mensagem:
        problemas.append("vazia")
    if len(mensagem) > 900:
        problemas.append("passa de 900 caracteres")
    if "?" in mensagem:
        problemas.append("tem interrogação")
    if DADO_PESSOAL.search(mensagem):
        problemas.append("possível dado pessoal ou segredo")
    return problemas


def texto_de_codigo(evento: dict) -> str:
    """Mensagem montada sem modelo, usada quando o modelo falha. Só diz o que o evento afirma."""
    tipo = evento.get("tipo", "candidatura_enviada")
    vaga, empresa = evento.get("vaga", "vaga não informada"), evento.get("empresa", "empresa não informada")
    linhas = []
    if tipo == "candidatura_enviada":
        linhas.append(f"Candidatura enviada: {vaga} na {empresa}")
        for rotulo, chave in (("Fonte", "fonte"), ("Compatibilidade", "score")):
            if evento.get(chave) is not None:
                linhas.append(f"{rotulo}: {evento[chave]}" + (" de 100" if chave == "score" else ""))
        if evento.get("url"):
            linhas.append(f"Link: {evento['url']}")
        linhas.append("Confirmada na tela do portal." if evento.get("confirmacao_tela") is True else "Sem confirmação na tela do portal.")
    elif tipo == "candidatura_pulada":
        linhas.append(f"Pulada: {vaga} na {empresa}")
        linhas.append(f"Motivo: {evento.get('motivo', 'não informado')}" + (f": \"{evento['pergunta']}\"" if evento.get("pergunta") else ""))
        linhas += ["Nada foi enviado."] + ([f"Link: {evento['url']}"] if evento.get("url") else [])
    elif tipo == "candidatura_falhou":
        linhas.append(f"Falhou: {vaga} na {empresa}")
        linhas.append(f"Etapa: {evento.get('etapa', 'não informada')}. Erro: {evento.get('erro', 'não informado')}")
        linhas.append("Nada foi enviado." if evento.get("enviado") is False else "Pode ter sido enviada, vale conferir no portal.")
    else:
        linhas.append("Resumo do dia disponível, mas o modelo de resumo falhou. Veja saida/fila_revisao.md.")
    return sanear("\n".join(linhas))


def pedir_ao_modelo(evento: dict, log: str, eventos_do_dia: list | None):
    system = avaliador.load_system_prompt(PROMPT)
    if eventos_do_dia is not None:
        usuario = f"<eventos_do_dia>{json.dumps(eventos_do_dia, ensure_ascii=False)}</eventos_do_dia>"
    else:
        usuario = f"<evento>{json.dumps(evento, ensure_ascii=False)}</evento>\n<log>{log}</log>"
    for provedor, modelo in ORDEM:
        modelo = modelo or avaliador.PROVIDER_DEFAULTS[provedor]["model"]
        bruto, erro = avaliador.call_with_retry(provedor, modelo, system, usuario)
        if erro:
            continue
        try:
            saida = json.loads(bruto)
        except json.JSONDecodeError:
            continue
        if isinstance(saida, dict) and sorted(saida) == CAMPOS and saida.get("tipo") in TIPOS and isinstance(saida.get("mensagem"), str):
            return saida, modelo
    return None, None


def gerar(evento: dict, log: str = "", eventos_do_dia: list | None = None) -> dict:
    saida, modelo = pedir_ao_modelo(evento, log, eventos_do_dia)
    if saida is not None:
        mensagem = sanear(saida["mensagem"])
        problemas = problemas_da_mensagem(mensagem)
        if not problemas:
            return {"tipo": saida["tipo"], "mensagem": mensagem, "alertas": saida.get("alertas", []), "origem": f"modelo:{modelo}"}
        motivo = "; ".join(problemas)
    else:
        motivo = "modelo indisponível ou resposta fora do formato"
    return {"tipo": evento.get("tipo", "candidatura_enviada"), "mensagem": texto_de_codigo(evento), "alertas": [], "origem": f"codigo ({motivo})"}


def gravar_na_outbox(resultado: dict, identificador: str = "evento") -> pathlib.Path:
    OUTBOX.mkdir(parents=True, exist_ok=True)
    marca = dt.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    seguro = re.sub(r"[^a-z0-9]+", "-", identificador.lower()).strip("-")[:40] or "evento"
    caminho = OUTBOX / f"{marca}_{resultado['tipo']}_{seguro}.json"
    caminho.write_text(json.dumps(resultado, ensure_ascii=False, indent=2), encoding="utf-8")
    return caminho


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--evento", required=True, help="arquivo JSON com o evento (ou lista de eventos para o resumo do dia)")
    parser.add_argument("--log", default="", help="linhas do log da candidatura")
    parser.add_argument("--seco", action="store_true", help="não grava na outbox")
    args = parser.parse_args()
    conteudo = json.loads(pathlib.Path(args.evento).read_text(encoding="utf-8"))
    eventos_do_dia = conteudo if isinstance(conteudo, list) else None
    evento = {"tipo": "resumo_diario"} if eventos_do_dia is not None else conteudo
    resultado = gerar(evento, args.log, eventos_do_dia)
    print(resultado["mensagem"])
    print(f"\n[origem: {resultado['origem']}]")
    if not args.seco:
        print("Gravada em", gravar_na_outbox(resultado, str(evento.get("empresa", "evento"))))


if __name__ == "__main__":
    main()
