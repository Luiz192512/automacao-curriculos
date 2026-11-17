"""Fila de candidaturas pedidas pelo WhatsApp: um worker `claude -p` por vez, com cota e perguntas ao dono.

Uso:  python aplicar_fila.py tick | uso | fila
      python aplicar_fila.py enfileirar gupy-1 gupy-2 [--forcar]
      python aplicar_fila.py responder "#7 salario 4500"
O bot chama `tick` a cada 60 s; conversar.py chama enfileirar/responder. Tudo fica em saida/aplicar/.
Nada roda enquanto saida/aplicar/config.json tiver "ativo": false (padrão). Desenho: saida/analise_aplicar_whatsapp.md.
"""

import datetime as dt
import json
import os
import pathlib
import random
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
import unicodedata
import uuid

import cota as medidor

HERE = pathlib.Path(__file__).resolve().parent
SAIDA = pathlib.Path(os.environ.get("AUTOMACAO_SAIDA", HERE / "saida"))
VAGAS = pathlib.Path(os.environ.get("AUTOMACAO_VAGAS", HERE / "dados" / "vagas.json"))
APROVADAS = pathlib.Path(os.environ.get("AUTOMACAO_APROVADAS", HERE / "dados" / "respostas_aprovadas.json"))
AP = SAIDA / "aplicar"
FILA, PERG, RES, DIA = (AP / n for n in ("fila", "perguntas", "resultados", "diario"))
OUTBOX, ESTADO, COTA, CONFIG = SAIDA / "outbox", SAIDA / "estado.json", AP / "cota.json", AP / "config.json"
TRAVESSOS = re.compile(r"\s*[" + chr(0x2014) + chr(0x2013) + r"]\s*")
ATIVOS = {"na_fila", "aguardando_cota", "executando", "aguardando_dono", "retomando"}
PADRAO = {
    "ativo": False,          # desligado até o dono ligar (nada é aberto nem enviado)
    "dry_run": True,         # o worker faz tudo menos o clique final de envio
    "sonda": False,          # medidor periódico gasta cota; ligar só depois de medir o custo (risco R5)
    "medidor_min": 30,
    "max_por_rodada": 5,
    "carencia_s": 60,
    "timeout_s": 720,
    "silencio_s": 300,
    "modelo": "sonnet",
    "reserva_semana": 0.9,
    "ferramentas": [],       # ferramentas do navegador liberadas ao worker (ver seção 9 do desenho)
    "comando_worker": None,  # lista, só para testes
}


# ---------- utilidades ----------

