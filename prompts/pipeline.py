"""Pipeline de candidaturas: triagem no Groq, verificação e redação no Gemini, com fallback entre os dois.

Uso (a partir da pasta prompts/):
    python pipeline.py --vagas dados/vagas.json

Chaves: GROQ_API_KEY e GEMINI_API_KEY (variáveis de ambiente).

Fluxo, para cada vaga:
  0. Pré-checagem em código: sem descrição ou sem empresa vai para revisão manual sem gastar chamada.
     O idioma da vaga escolhe o currículo e o idioma da mensagem: vaga em português usa dados/perfil.pt.md
     e mensagem em português; vaga em inglês usa dados/perfil.en.md e mensagem em inglês. Vaga em outro
     idioma é descartada (não há currículo compatível); idioma não identificado vai para revisão manual.
  1. Triagem no Groq (alto volume). Se o Groq estiver sem cota, o Gemini faz a triagem.
  2. Só as vagas que a triagem marcou como "candidatar" passam pela verificação no outro modelo, que roda o
     mesmo prompt de forma independente. Se discordar, a vaga vai para revisão manual; se concordar, a
     mensagem do Gemini é a que fica. Se o outro modelo estiver sem cota, a vaga fica como "pronta, fonte
     única" (revise com mais atenção), a não ser que --exigir-verificacao seja usado.
  3. Em código, depois de cada chamada: travessões viram vírgulas, a decisão é recalculada a partir de área,
     score e alertas, e uma checagem de exagero manda mensagens suspeitas para revisão manual.

NADA é enviado a ninguém. O resultado é saida/fila_revisao.md, para você aprovar e enviar. O estado fica em
saida/estado.json: rodar de novo continua de onde parou (útil quando a cota diária zera).
"""

import argparse
import datetime as dt
import importlib.util
import json
import pathlib
import re
import sys
import time

HERE = pathlib.Path(__file__).resolve().parent
EXIT_PENDENTE = 75

ORDEM_TRIAGEM = ["groq", "gemini"]
DESATIVADOS = set()  # provedores desligados nesta execução (por exemplo, --so-groq desliga o gemini)
AREAS_IA = {"ia_fullstack", "ia_backend"}
AREAS_AMPLAS = AREAS_IA | {"fullstack_backend"}
TRAVESSOS = re.compile(r"\s*[" + chr(0x2014) + chr(0x2013) + r"]\s*")

PALAVRAS_PT = {
    "de", "da", "do", "dos", "das", "para", "com", "uma", "os", "em", "vaga", "requisitos", "experiência",
    "desejável", "conhecimento", "conhecimentos", "equipe", "você", "será", "nossa", "empresa", "trabalho",
    "pela", "pelo", "são", "não", "atuação", "diferencial", "benefícios",
}
PALAVRAS_EN = {
    "the", "and", "with", "for", "you", "will", "our", "required", "desirable", "preferred", "experience",
    "skills", "team", "work", "are", "of", "to", "in", "is", "we", "at", "engineer", "developer", "years",
    "knowledge", "degree", "ability", "strong", "remote",
}
PALAVRAS_ES = {
    "el", "los", "las", "con", "una", "años", "experiencia", "equipo", "trabajo", "buscamos",
    "conocimientos", "puesto", "oferta", "nuestro", "nuestra",
}
IDIOMA_DA_MENSAGEM = {"pt": "pt-BR", "en": "en"}


def carregar_avaliador():
    spec = importlib.util.spec_from_file_location("run_evals", HERE / "evals" / "run_evals.py")
    modulo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modulo)
    return modulo


avaliador = carregar_avaliador()

# Modelos de cada provedor, em ordem de preferência. Cada modelo tem cota própria, então o pipeline passa ao
# seguinte quando um acaba. Modelos fora de MODELOS_AVALIADOS ainda não foram medidos no eval set: o resultado
# deles aparece na fila com um aviso.
MODELOS = {
    # gpt-oss-20b ficou de fora: no eval set da v6 acertou só 8 de 11 decisões, devolveu JSON inválido uma vez e
    # chegou a marcar "candidatar" numa vaga com tentativa de injeção de prompt.
    "groq": ["qwen/qwen3.8-27b", "openai/gpt-oss-120b"],
    "gemini": [avaliador.PROVIDER_DEFAULTS["gemini"]["model"]],
}
# qwen3.8-27b na v6: JSON 11/11, decisão 10/11 (o erro foi para o lado seguro), exagero 11/11, score na faixa 7/9.
MODELOS_AVALIADOS = {"openai/gpt-oss-120b", "qwen/qwen3.8-27b", avaliador.PROVIDER_DEFAULTS["gemini"]["model"]}


