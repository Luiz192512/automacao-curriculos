"""Coletor de vagas a partir de fontes públicas feitas para isso: Remotive, e quadros Greenhouse e Lever.

Uso (a partir da pasta prompts/):
    python coletor.py --saida dados/vagas.json --max 40

Fontes:
  - Remotive (API pública e gratuita de vagas remotas). Regras do serviço, respeitadas aqui: no máximo 4
    consultas por dia (o coletor guarda um contador e um cache em dados/cache/), no máximo 2 por minuto, e
    cada vaga mantém o link para a página da Remotive e a fonte "Remotive". Não republique essas vagas em
    outros sites.
  - Jobicy (API pública e gratuita de vagas remotas, com filtro por região: Brasil, LATAM e qualquer lugar).
    Pede-se dar crédito à Jobicy e manter o link da vaga, o que o pipeline faz no campo "fonte". Há cache diário.
  - Greenhouse e Lever: quadros públicos de empresas listadas em dados/empresas.json, no formato
    {"greenhouse": ["slug"], "lever": ["slug"]}. O slug é o nome na URL do quadro de vagas da empresa.

Filtros em código, para não gastar chamadas de modelo à toa: títulos fora do perfil (front-end puro, dados,
mobile, DevOps, design, vendas e semelhantes) e vagas de nível sênior saem, a não ser que --incluir-senior seja
usado. Vagas com restrição geográfica incompatível com o Brasil também saem.
"""

import argparse
import datetime as dt
import html
import json
import pathlib
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = pathlib.Path(__file__).resolve().parent
CACHE = HERE / "dados" / "cache"
REMOTIVE_URL = "https://remotive.com/api/remote-jobs"
REMOTIVE_MAX_POR_DIA = 4
USER_AGENT = "candidaturas-coletor/1.0 (uso pessoal)"
MAX_CARACTERES_TEXTO = 5000


CACHE_VALIDADE_HORAS = 6


def cache_valido(arquivo: pathlib.Path, horas: float = CACHE_VALIDADE_HORAS) -> bool:
    """O cache vale por algumas horas (e não pelo dia inteiro), para cada rodada ver as vagas novas."""
    return arquivo.exists() and (time.time() - arquivo.stat().st_mtime) < horas * 3600

TITULO_ALVO = re.compile(
    r"back[\s-]?end|full[\s-]?stack|software (engineer|developer)|developer|engineer|\bai\b|llm|python|node|"
    r"typescript|react|java\b|golang|\bgo\b|desenvolvedor|desenvolvedora|programador|engenheir[oa]|software", re.I)
TITULO_FORA = re.compile(
    r"front[\s-]?end(?![\s-]*(and|&)?[\s-]*back)|data (engineer|scientist|analyst)|machine learning|\bml\b|mobile|\bios\b|"
    r"android|devops|\bsre\b|designer|\bqa\b|\btest|support|sales|marketing|recruiter|writer|account|customer|"
    r"security engineer|embedded|firmware|blockchain|solidity|salesforce|sap\b|\.net\b|php|ruby|rails|c\+\+|"
    r"service desk|help ?desk|administrator|assistant|reviewer|coordinator|linguist|localization|specialist|"
    r"banco de talentos|talent (pool|bank)|analista de (dados|bi|qualidade|suporte|infra|sistemas|neg[óo]cios)|cientista|"
    r"\bbi\b|suporte|seguran[çc]a|qualidade|\bteste|\brpa\b|power ?bi|\bdba\b|abap|cobol|delphi|c#|protheus|totvs|oracle|\berp\b|"
    r"engenheir[oa](\(a\))? (de|em) dados|\bq\.?a\b|researcher|pesquisador", re.I)
TITULO_SENIOR = re.compile(
    r"senior|\bsr\b\.?|staff|principal|\blead\b|head of|director|manager|architect|\bvp\b|"
    r"s[êe]nior|pleno|especialista|\bpl\b|\biii\b|\biv\b|l[íi]der|coordenador|gerente|arquitet[oa]|\bhead\b", re.I)