def ler(caminho, padrao):
    try:
        return json.loads(pathlib.Path(caminho).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return padrao


def gravar(caminho, dados) -> None:
    caminho = pathlib.Path(caminho)
    caminho.parent.mkdir(parents=True, exist_ok=True)
    tmp = caminho.with_name(caminho.name + ".tmp")
    tmp.write_text(json.dumps(dados, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, caminho)


def config() -> dict:
    return {**PADRAO, **ler(CONFIG, {})}


def sem_acento(texto: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", texto.lower()) if unicodedata.category(c) != "Mn").strip()


def avisar(texto: str, prio: int = 1, tipo: str = "aplicar", imagem=None) -> None:
    """Prefixo da prioridade no nome: o bot envia a outbox em ordem de nome (0 pergunta/limite, 1 resultado, 2 uso, 3 lembrete)."""
    OUTBOX.mkdir(parents=True, exist_ok=True)
    item = {"mensagem": TRAVESSOS.sub(", ", texto.strip())[:3400], "tipo": tipo}
    if imagem:
        item["imagem"] = str(imagem)
    nome = f"{prio}_{dt.datetime.now():%Y%m%d-%H%M%S-%f}_{tipo}.json"
    (OUTBOX / nome).write_text(json.dumps(item, ensure_ascii=False), encoding="utf-8")


def pedidos() -> list:
    FILA.mkdir(parents=True, exist_ok=True)
    return [p for p in (ler(f, None) for f in sorted(FILA.glob("*.json"), key=lambda f: int(f.stem) if f.stem.isdigit() else 0)) if p]


def carregar(pid):
    return ler(FILA / f"{pid}.json", None)


def salvar(p: dict) -> None:
    gravar(FILA / f"{p['id']}.json", p)


def novo_id() -> int:
    seq = ler(AP / "seq.json", {"n": 0})
    seq["n"] += 1
    gravar(AP / "seq.json", seq)
    return seq["n"]


def vaga_de(vid: str) -> dict:
    return next((v for v in ler(VAGAS, []) if v.get("id") == vid), {})


def nome(p: dict) -> str:
    v = vaga_de(p["vaga_id"])
    return f"{v.get('titulo', p['vaga_id'])} na {v.get('empresa', '?')}"


def marcar(vaga_id: str, valor: str) -> None:
    estado = ler(ESTADO, None)
    if estado and vaga_id in estado.get("vagas", {}):
        estado["vagas"][vaga_id]["candidatura"] = valor
        gravar(ESTADO, estado)


def clicou(p: dict) -> bool:
    return (DIA / f"{p['id']}.antes_de_enviar").exists()


# ---------- entrada: pedidos e respostas do dono ----------

def enfileirar(ids: list, forcar: bool = False) -> str:
    cfg, estado = config(), ler(ESTADO, {"vagas": {}})
    vagas = {v["id"]: v for v in ler(VAGAS, [])}
    ativos = {p["vaga_id"]: p for p in pedidos() if p["estado"] in ATIVOS}
    linhas, novos = [], []
    for vid in ids:
        reg = estado.get("vagas", {}).get(vid, {})
        cand = str(reg.get("candidatura") or "")
        if vid not in vagas:
            linhas.append(f"Não achei a vaga {vid}. Mande revisao para ver a lista atual.")
        elif vid in ativos:
            linhas.append(f"Já tenho essa vaga como #{ativos[vid]['id']} ({ativos[vid]['estado']}). Não dupliquei.")
        elif cand.startswith("enviada") and not forcar:
            linhas.append(f"Já enviei {vagas[vid].get('titulo')} na {vagas[vid].get('empresa')}. Não vou repetir (para forçar: aplicar forcar N).")
        elif cand == "vaga_encerrada" and not forcar:
            linhas.append(f"{vagas[vid].get('titulo')} já estava marcada como encerrada. Nada foi enviado.")
        elif reg.get("status") == "descartada" and not forcar:
            linhas.append(f"{vagas[vid].get('titulo')} está como descartada. Para aplicar mesmo assim: aplicar forcar N.")
        else:
            p = {"id": novo_id(), "vaga_id": vid, "estado": "na_fila", "pedido_em": dt.datetime.now().isoformat(timespec="seconds"),
                 "inicia_apos": time.time() + cfg["carencia_s"], "session_id": None, "tentativas": 0, "retomar": False}
            salvar(p)
            novos.append(p)
            linhas.append(f"#{p['id']} {nome(p)}")
    if novos:
        if cfg["ativo"]:
            linhas.append(f"Começo em {cfg['carencia_s']} s. Para parar: cancelar #{novos[0]['id']}.")
            if len(novos) > cfg["max_por_rodada"]:
                linhas.append(f"São {len(novos)} vagas: faço {cfg['max_por_rodada']} por rodada para não gastar a cota de uma vez.")
        else:
            linhas.append("A automação de candidatura está desligada (aplicar/config.json, ativo=false). Anotei, mas nada será aberto nem enviado.")
        linhas.insert(0, "Vou aplicar:" if cfg["ativo"] else "Pedidos anotados:")
    return "\n".join(linhas)


def abertas() -> list:
    return [p for p in pedidos() if p["estado"] == "aguardando_dono"]


def _ids(texto: str) -> list:
    return [int(n) for n in re.findall(r"#(\d+)", texto)]


def responder(texto: str):
    """Trata comandos e respostas do fluxo de candidatura. Devolve o texto de resposta, ou None se a mensagem não é daqui."""
    cmd = sem_acento(texto)
    ids = _ids(cmd)
    pid = ids[0] if ids else None
    if cmd in ("uso", "cota"):
        return uso()
    if cmd in ("fila", "pedidos"):
        return fila_texto()
    if cmd == "cancelar fila":
        n = sum(1 for p in pedidos() if p["estado"] in ATIVOS and _encerrar(p, "cancelada"))
        return f"Cancelei {n} pedido(s). O que já foi enviado continua enviado."
    if cmd in ("retomar", "seguir"):
        c = ler(COTA, {})
        c["semana_liberada"] = True
        gravar(COTA, c)
        for p in pedidos():
            if p["estado"] == "aguardando_dono" and (ler(PERG / f"{p['id']}.json", {}).get("tipo") == "sistema"):
                p.update(estado="na_fila", inicia_apos=time.time())
                salvar(p)
        return "Ok, retomando a fila."
    for verbo, acao in (("cancelar", "cancelada"), ("pular", "pulada")):
        if cmd.startswith(verbo + " #") and pid:
            p = carregar(pid)
            if not p:
                return f"Não achei o #{pid}."
            if p["estado"] not in ATIVOS:
                return f"O #{pid} já terminou ({p['estado']})."
            _encerrar(p, acao)
            aviso = " Se já tinha clicado em enviar, confira no portal." if clicou(p) else " Nada foi enviado."
            return f"{'Cancelei' if acao == 'cancelada' else 'Pulei'} o #{pid}.{aviso}"
    if pid and cmd.startswith(("manual #", "feito #")):
        p = carregar(pid)
        if not p:
            return f"Não achei o #{pid}."
        if cmd.startswith("manual"):
            p.update(estado="aguardando_dono", manual=True)
            salvar(p)
            return f"Ok, o #{pid} fica por sua conta. Quando enviar, responda: feito #{pid}."
        _fechar(p, "concluida_confirmada", "enviada")
        return f"Registrei o #{pid} como enviada por você."
    if pid and cmd.startswith("de novo #"):
        p = carregar(pid)
        if not p or p["estado"] in ATIVOS:
            return f"O #{pid} não pode ser reenfileirado agora."
        if clicou(p):
            return f"O #{pid} já chegou a clicar em enviar. Confira no portal e responda: conferi enviou #{pid} ou conferi nao enviou #{pid}."
        p.update(estado="na_fila", inicia_apos=time.time() + config()["carencia_s"], tentativas=0)
        salvar(p)
        return f"Reenfileirei o #{pid}."
    if pid and cmd.startswith("conferi "):
        p = carregar(pid)
        if not p:
            return f"Não achei o #{pid}."
        if "nao enviou" in cmd:
            (DIA / f"{pid}.antes_de_enviar").unlink(missing_ok=True)
            p.update(estado="na_fila", inicia_apos=time.time() + config()["carencia_s"], tentativas=0, retomar=False)
            salvar(p)
            return f"Ok, reenfileirei o #{pid}."
        _fechar(p, "concluida_confirmada", "enviada")
        return f"Registrei o #{pid} como enviada."
    return _resposta_pergunta(texto, cmd, pid)


def _encerrar(p: dict, estado: str) -> bool:
    p["estado"] = estado
    salvar(p)
    if estado == "pulada" and not clicou(p):
        marcar(p["vaga_id"], "pulada:dono")
    return True


def _fechar(p: dict, estado: str, candidatura: str) -> None:
    p["estado"] = estado
    salvar(p)
    marcar(p["vaga_id"], candidatura)


def _resposta_pergunta(texto: str, cmd: str, pid):
    abertas_ = abertas()
    if pid is None:
        if not abertas_:
            return None
        if not re.fullmatch(r"\d|sim|nao|continuar|pular|manual", cmd):
            return None  # texto livre sem número não é resposta: segue para a conversa normal
        if len(abertas_) > 1:
            lista = ", ".join(f"#{p['id']}" for p in abertas_)
            return f"Tenho {len(abertas_)} perguntas abertas ({lista}). Responda com o número, por exemplo: #{abertas_[0]['id']} {texto.strip()}."
        pid = abertas_[0]["id"]
        corpo = texto.strip()
    else:
        corpo = re.sub(r"\s*#\d+\s*", " ", texto).strip()
        if cmd.startswith("continuar"):
            corpo = "continuar"
    p = carregar(pid)
    if not p:
        return f"Não achei o #{pid}."
    if p["estado"] != "aguardando_dono":
        return f"O #{pid} não está esperando resposta ({p['estado']})."
    perg = ler(PERG / f"{pid}.json", {})
    if sem_acento(corpo) == "pular":
        _encerrar(p, "pulada")
        return f"Pulei o #{pid}. Nada foi enviado."
    if perg.get("tipo") == "sistema":
        return _resposta_sistema(p, corpo)
    lembrar = sem_acento(corpo).endswith(" lembrar")
    if lembrar:
        corpo = corpo[: -len("lembrar")].strip()
    if perg.get("tipo") == "escolha" and corpo.isdigit() and 1 <= int(corpo) <= len(perg.get("opcoes") or []):
        corpo = perg["opcoes"][int(corpo) - 1]
    if perg.get("tipo") == "campo_aberto" and perg.get("chave") and sem_acento(corpo).startswith(sem_acento(perg["chave"])):
        corpo = corpo[len(perg["chave"]):].strip()
    if lembrar and perg.get("chave"):
        guardadas = ler(APROVADAS, {})
        guardadas[perg["chave"]] = corpo
        gravar(APROVADAS, guardadas)
    p.update(estado="retomando", resposta=corpo, retomar=True, inicia_apos=time.time())
    salvar(p)
    return f"Recebi, continuando o #{pid}."


def _resposta_sistema(p: dict, corpo: str) -> str:
    """Pergunta criada pelo próprio sistema (retries esgotados na cota): 1 tentar em 1 h, 2 esperar até amanhã, 3 cancelar."""
    opcao = sem_acento(corpo)
    if opcao == "3":
        _encerrar(p, "cancelada")
        return f"Cancelei o #{p['id']}."
    if opcao in ("1", "2"):
        p.update(estado="na_fila", inicia_apos=time.time() + (3600 if opcao == "1" else 86400))
        salvar(p)
        c = ler(COTA, {})
        c["tentativas_retry"] = 0
        gravar(COTA, c)
        return f"Ok, tento de novo {'em 1 hora' if opcao == '1' else 'amanhã'}."
    return "Responda 1 (tentar em 1 h), 2 (esperar até amanhã) ou 3 (cancelar)."


def uso() -> str:
    cfg, c, t = config(), ler(COTA, {}), time.time()
    if cfg["sonda"] and t - c.get("lido_em", 0) > 600:
        info = medidor.sonda()
        if info:
            for prio, aviso in medidor.registrar(c, info, t):
                avisar(aviso, prio, "uso")
            gravar(COTA, c)
    return medidor.resumo(c, t)


def fila_texto() -> str:
    ativos = [p for p in pedidos() if p["estado"] in ATIVOS]
    if not ativos:
        return "Fila vazia."
    return "Fila:\n" + "\n".join(f"#{p['id']} {nome(p)}: {p['estado']}" for p in ativos)


# ---------- laço: tick ----------

def tick() -> None:
    AP.mkdir(parents=True, exist_ok=True)
    trava = AP / "tick.lock"
    if trava.exists() and time.time() - trava.stat().st_mtime < 900:
        return
    trava.write_text(str(os.getpid()))
    try:
        _tick()
    finally:
        trava.unlink(missing_ok=True)


def _tick() -> None:
    cfg = config()
    if not cfg["ativo"]:
        return
    recuperar_orfaos()
    prazos()
    c, t = ler(COTA, {}), time.time()
    if cfg["sonda"] and t - c.get("lido_em", 0) > cfg["medidor_min"] * 60:
        info = medidor.sonda()
        if info:
            for prio, aviso in medidor.registrar(c, info, t):
                avisar(aviso, prio, "uso")
            b = medidor.bloqueio(info)
            if b:
                _bloquear(c, b, t)
            gravar(COTA, c)
    bloq = c.get("bloqueado_ate")
    if bloq and t < bloq:
        for p in pedidos():
            if p["estado"] == "na_fila" and p["inicia_apos"] <= t:
                p["estado"] = "aguardando_cota"
                salvar(p)
        return
    if bloq:  # a hora de volta chegou: a próxima execução é a própria nova tentativa
        c["bloqueado_ate"] = None
        gravar(COTA, c)
        esperando = [p for p in pedidos() if p["estado"] == "aguardando_cota"]
        for p in esperando:
            p.update(estado="na_fila", inicia_apos=t)
            salvar(p)
        if esperando:
            avisar(f"Cota voltou. Retomando a fila ({len(esperando)} vaga(s)), uma por vez.", 0, "cota")
    devidos = [p for p in pedidos() if p["estado"] in ("na_fila", "retomando") and p["inicia_apos"] <= time.time()]
    devidos.sort(key=lambda p: (p["estado"] != "retomando", p["id"]))
    feitos = 0
    for p in devidos:
        c = ler(COTA, {})
        sem = (c.get("semana") or {}).get("utilizacao") or 0
        if sem >= cfg["reserva_semana"] and not c.get("semana_liberada"):
            if not c.get("semana_perguntada"):
                c["semana_perguntada"] = True
                gravar(COTA, c)
                avisar(f"Semana em {round(sem * 100)}%. Para não zerar a cota, não começo novas vagas sozinho. Responda: seguir (continuar mesmo assim) ou espere o reset da semana.", 0, "cota")
            break
        if feitos >= cfg["max_por_rodada"]:
            break
        executar(p)
        feitos += 1
        if (ler(COTA, {}).get("bloqueado_ate") or 0) > time.time():
            break


def prazos() -> None:
    t = time.time()
    for p in abertas():
        perg = ler(PERG / f"{p['id']}.json", {})
        if perg.get("tipo") == "sistema":
            continue
        if t > p.get("expira_em", t + 1):
            _encerrar(p, "expirada")
            avisar(f"#{p['id']} expirou sem resposta. Nada foi enviado.", 1)
        elif t > p.get("lembrete_em", t + 1) and not p.get("lembrado"):
            p["lembrado"] = True
            salvar(p)
            avisar(f"Lembrete: #{p['id']} ainda espera sua resposta. {perg.get('pergunta', '')}", 3, "lembrete")


def recuperar_orfaos() -> None:
    for p in pedidos():
        if p["estado"] != "executando":
            continue
        if clicou(p):
            _fechar(p, "concluida_sem_confirmacao", "enviada_sem_confirmacao")
            avisar(f"#{p['id']} {nome(p)} estava rodando quando o programa parou e já tinha clicado em enviar. Não reenviei. Confira no portal e responda: conferi enviou #{p['id']} ou conferi nao enviou #{p['id']}.", 0)
        elif p["tentativas"] >= 2:
            _fechar(p, "falhou", f"falhou:interrompido")
            avisar(f"Falhou: {nome(p)} (#{p['id']}). O robô foi interrompido 2 vezes. Nada foi enviado. Para tentar de novo: de novo #{p['id']}", 1)
        else:
            p.update(estado="na_fila", inicia_apos=time.time(), retomar=False)
            salvar(p)
            avisar(f"#{p['id']} foi interrompido (o PC ou o bot reiniciou). Nada foi enviado. Tentando de novo.", 1)


# ---------- worker ----------

def montar(p: dict, retomar: bool):
    cfg = config()
    if cfg["comando_worker"]:
        return list(cfg["comando_worker"]) + [str(p["id"])] + (["retomar"] if retomar else []), None
    exe = shutil.which("claude")
    if not exe:
        return None, "não encontrei o comando claude neste PC"
    if not cfg["ferramentas"] or not (HERE / "worker_mcp.json").exists():
        return None, "falta configurar o navegador do robô (config.json: ferramentas, e worker_mcp.json)"
    v = vaga_de(p["vaga_id"])
    entrada = {"pedido": {"id": p["id"], "vaga_id": p["vaga_id"], "url": v.get("url"), "titulo": v.get("titulo"), "empresa": v.get("empresa"),
                          "dry_run": cfg["dry_run"]}, "resposta": p.get("resposta")}
    cmd = [exe, "-p", json.dumps(entrada, ensure_ascii=False), "--output-format", "stream-json", "--verbose", "--model", cfg["modelo"],
           "--permission-mode", "dontAsk", "--permission-prompts", "none", "--strict-mcp-config", "--mcp-config", str(HERE / "worker_mcp.json"),
           "--disable-slash-commands", "--append-system-prompt", (HERE / "worker_prompt.md").read_text(encoding="utf-8"),
           "--allowedTools", *cfg["ferramentas"], "Read", "Write"]
    cmd += ["--resume", p["session_id"]] if retomar and p.get("session_id") else ["--session-id", p["session_id"]]
    return cmd, None


def executar(p: dict) -> None:
    cfg = config()
    retomar = bool(p.get("retomar")) and bool(p.get("session_id") or cfg["comando_worker"])
    if not retomar:
        p["session_id"] = str(uuid.uuid4())
        p["tentativas"] = p.get("tentativas", 0) + 1
    p["estado"] = "executando"
    salvar(p)
    cmd, erro = montar(p, retomar)
    if erro:
        _fechar(p, "falhou", "falhou:configuracao")
        avisar(f"Não consegui iniciar o #{p['id']} ({nome(p)}): {erro}. Nada foi enviado.", 0)
        return
    (RES / f"{p['id']}.json").unlink(missing_ok=True)
    estado_run = {"bloq": None, "erro": "", "ultimo": time.time(), "timeout": False}
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace", cwd=HERE,
                            creationflags=0x08000000 if os.name == "nt" else 0)
    leitor = threading.Thread(target=_ler_saida, args=(proc, estado_run), daemon=True)
    leitor.start()
    inicio = time.time()
    while proc.poll() is None:
        agora = time.time()
        atual = carregar(p["id"]) or {}
        if atual.get("estado") == "cancelada" or agora - inicio > cfg["timeout_s"] or agora - estado_run["ultimo"] > cfg["silencio_s"]:
            estado_run["timeout"] = atual.get("estado") != "cancelada"
            proc.kill()
            break
        time.sleep(0.5)
    proc.wait()
    leitor.join(5)
    concluir(carregar(p["id"]) or p, estado_run)


def _ler_saida(proc, run: dict) -> None:
    for linha in proc.stdout:
        run["ultimo"] = time.time()
        info = medidor.evento(linha)
        if info is not None:
            c = ler(COTA, {})
            for prio, aviso in medidor.registrar(c, info, time.time()):
                avisar(aviso, prio, "uso")
            gravar(COTA, c)
            run["bloq"] = medidor.bloqueio(info) or run["bloq"]
            continue
        try:
            obj = json.loads(linha)
        except ValueError:
            continue
        if isinstance(obj, dict) and obj.get("type") == "result" and obj.get("is_error"):
            run["erro"] = str(obj.get("result"))[:300]


def _bloquear(c: dict, b: dict, t: float) -> float:
    volta = (b["volta"] or t + 1800) + 90 + random.randint(0, 30)
    c["bloqueado_ate"] = volta
    c["janela_bloqueada"] = b["janela"]
    return volta


def concluir(p: dict, run: dict) -> None:
    if p["estado"] == "cancelada":
        avisar(f"Cancelei o #{p['id']}." + (" Já tinha clicado em enviar: confira no portal." if clicou(p) else " Nada foi enviado."), 1)
        return
    res = ler(RES / f"{p['id']}.json", None)
    bloq = run["bloq"]
    if res and res.get("estado") in ("concluida_confirmada", "concluida_sem_confirmacao", "pulada", "aguardando_dono", "falhou", "dry_run_ok"):
        _zerar_retries()
        return _aplicar_resultado(p, res)
    if bloq:
        return _parar_por_cota(p, bloq)
    if clicou(p):
        _fechar(p, "concluida_sem_confirmacao", "enviada_sem_confirmacao")
        avisar(f"Enviada sem confirmação: {nome(p)} (#{p['id']}). O robô parou depois de clicar em enviar e não deixou resultado. Não reenviei. Confira no portal e responda: conferi enviou #{p['id']} ou conferi nao enviou #{p['id']}.", 0)
    elif p["tentativas"] >= 2:
        _fechar(p, "falhou", "falhou:worker")
        motivo = "passou do tempo" if run["timeout"] else "travou"
        avisar(f"Falhou: {nome(p)} (#{p['id']}). O robô {motivo} 2 vezes. Nada foi enviado. {run['erro']}\nPara tentar de novo: de novo #{p['id']}", 1)
    else:
        p.update(estado="na_fila", inicia_apos=time.time() + 60, retomar=False)
        salvar(p)
        avisar(f"#{p['id']} O robô {'passou do tempo' if run['timeout'] else 'travou'}. Nada foi enviado. Tentando de novo uma vez.", 1)


def _zerar_retries() -> None:
    c = ler(COTA, {})
    if c.get("tentativas_retry"):
        c["tentativas_retry"] = 0
        gravar(COTA, c)


def _parar_por_cota(p: dict, bloq: dict) -> None:
    t = time.time()
    c = ler(COTA, {})
    if clicou(p):
        _fechar(p, "concluida_sem_confirmacao", "enviada_sem_confirmacao")
        avisar(f"O limite acabou logo depois que cliquei em enviar o #{p['id']} ({nome(p)}). Não sei se passou e não vou reenviar. Confira no portal e responda: conferi enviou #{p['id']} ou conferi nao enviou #{p['id']}.", 0, "cota")
        volta = _bloquear(c, bloq, t)
        gravar(COTA, c)
        return
    c["tentativas_retry"] = c.get("tentativas_retry", 0) + 1
    if c["tentativas_retry"] > 3:
        p["estado"] = "aguardando_dono"
        salvar(p)
        gravar(PERG / f"{p['id']}.json", {"id": p["id"], "tipo": "sistema", "pergunta": "limite estourou de novo"})
        gravar(COTA, c)
        avisar(f"Tentei {c['tentativas_retry'] - 1} vezes depois da volta e o limite estourou de novo. Parei no #{p['id']}. Responda: #{p['id']} 1 (tentar em 1 h), #{p['id']} 2 (esperar até amanhã) ou #{p['id']} 3 (cancelar).", 0, "cota")
        return
    volta = _bloquear(c, bloq, t)
    gravar(COTA, c)
    p.update(estado="aguardando_cota", retomar=bool(p.get("session_id")))
    salvar(p)
    if c.get("aviso_bloqueio") != volta // 10:
        c["aviso_bloqueio"] = volta // 10
        gravar(COTA, c)
        na_fila = len([x for x in pedidos() if x["estado"] in ("aguardando_cota", "na_fila", "retomando")])
        quando = medidor.hora(volta, t) if bloq["volta"] else "em horário não informado (tento a cada 30 min)"
        avisar(f"Limite de {bloq['janela']} atingido. Volta {quando}. Vou tentar de novo sozinho nesse horário. Na fila: {na_fila} vaga(s). Para cancelar tudo: cancelar fila", 0, "cota")


def _aplicar_resultado(p: dict, res: dict) -> None:
    e, v = res["estado"], vaga_de(p["vaga_id"])
    url = v.get("url", "")
    if e == "concluida_confirmada":
        _fechar(p, e, "enviada")
        linhas = [f"Candidatura enviada e confirmada: {nome(p)} (#{p['id']})", f"Fonte: {v.get('fonte', '?')}"]
        if res.get("cv_usado"):
            linhas.append(f"CV usado: {res['cv_usado']}")
        if res.get("respostas_enviadas"):
            linhas.append("Respostas: " + "; ".join(f"{k} {val}" for k, val in res["respostas_enviadas"].items()))
        linhas += ["Confirmada na tela do portal.", url]
        avisar("\n".join(linhas), 1)
    elif e == "concluida_sem_confirmacao":
        _fechar(p, e, "enviada_sem_confirmacao")
        avisar(f"Enviada sem confirmação: {nome(p)} (#{p['id']}). Cliquei em enviar mas a tela não confirmou. Vale conferir no portal. {url}\nResponda: conferi enviou #{p['id']} ou conferi nao enviou #{p['id']}", 0)
    elif e == "pulada":
        _fechar(p, e, f"pulada:{str(res.get('motivo') or 'sem motivo')[:60]}")
        avisar(f"Pulada: {nome(p)} (#{p['id']}). {res.get('motivo') or 'sem motivo informado'}. Nada foi enviado. {url}", 1)
    elif e == "dry_run_ok":
        _fechar(p, e, "ensaio_ok")
        avisar(f"Ensaio concluído (nada enviado): {nome(p)} (#{p['id']}). Respostas que eu usaria: {res.get('respostas_enviadas') or 'nenhuma'}. CV: {res.get('cv_usado', '?')}.", 1)
    elif e == "aguardando_dono":
        perg = ler(PERG / f"{p['id']}.json", None)
        if not perg:
            return _fechar_falha(p, "o robô pediu uma resposta mas não gravou a pergunta")
        t = time.time()
        p.update(estado="aguardando_dono", lembrete_em=t + 1800, expira_em=t + 86400, lembrado=False, retomar=True)
        salvar(p)
        avisar(_texto_pergunta(p, perg), 0, "pergunta", perg.get("screenshot"))
    else:
        _fechar_falha(p, f"etapa {res.get('etapa_erro') or 'desconhecida'}: {res.get('motivo') or 'sem detalhe'}", res.get("enviado"))


def _fechar_falha(p: dict, detalhe: str, enviado=None) -> None:
    _fechar(p, "falhou", "falhou:worker")
    aviso = "Pode ter sido enviada, vale conferir no portal." if enviado else "Nada foi enviado."
    avisar(f"Falhou: {nome(p)} (#{p['id']}). {detalhe}. {aviso}\nPara tentar de novo: de novo #{p['id']}", 1)


def _texto_pergunta(p: dict, perg: dict) -> str:
    cab = f"#{p['id']} {nome(p)}: {perg.get('pergunta', '')}"
    if perg.get("tipo") == "acao_fisica":
        return f"{cab}\nFaça isso no Chrome deste PC e responda: continuar #{p['id']}. Ou: pular #{p['id']}."
    if perg.get("tipo") == "escolha":
        ops = "\n".join(f"{i}) {o}" for i, o in enumerate(perg.get("opcoes") or [], 1))
        return f"{cab}\n{ops}\nResponda: #{p['id']} <número>. Ou: pular #{p['id']}."
    chave = perg.get("chave") or "resposta"
    return f"{cab}\nResponda: #{p['id']} {chave} <valor> (junte a palavra lembrar para eu guardar). Ou: pular #{p['id']}, manual #{p['id']}."


if __name__ == "__main__":
    comando = sys.argv[1] if len(sys.argv) > 1 else "tick"
    if comando == "tick":
        tick()
    elif comando == "uso":
        print(uso())
    elif comando == "fila":
        print(fila_texto())
    elif comando == "enfileirar":
        print(enfileirar([a for a in sys.argv[2:] if not a.startswith("--")], "--forcar" in sys.argv))
    elif comando == "responder":
        print(responder(" ".join(sys.argv[2:])))
    else:
        print(__doc__)