def agora() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def prompt_padrao() -> pathlib.Path:
    return HERE / "automacao-candidaturas-rag.v6.md"


# ---------- idioma ----------

def detectar_idioma(texto: str) -> str:
    """Devolve 'pt', 'en', 'outro' (por exemplo espanhol) ou 'indefinido'."""
    palavras = re.findall(r"[^\W\d_]+", texto.lower())
    pt = sum(1 for palavra in palavras if palavra in PALAVRAS_PT)
    en = sum(1 for palavra in palavras if palavra in PALAVRAS_EN)
    es = sum(1 for palavra in palavras if palavra in PALAVRAS_ES)
    if es >= 3 and es > pt and es > en:
        return "outro"
    if pt >= 2 and pt > en:
        return "pt"
    if en >= 2 and en > pt:
        return "en"
    return "indefinido"


# ---------- estado e bloqueios de cota ----------

def carregar_estado(caminho: pathlib.Path) -> dict:
    if caminho.exists():
        return json.loads(caminho.read_text(encoding="utf-8"))
    return {"bloqueios": {}, "vagas": {}}


def salvar_estado(caminho: pathlib.Path, estado: dict) -> None:
    caminho.write_text(json.dumps(estado, ensure_ascii=False, indent=2), encoding="utf-8")


def proximo_reset_gemini() -> dt.datetime:
    # A cota diária do plano gratuito zera à meia-noite do Pacífico, por volta de 07:00 UTC.
    alvo = agora().replace(hour=7, minute=5, second=0, microsecond=0)
    return alvo if alvo > agora() else alvo + dt.timedelta(days=1)


def bloqueado(estado: dict, provedor: str) -> bool:
    ate = estado["bloqueios"].get(provedor)
    return bool(ate) and agora() < dt.datetime.fromisoformat(ate)


AVISO_DE_ESPERA = re.compile(r"(?:retry|try again) in\s+(?:(\d+)h)?\s*(?:(\d+)m(?!s))?\s*(?:([\d.]+)s)?", re.I)


def espera_do_erro(erro: str):
    """Lê o 'try again in 15m11.52s' (Groq) ou 'retry in 17h12m58s' (Gemini) da mensagem de cota."""
    achado = AVISO_DE_ESPERA.search(erro or "")
    if not achado or not any(achado.groups()):
        return None
    horas, minutos, segundos = (float(parte) if parte else 0.0 for parte in achado.groups())
    return dt.timedelta(hours=horas, minutes=minutos, seconds=segundos + 60)


def bloquear(estado: dict, chave: str, erro: str = "") -> None:
    """chave: 'provedor:modelo'."""
    espera = espera_do_erro(erro)
    if espera is not None:
        ate = agora() + espera
    else:
        ate = proximo_reset_gemini() if chave.startswith("gemini") else agora() + dt.timedelta(minutes=30)
    estado["bloqueios"][chave] = ate.isoformat()


def bloqueios_ativos(estado: dict) -> list:
    return [chave for chave in estado["bloqueios"] if bloqueado(estado, chave)]


# ---------- vaga, chamada e pós-processamento ----------

def texto_da_vaga(vaga: dict) -> str:
    cabecalho = vaga["titulo"] + (f" at {vaga['empresa']}" if vaga.get("empresa") else "")
    return f"{cabecalho}. {vaga.get('texto', '').strip()}".strip()


def caso_da_vaga(vaga: dict) -> dict:
    # Palavras do próprio título (por exemplo "senior") podem aparecer na mensagem sem contar como exagero.
    return {"id": vaga["id"], "posting": texto_da_vaga(vaga), "allow_terms": re.findall(r"\w+", vaga["titulo"].lower())}


def montar_mensagem(perfil: str, caso: dict, idioma: str) -> str:
    return (
        f"<candidate_profile>\n{perfil}\n</candidate_profile>\n"
        f"<message_language>{IDIOMA_DA_MENSAGEM[idioma]}</message_language>\n"
        f"<job_posting id=\"{caso['id']}\">\n{caso['posting']}\n</job_posting>"
    )


def sanear(valor):
    if isinstance(valor, str):
        return TRAVESSOS.sub(", ", valor)
    if isinstance(valor, list):
        return [sanear(item) for item in valor]
    if isinstance(valor, dict):
        return {chave: sanear(item) for chave, item in valor.items()}
    return valor