TITULO_JUNIOR = re.compile(r"junior|\bjr\b\.?|trainee|intern|entry|graduate|associate|j[uú]nior|est[aá]gio", re.I)
LOCAL_ACEITO = ("worldwide", "anywhere", "global", "brazil", "brasil", "latam", "latin america", "south america",
                "americas", "remote")
LOCAL_RECUSADO = re.compile(r"\b(usa|us|u\.s\.|united states|canada|uk|united kingdom|europe|eu|emea|apac|india)\b[^,;/]*\b(only|based|residents?)\b|"
                            r"^(usa|us|united states|uk|canada|europe|india)$", re.I)


def requisitar(url: str):
    pedido = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urllib.request.urlopen(pedido, timeout=60) as resposta:
        return json.loads(resposta.read().decode("utf-8"))


def html_para_texto(bruto: str) -> str:
    texto = html.unescape(bruto or "")
    texto = re.sub(r"(?is)<(script|style).*?</\1>", " ", texto)
    texto = re.sub(r"(?i)<br\s*/?>|</(p|div|li|h[1-6]|ul|ol|tr)>", "\n", texto)
    texto = re.sub(r"<[^>]+>", " ", texto)
    texto = html.unescape(texto)
    texto = re.sub(r"[ \t\r\f\v]+", " ", texto)
    texto = re.sub(r"\n\s*\n+", "\n", texto).strip()
    if len(texto) <= MAX_CARACTERES_TEXTO:
        return texto
    # Texto longo: guarda o começo e, principalmente, o fim, onde costumam estar os requisitos.
    inicio = int(MAX_CARACTERES_TEXTO * 0.4)
    return texto[:inicio] + "\n...\n" + texto[-(MAX_CARACTERES_TEXTO - inicio):]


def titulo_serve(titulo: str, incluir_senior: bool) -> bool:
    if not TITULO_ALVO.search(titulo) or TITULO_FORA.search(titulo):
        return False
    return incluir_senior or not TITULO_SENIOR.search(titulo)


def local_serve(local: str) -> bool:
    local = (local or "").strip().lower()
    if not local:
        return True
    if LOCAL_RECUSADO.search(local):
        return False
    return any(termo in local for termo in LOCAL_ACEITO)


# ---------- Remotive ----------

def consultar_remotive() -> list:
    CACHE.mkdir(parents=True, exist_ok=True)
    hoje = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
    arquivo_cache = CACHE / f"remotive_{hoje}.json"
    if cache_valido(arquivo_cache):
        print("[remotive] usando o cache de hoje, sem nova consulta.", flush=True)
        return json.loads(arquivo_cache.read_text(encoding="utf-8"))
    contador_arquivo = CACHE / "remotive_contador.json"
    contador = json.loads(contador_arquivo.read_text(encoding="utf-8")) if contador_arquivo.exists() else {}
    if contador.get(hoje, 0) >= REMOTIVE_MAX_POR_DIA:
        print("[remotive] limite de 4 consultas por dia atingido; pulando.", flush=True)
        return []
    url = f"{REMOTIVE_URL}?" + urllib.parse.urlencode({"category": "software-dev"})
    print("[remotive] consultando a API (1 de no máximo 4 por dia)...", flush=True)
    dados = requisitar(url)
    contador[hoje] = contador.get(hoje, 0) + 1
    contador_arquivo.write_text(json.dumps(contador), encoding="utf-8")
    vagas = dados.get("jobs", [])
    arquivo_cache.write_text(json.dumps(vagas, ensure_ascii=False), encoding="utf-8")
    return vagas


def vagas_remotive(incluir_senior: bool) -> list:
    saida = []
    for item in consultar_remotive():
        if not titulo_serve(item.get("title", ""), incluir_senior) or not local_serve(item.get("candidate_required_location", "")):
            continue
        saida.append({
            "id": f"remotive-{item['id']}", "titulo": item["title"], "empresa": item.get("company_name", ""),
            "texto": html_para_texto(item.get("description", "")), "url": item.get("url", ""), "fonte": "Remotive",
            "publicada_em": item.get("publication_date", ""), "local": item.get("candidate_required_location", ""),
        })
    return saida