def decidir(obj: dict, areas_alvo: set) -> str:
    """Regras de decisão do prompt, aplicadas em código sobre os campos que o modelo devolve."""
    if obj.get("area") not in areas_alvo:
        return "descartar"
    if obj.get("alertas"):
        return "revisar_manualmente"
    if not obj.get("evidencias") and not obj.get("lacunas"):
        return "revisar_manualmente"  # vaga sem requisitos identificáveis
    score = obj.get("score_compatibilidade")
    if not isinstance(score, (int, float)):
        return "revisar_manualmente"
    if score >= 75:
        return "candidatar"
    return "revisar_manualmente" if score >= 50 else "descartar"


def analisar(obj: dict, caso: dict, perfil: str, areas_alvo: set) -> dict:
    obj = sanear(obj)
    decisao_modelo = obj.get("decisao")
    decisao = decidir(obj, areas_alvo)
    exageros = avaliador.check_grounding(caso, perfil, obj)
    if decisao == "candidatar" and (exageros or not obj.get("mensagem_candidatura")):
        decisao = "revisar_manualmente"
    if decisao != "candidatar":
        obj["mensagem_candidatura"] = None
    obj.update({"decisao_modelo": decisao_modelo, "decisao": decisao, "exageros": exageros})
    return obj


def chamar(provedor, system, perfil, idioma, vaga, estado, delay, areas_alvo):
    """Devolve (resultado analisado, tipo). Tipo: 'ok', 'cota' ou 'erro'.

    Tenta os modelos do provedor em ordem. Cada modelo tem a sua própria cota, então um bloqueio vale só para ele.
    """
    if provedor in DESATIVADOS:
        return None, "cota"
    caso = caso_da_vaga(vaga)
    mensagem = montar_mensagem(perfil, caso, idioma)
    for modelo in MODELOS[provedor]:
        chave = f"{provedor}:{modelo}"
        if bloqueado(estado, chave):
            continue
        bruto, erro = avaliador.call_with_retry(provedor, modelo, system, mensagem)
        time.sleep(delay)
        if erro and erro.startswith(avaliador.QUOTA_PREFIX):
            bloquear(estado, chave, erro)
            continue
        if erro:
            return None, "erro"
        try:
            obj = json.loads(bruto)
        except json.JSONDecodeError:
            return None, "erro"
        resultado = analisar(obj, caso, perfil, areas_alvo)
        resultado["modelo"] = modelo
        return resultado, "ok"
    return None, "cota"


def status_da_decisao(decisao: str) -> str:
    return {"candidatar": "triada", "revisar_manualmente": "revisao_manual"}.get(decisao, "descartada")


# ---------- etapas ----------

def triar(vaga, system, perfis, estado, delays, areas_alvo):
    if not vaga.get("texto", "").strip():
        return {"status": "revisao_manual", "motivo": "vaga sem descrição (pré-checagem em código)", "provedor": None}
    if not vaga.get("empresa", "").strip():
        return {"status": "revisao_manual", "motivo": "empresa não identificada (pré-checagem em código)", "provedor": None}
    idioma = vaga.get("idioma") or detectar_idioma(texto_da_vaga(vaga))
    if idioma == "outro":
        return {"status": "descartada", "motivo": "idioma da vaga sem currículo compatível", "provedor": None, "idioma": idioma}
    if idioma not in perfis:
        return {"status": "revisao_manual", "motivo": "idioma da vaga não identificado", "provedor": None, "idioma": idioma}
    for provedor in ORDEM_TRIAGEM:
        resultado, tipo = chamar(provedor, system, perfis[idioma], idioma, vaga, estado, delays[provedor], areas_alvo)
        if tipo == "ok":
            return {"status": status_da_decisao(resultado["decisao"]), "triagem": resultado, "provedor": provedor, "idioma": idioma}
    return None