# ---------- Jobicy ----------

JOBICY_URL = "https://jobicy.com/api/jobs"
JOBICY_GEOS = ["brazil", "latam", "anywhere"]
JOBICY_NIVEIS_SENIORES = re.compile(r"senior|director|executive|lead", re.I)


def consultar_jobicy(geo: str) -> list:
    """API pública e gratuita. Pede-se dar crédito à Jobicy e manter o link da vaga; o cache diário evita repetir consultas."""
    CACHE.mkdir(parents=True, exist_ok=True)
    hoje = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
    arquivo_cache = CACHE / f"jobicy_{geo}_{hoje}.json"
    if cache_valido(arquivo_cache):
        return json.loads(arquivo_cache.read_text(encoding="utf-8"))
    url = f"{JOBICY_URL}?" + urllib.parse.urlencode({"count": 100, "geo": geo, "industry": "engineering"})
    print(f"[jobicy] consultando geo={geo}...", flush=True)
    vagas = requisitar(url).get("jobs", [])
    arquivo_cache.write_text(json.dumps(vagas, ensure_ascii=False), encoding="utf-8")
    time.sleep(2)
    return vagas


def vagas_jobicy(incluir_senior: bool) -> list:
    saida = []
    for geo in JOBICY_GEOS:
        for item in consultar_jobicy(geo):
            titulo, nivel = item.get("jobTitle", ""), item.get("jobLevel", "")
            if not titulo_serve(titulo, incluir_senior) or not local_serve(item.get("jobGeo", "")):
                continue
            if not incluir_senior and JOBICY_NIVEIS_SENIORES.search(nivel):
                continue
            saida.append({
                "id": f"jobicy-{item['id']}", "titulo": titulo, "empresa": item.get("companyName", ""),
                "texto": html_para_texto(item.get("jobDescription", "")), "url": item.get("url", ""), "fonte": "Jobicy",
                "publicada_em": item.get("pubDate", ""), "local": item.get("jobGeo", ""), "nivel": nivel,
            })
    return saida


# ---------- Gupy ----------

GUPY_URL = "https://portal.gupy.io/job-search/term="
GUPY_TERMOS_PADRAO = [
    "desenvolvedor junior", "desenvolvedor backend", "desenvolvedor fullstack", "desenvolvedor node",
    "desenvolvedor python", "desenvolvedor react", "desenvolvedor java", "trainee desenvolvedor",
    "estagio desenvolvimento de software", "engenheiro de software", "inteligencia artificial",
]
NEXT_DATA = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)


def pagina_html(url: str) -> str:
    pedido = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (candidaturas-coletor uso pessoal)", "Accept": "text/html"})
    with urllib.request.urlopen(pedido, timeout=60) as resposta:
        return resposta.read().decode("utf-8", "replace")


GUPY_MAX_PAGINAS = 3  # 12 vagas por página, então até 36 por termo
GUPY_CACHE_HORAS = 12


def consultar_gupy(termo: str, pagina: int = 1) -> dict:
    """Lê uma página (12 vagas) da busca pública do portal da Gupy, como um visitante, com cache.

    A Gupy não documenta uma API pública para candidatos; a lista vem do JSON que o próprio portal embute na
    página de busca (a página N sai com "?page=N"). O robots.txt do portal permite o acesso. Mantenha poucas
    consultas e o cache ligado. Devolve {"data": [vagas], "total": total de resultados do termo, "limite": 12}.
    """
    CACHE.mkdir(parents=True, exist_ok=True)
    hoje = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
    arquivo_cache = CACHE / f"gupy_{re.sub(r'[^a-z0-9]+', '-', termo.lower())}_p{pagina}_{hoje}.json"
    if cache_valido(arquivo_cache, GUPY_CACHE_HORAS):
        return json.loads(arquivo_cache.read_text(encoding="utf-8"))
    print(f"[gupy] buscando '{termo}' página {pagina}...", flush=True)
    url = GUPY_URL + urllib.parse.quote(termo) + (f"?page={pagina}" if pagina > 1 else "")
    achado = NEXT_DATA.search(pagina_html(url))
    lista = json.loads(achado.group(1))["props"]["pageProps"]["initialJobList"] if achado else {}
    paginacao = lista.get("pagination") or {}
    resultado = {"data": lista.get("data", []), "total": paginacao.get("total", 0), "limite": paginacao.get("limit", 12) or 12}
    arquivo_cache.write_text(json.dumps(resultado, ensure_ascii=False), encoding="utf-8")
    time.sleep(2)
    return resultado