def verificar(vaga, registro, system, perfis, estado, delays, exigir_verificacao, areas_alvo):
    triagem = registro["triagem"]
    verificador = "gemini" if registro["provedor"] == "groq" else "groq"
    resultado, tipo = chamar(verificador, system, perfis[registro["idioma"]], registro["idioma"], vaga, estado,
                             delays[verificador], areas_alvo)
    if tipo == "ok":
        registro["verificacao"] = resultado
        registro["verificador"] = verificador
        if resultado["decisao"] == "candidatar":
            gemini = resultado if verificador == "gemini" else triagem
            registro.update(status="pronta", mensagem=gemini["mensagem_candidatura"], fonte_mensagem="gemini",
                            motivo=f"confirmada por {verificador}")
        else:
            registro.update(status="revisao_manual",
                            motivo=f"{verificador} discordou da triagem ({resultado['decisao']})")
        return True
    if tipo == "cota" and not exigir_verificacao:
        registro.update(status="pronta_fonte_unica", mensagem=triagem["mensagem_candidatura"],
                        fonte_mensagem=registro["provedor"],
                        motivo=f"{verificador} sem cota: sem segunda opinião (mensagem do {registro['provedor']})")
        return True
    return False  # erro transitório, ou verificação exigida: fica "triada" para a próxima execução


# ---------- relatório ----------

def ordem_na_fila(item) -> tuple:
    _, registro = item
    triagem = (registro or {}).get("triagem", {})
    return (0 if triagem.get("area") in AREAS_IA else 1, -(triagem.get("score_compatibilidade") or 0))


def escrever_fila(vagas, estado, saida: pathlib.Path) -> dict:
    grupos = {"pronta": [], "pronta_fonte_unica": [], "revisao_manual": [], "descartada": [], "triada": [], "pendente": []}
    for vaga in vagas:
        registro = estado["vagas"].get(vaga["id"])
        grupos[registro["status"] if registro else "pendente"].append((vaga, registro))

    linhas = ["# Fila de revisão de candidaturas", "",
              f"Gerada em {dt.datetime.now().isoformat(timespec='seconds')}. Nada foi enviado.",
              "Vagas de IA aparecem antes das demais, e dentro de cada grupo por score.", ""]
    titulos = [
        ("pronta", "Prontas para aprovar (duas opiniões concordam)"),
        ("pronta_fonte_unica", "Prontas, fonte única (revise com mais atenção)"),
        ("revisao_manual", "Revisão manual"),
        ("triada", "Triadas, aguardando verificação"),
        ("pendente", "Ainda não processadas (sem cota nos dois provedores)"),
    ]
    for chave, titulo in titulos:
        if not grupos[chave]:
            continue
        linhas += [f"## {titulo}", ""]
        for vaga, registro in sorted(grupos[chave], key=ordem_na_fila):
            linhas.append(f"### {vaga['titulo']} ({vaga.get('empresa') or 'empresa não informada'}) [{vaga['id']}]")
            if vaga.get("url"):
                fonte = f" (fonte: {vaga['fonte']})" if vaga.get("fonte") else ""
                linhas.append(f"- Link: {vaga['url']}{fonte}")
            if registro:
                triagem = registro.get("triagem", {})
                if triagem:
                    linhas.append(f"- Score: {triagem.get('score_compatibilidade')} | área: {triagem.get('area')} | "
                                  f"idioma: {registro.get('idioma')} | triagem: {registro['provedor']} ({triagem.get('modelo')})")
                    if triagem.get("modelo") not in MODELOS_AVALIADOS or registro.get("verificacao", {}).get("modelo", triagem.get("modelo")) not in MODELOS_AVALIADOS:
                        linhas.append("- Atenção: algum modelo desta análise ainda não foi medido no eval set; confira com mais cuidado.")
                    if triagem.get("lacunas"):
                        linhas.append(f"- Lacunas: {', '.join(triagem['lacunas'])}")
                    if triagem.get("exageros"):
                        linhas.append(f"- Atenção, possível exagero: {'; '.join(triagem['exageros'])}")
                    if triagem.get("alertas"):
                        linhas.append(f"- Alertas: {'; '.join(triagem['alertas'])}")
                if registro.get("motivo"):
                    linhas.append(f"- Motivo: {registro['motivo']}")
                if registro.get("mensagem"):
                    linhas += ["", "Mensagem sugerida:", "", "> " + registro["mensagem"].replace("\n", "\n> ")]
            linhas.append("")
    if grupos["descartada"]:
        linhas += ["## Descartadas", ""] + [
            f"- {vaga['titulo']} ({vaga.get('empresa') or '?'}) [{vaga['id']}]" + (f": {registro['motivo']}" if registro and registro.get("motivo") else "")
            for vaga, registro in grupos["descartada"]] + [""]
    (saida / "fila_revisao.md").write_text("\n".join(linhas) + "\n", encoding="utf-8")
    return {chave: len(itens) for chave, itens in grupos.items()}


# ---------- execução ----------

def executar(args) -> int:
    vagas = json.loads(pathlib.Path(args.vagas).read_text(encoding="utf-8"))
    if args.max_vagas:
        vagas = vagas[: args.max_vagas]
    perfis = {
        "pt": pathlib.Path(args.perfil_pt).read_text(encoding="utf-8").strip(),
        "en": pathlib.Path(args.perfil_en).read_text(encoding="utf-8").strip(),
    }
    areas_alvo = AREAS_IA if args.escopo == "ia" else AREAS_AMPLAS
    DESATIVADOS.clear()
    if getattr(args, "so_groq", False):
        DESATIVADOS.add("gemini")
    prompt = pathlib.Path(args.prompt) if args.prompt else prompt_padrao()
    system = avaliador.load_system_prompt(prompt)
    saida = pathlib.Path(args.saida)
    saida.mkdir(parents=True, exist_ok=True)
    caminho_estado = saida / "estado.json"
    estado = carregar_estado(caminho_estado)
    delays = {
        "groq": avaliador.PROVIDER_DEFAULTS["groq"]["delay"] if args.delay_groq is None else args.delay_groq,
        "gemini": avaliador.PROVIDER_DEFAULTS["gemini"]["delay"] if args.delay_gemini is None else args.delay_gemini,
    }
    print(f"Prompt: {prompt.name} | escopo: {args.escopo} | vagas: {len(vagas)} | bloqueios ativos: "
          f"{bloqueios_ativos(estado) or 'nenhum'}", flush=True)

    sem_provedor = False
    for vaga in vagas:  # passo 1: triagem
        if estado["vagas"].get(vaga["id"]):
            continue
        resultado = triar(vaga, system, perfis, estado, delays, areas_alvo)
        if resultado is None:
            sem_provedor = True
            salvar_estado(caminho_estado, estado)
            bloqueios = {p: estado["bloqueios"][p] for p in bloqueios_ativos(estado)}
            print(f"Nenhum provedor disponível agora (cota ou instabilidade). Bloqueios até: {bloqueios or 'nenhum'}. "
                  "Parando; rode de novo depois.", flush=True)
            break
        estado["vagas"][vaga["id"]] = resultado
        salvar_estado(caminho_estado, estado)
        print(f"[triagem] {vaga['id']}: {resultado['status']} ({resultado.get('provedor') or 'código'})", flush=True)

    for vaga in vagas:  # passo 2: verificação das vagas triadas como "candidatar"
        registro = estado["vagas"].get(vaga["id"])
        if not registro or registro["status"] != "triada":
            continue
        if verificar(vaga, registro, system, perfis, estado, delays, args.exigir_verificacao, areas_alvo):
            print(f"[verificação] {vaga['id']}: {registro['status']} ({registro['motivo']})", flush=True)
        salvar_estado(caminho_estado, estado)

    contagem = escrever_fila(vagas, estado, saida)
    print("Resumo:", {chave: valor for chave, valor in contagem.items() if valor}, flush=True)
    print(f"Fila de revisão: {saida / 'fila_revisao.md'}", flush=True)
    pendentes = contagem["pendente"] + contagem["triada"]
    return EXIT_PENDENTE if (sem_provedor or pendentes) else 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--vagas", required=True, help="JSON: lista de {id, titulo, empresa, texto, url, fonte, idioma}")
    parser.add_argument("--perfil-pt", default=str(HERE / "dados" / "perfil.pt.md"), help="currículo em português (texto)")
    parser.add_argument("--perfil-en", default=str(HERE / "dados" / "perfil.en.md"), help="currículo em inglês (texto)")
    parser.add_argument("--prompt", default=None, help="padrão: automacao-candidaturas-rag.v6.md")
    parser.add_argument("--escopo", choices=["amplo", "ia"], default="amplo",
                        help="amplo: IA, full stack e back-end (IA primeiro na fila); ia: só vagas centradas em IA")
    parser.add_argument("--saida", default=str(HERE / "saida"))
    parser.add_argument("--max-vagas", type=int, default=None)
    parser.add_argument("--delay-groq", type=float, default=None)
    parser.add_argument("--delay-gemini", type=float, default=None)
    parser.add_argument("--so-groq", action="store_true",
                        help="não chama o Gemini (teste): vagas aprovadas ficam como 'fonte única'")
    parser.add_argument("--exigir-verificacao", action="store_true",
                        help="não usa 'fonte única': espera o outro modelo ter cota")
    sys.exit(executar(parser.parse_args()))


if __name__ == "__main__":
    main()