def itens_do_termo(termo: str) -> list:
    primeira = consultar_gupy(termo, 1)
    itens = list(primeira["data"])
    paginas = min(GUPY_MAX_PAGINAS, -(-primeira["total"] // primeira["limite"]))
    for numero in range(2, paginas + 1):
        itens += consultar_gupy(termo, numero)["data"]
    return itens


def vagas_gupy(termos: list, incluir_senior: bool, aceitar_hibrido: bool) -> list:
    saida = []
    for termo in termos:
        try:
            itens = itens_do_termo(termo)
        except (urllib.error.URLError, TimeoutError, ValueError, KeyError) as erro:
            print(f"[gupy] '{termo}' falhou: {erro}", flush=True)
            continue
        for item in itens:
            titulo, modelo = item.get("name", ""), item.get("workplaceType", "")
            if "talent_pool" in item.get("type", ""):
                continue  # banco de talentos: não é uma vaga aberta
            if not (modelo == "remote" or (aceitar_hibrido and modelo == "hybrid")) or not titulo_serve(titulo, incluir_senior):
                continue
            saida.append({
                "id": f"gupy-{item['id']}", "titulo": titulo, "empresa": item.get("careerPageName", ""),
                "texto": html_para_texto(item.get("description", "")), "url": item.get("jobUrl", ""), "fonte": "Gupy",
                "publicada_em": item.get("publishedDate", ""), "local": f"Brazil ({modelo})", "nivel": item.get("type", ""),
            })
    return saida


# ---------- InHire ----------

INHIRE_API = "https://api.inhire.app/job-posts/public/pages"


def requisitar_inhire(url: str, tenant: str) -> dict:
    pedido = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json", "x-tenant": tenant})
    with urllib.request.urlopen(pedido, timeout=60) as resposta:
        return json.loads(resposta.read().decode("utf-8"))


def local_brasil(local: str) -> bool:
    local = (local or "").strip()
    return not local or local.upper().endswith("BR") or "brazil" in local.lower() or "brasil" in local.lower()


def vagas_inhire(tenant: str, incluir_senior: bool, aceitar_hibrido: bool) -> list:
    """Quadro público de vagas de uma empresa que usa a InHire (o mesmo que a página tenant.inhire.app/vagas carrega)."""
    CACHE.mkdir(parents=True, exist_ok=True)
    hoje = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
    arquivo_cache = CACHE / f"inhire_{tenant}_{hoje}.json"
    if cache_valido(arquivo_cache):
        return json.loads(arquivo_cache.read_text(encoding="utf-8"))
    lista = requisitar_inhire(INHIRE_API, tenant).get("jobsPage", [])
    time.sleep(1)
    saida = []
    for item in lista:
        titulo, modelo = item.get("displayName", ""), item.get("workplaceType", "")
        if item.get("status") != "published" or not titulo_serve(titulo, incluir_senior):
            continue
        if not (modelo == "Remote" or (aceitar_hibrido and modelo == "Hybrid")) or not local_brasil(item.get("location", "")):
            continue
        detalhe = requisitar_inhire(f"{INHIRE_API}/{item['jobId']}", tenant)
        time.sleep(1)
        saida.append({
            "id": f"inhire-{tenant}-{item['jobId']}", "titulo": titulo, "empresa": detalhe.get("tenantName") or tenant,
            "texto": html_para_texto(detalhe.get("description", "")), "url": f"https://{tenant}.inhire.app/vagas/{item['jobId']}",
            "fonte": f"InHire ({tenant})", "publicada_em": detalhe.get("publishedAt", ""),
            "local": f"Brazil ({modelo})", "nivel": ", ".join(detalhe.get("contractType") or []),
        })
    arquivo_cache.write_text(json.dumps(saida, ensure_ascii=False), encoding="utf-8")
    return saida


# ---------- Greenhouse e Lever ----------

# Quadros de empresas grandes misturam todas as áreas; aqui o título precisa ser de desenvolvimento de software.
TITULO_SOFTWARE = re.compile(
    r"software|developer|back[\s-]?end|full[\s-]?stack|\bai\b|llm|python|node|typescript|react|golang|\bgo\b|java\b", re.I)
TITULO_NAO_SOFTWARE = re.compile(
    r"advocate|solutions?|sales|field|alliances?|customer|partner|account|abuse|trust|design|analytics|data|"
    r"support|success|manager|recruit|finance|legal|marketing|technical writer|sales engineer", re.I)
MAX_POR_QUADRO = 5
# "Remote - US" ou "Remote, Canada" não servem para quem mora no Brasil: nos quadros estrangeiros o local precisa citar
# o mundo todo, as Américas, a América Latina ou o Brasil.
LOCAL_ABERTO = re.compile(r"worldwide|anywhere|global|brazil|brasil|latam|latin america|south america|americas", re.I)


def local_aberto_serve(local: str) -> bool:
    return bool(LOCAL_ABERTO.search(local or "")) and local_serve(local)


def titulo_software_serve(titulo: str, incluir_senior: bool) -> bool:
    return bool(TITULO_SOFTWARE.search(titulo)) and not TITULO_NAO_SOFTWARE.search(titulo) and titulo_serve(titulo, incluir_senior)


def vagas_greenhouse(slug: str, incluir_senior: bool) -> list:
    dados = requisitar(f"https://boards-api.greenhouse.io/v1/boards/{urllib.parse.quote(slug)}/jobs?content=true")
    saida = []
    for item in dados.get("jobs", []):
        local = (item.get("location") or {}).get("name", "")
        if not titulo_software_serve(item.get("title", ""), incluir_senior) or not local_aberto_serve(local):
            continue
        if len(saida) >= MAX_POR_QUADRO:
            break
        saida.append({
            "id": f"greenhouse-{slug}-{item['id']}", "titulo": item["title"], "empresa": slug,
            "texto": html_para_texto(item.get("content", "")), "url": item.get("absolute_url", ""),
            "fonte": f"Greenhouse ({slug})", "publicada_em": item.get("updated_at", ""), "local": local,
        })
    return saida


def vagas_lever(slug: str, incluir_senior: bool) -> list:
    dados = requisitar(f"https://api.lever.co/v0/postings/{urllib.parse.quote(slug)}?mode=json")
    saida = []
    for item in dados:
        local = (item.get("categories") or {}).get("location", "")
        if not titulo_software_serve(item.get("text", ""), incluir_senior) or not local_aberto_serve(local):
            continue
        if len(saida) >= MAX_POR_QUADRO:
            break
        listas = " ".join(f"{bloco.get('text', '')} {html_para_texto(bloco.get('content', ''))}" for bloco in item.get("lists", []))
        texto = f"{item.get('descriptionPlain', '')} {listas} {item.get('additionalPlain', '')}".strip()
        criada = item.get("createdAt")
        saida.append({
            "id": f"lever-{slug}-{item['id']}", "titulo": item["text"], "empresa": slug, "texto": texto[:MAX_CARACTERES_TEXTO],
            "url": item.get("hostedUrl", ""), "fonte": f"Lever ({slug})",
            "publicada_em": dt.datetime.fromtimestamp(criada / 1000, dt.timezone.utc).isoformat() if criada else "", "local": local,
        })
    return saida


# ---------- execução ----------

def pontuar(vaga: dict) -> tuple:
    junior = 0 if (TITULO_JUNIOR.search(vaga["titulo"]) or re.search(r"junior|entry", vaga.get("nivel", ""), re.I)) else 1
    ia = 0 if re.search(r"\bai\b|llm|machine|generative", vaga["titulo"] + " " + vaga["texto"][:400], re.I) else 1
    return (junior, ia, "".join(chr(255 - ord(c)) for c in (vaga.get("publicada_em") or "")[:10]))


def coletar(args) -> list:
    todas = []
    if not args.sem_remotive:
        try:
            todas += vagas_remotive(args.incluir_senior)
        except (urllib.error.URLError, TimeoutError) as erro:
            print(f"[remotive] falhou: {erro}", flush=True)
    if not args.sem_jobicy:
        try:
            todas += vagas_jobicy(args.incluir_senior)
        except (urllib.error.URLError, TimeoutError) as erro:
            print(f"[jobicy] falhou: {erro}", flush=True)
    empresas_arquivo = pathlib.Path(args.empresas)
    empresas = json.loads(empresas_arquivo.read_text(encoding="utf-8")) if empresas_arquivo.exists() else {}
    if not args.sem_gupy:
        todas += vagas_gupy(empresas.get("gupy_termos") or GUPY_TERMOS_PADRAO, args.incluir_senior, args.aceitar_hibrido)
    if not args.sem_inhire:
        for tenant in empresas.get("inhire", []):
            try:
                novas = vagas_inhire(tenant, args.incluir_senior, args.aceitar_hibrido)
                print(f"[inhire] {tenant}: {len(novas)} vagas dentro do perfil", flush=True)
                todas += novas
            except (urllib.error.URLError, TimeoutError, ValueError, KeyError) as erro:
                print(f"[inhire] {tenant}: falhou ({erro})", flush=True)
    for fonte, funcao in (("greenhouse", vagas_greenhouse), ("lever", vagas_lever)):
        for slug in empresas.get(fonte, []):
            try:
                novas = funcao(slug, args.incluir_senior)
                print(f"[{fonte}] {slug}: {len(novas)} vagas dentro do perfil", flush=True)
                todas += novas
            except (urllib.error.URLError, TimeoutError) as erro:
                print(f"[{fonte}] {slug}: falhou ({erro})", flush=True)
            time.sleep(1)
    # Reaplica o filtro de título: o cache guarda vagas filtradas com as regras da data em que foi gravado.
    todas = [vaga for vaga in todas if titulo_serve(vaga["titulo"], args.incluir_senior)]
    vistas, unicas = set(), []
    for vaga in todas:
        chave = (vaga["titulo"].lower(), vaga["empresa"].lower())
        if vaga["id"] in vistas or chave in vistas or not vaga["texto"].strip():
            continue
        vistas.update({vaga["id"], chave})
        unicas.append(vaga)
    unicas.sort(key=pontuar)
    return unicas[: args.max]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--saida", default=str(HERE / "dados" / "vagas.json"))
    parser.add_argument("--empresas", default=str(HERE / "dados" / "empresas.json"))
    parser.add_argument("--max", type=int, default=40, help="máximo de vagas na saída (júnior e IA primeiro)")
    parser.add_argument("--incluir-senior", action="store_true")
    parser.add_argument("--sem-remotive", action="store_true")
    parser.add_argument("--sem-jobicy", action="store_true")
    parser.add_argument("--sem-gupy", action="store_true")
    parser.add_argument("--sem-inhire", action="store_true")
    parser.add_argument("--aceitar-hibrido", action="store_true", help="Gupy e InHire: inclui vagas híbridas (padrão: só remotas)")
    args = parser.parse_args()
    vagas = coletar(args)
    pathlib.Path(args.saida).write_text(json.dumps(vagas, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"{len(vagas)} vagas salvas em {args.saida}", flush=True)
    sys.exit(0 if vagas else 1)


if __name__ == "__main__":
    main()
