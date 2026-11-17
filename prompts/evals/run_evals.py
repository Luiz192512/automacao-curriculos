"""Roda prompts de matching contra o eval set e compara decisões.

Uso (a partir da pasta prompts/):
    python evals/run_evals.py --prompts automacao-candidaturas-rag.v1.md automacao-candidaturas-rag.md

Provedor padrão: Gemini (plano gratuito). Requer a variável GEMINI_API_KEY.
Para usar a Anthropic: --provider anthropic (requer ANTHROPIC_API_KEY e pip install anthropic).
Para usar o Groq: --provider groq (requer GROQ_API_KEY).
"""

import argparse
import json
import os
import pathlib
import re
import sys
import time
import urllib.error
import urllib.request

HERE = pathlib.Path(__file__).resolve().parent
DEFAULT_EVAL = HERE / "automacao-candidaturas-rag.eval.json"

# Modelos padrão e pausa entre chamadas (segundos). A pausa evita o limite por minuto do plano gratuito.
PROVIDER_DEFAULTS = {
    "gemini": {"model": "gemini-3.8-flash", "delay": 12.0},
    "anthropic": {"model": "claude-haiku-5-5", "delay": 0.0},
    # Modelos de texto da conta Groq (GET /openai/v1/models, ver groq_modelos.txt): gpt-oss-120b, gpt-oss-20b, qwen3.8-27b.
    "groq": {"model": "openai/gpt-oss-120b", "delay": 10.0},
}

EM_DASH = chr(0x2014)
EN_DASH = chr(0x2013)

# Termos que elevam escopo, escala ou senioridade. Só podem aparecer na saída se estiverem no perfil
# (ou, na justificativa, na própria vaga). Cases podem liberar termos via "allow_terms".
INFLATION_TERMS = [
    "production", "produção", "producao", "expert", "expertise", "especialista",
    "senior", "sênior", "end-to-end", "end to end", "ponta a ponta", "enterprise",
    "scale", "scalable", "escala", "escalável", "ingestion", "ingestão",
    "facilidade", "extensive", "extensiva", "proven", "comprovada", "comprovado",
    "led", "liderei", "liderou",
]

# "um" e "uma" ficam de fora porque são artigos em português.
NUMBER_WORDS = {
    "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "dois": 2, "duas": 2, "três": 3, "tres": 3, "quatro": 4, "cinco": 5, "seis": 6, "sete": 7,
    "oito": 8, "nove": 9, "dez": 10,
}


# Na justificativa, estas palavras aparecem em frases sobre o que falta ("lacks PyTorch expertise"),
# então só são checadas na mensagem.
MESSAGE_ONLY_TERMS = {
    "expert", "expertise", "especialista",
    # também aparecem em frases que descrevem o requisito da vaga ("extensive backend experience") ou negam algo ("no proven deployment")
    "extensive", "extensiva", "proven", "comprovada", "comprovado",
}


def contains_term(text: str, term: str) -> bool:
    return re.search(rf"\b{re.escape(term)}\b", text, re.I) is not None


def numbers_in(text: str) -> set:
    values = {int(digits) for digits in re.findall(r"\d+", text)}
    for word, value in NUMBER_WORDS.items():
        if contains_term(text, word):
            values.add(value)
    return values


def check_grounding(case: dict, profile: str, obj: dict) -> list:
    """Devolve a lista de exageros: termos de inflação e números que o perfil não sustenta."""
    allowed = {term.lower() for term in case.get("allow_terms", [])}
    justificativa = str(obj.get("justificativa") or "")
    mensagem = str(obj.get("mensagem_candidatura") or "")
    issues = []
    for term in INFLATION_TERMS:
        if term in allowed or contains_term(profile, term):
            continue
        if (
            term not in MESSAGE_ONLY_TERMS
            and contains_term(justificativa, term)
            and not contains_term(case["posting"], term)
        ):
            issues.append(f"justificativa usa '{term}'")
        if contains_term(mensagem, term):
            issues.append(f"mensagem usa '{term}'")
    extra_numbers = numbers_in(mensagem) - numbers_in(profile)
    if extra_numbers:
        issues.append(f"mensagem tem números fora do perfil: {sorted(extra_numbers)}")
    return issues


def load_system_prompt(path: pathlib.Path) -> str:
    text = path.read_text(encoding="utf-8")
    match = re.search(r"```text\n(.*?)\n```", text, re.S)
    if not match:
        raise SystemExit(f"Nenhum bloco ```text encontrado em {path}")
    return match.group(1)


def build_user_message(profile: str, case: dict) -> str:
    return (
        f"<candidate_profile>\n{profile}\n</candidate_profile>\n"
        f"<job_posting id=\"{case['id']}\">\n{case['posting']}\n</job_posting>"
    )


def call_gemini(model: str, system: str, user: str) -> str:
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        sys.exit("GEMINI_API_KEY não está definida neste terminal.")

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    body = {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": user}]}],
        "generationConfig": {"temperature": 0, "responseMimeType": "application/json"},
    }
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", "x-goog-api-key": api_key},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=120) as response:
        data = json.loads(response.read().decode("utf-8"))

    parts = data.get("candidates", [{}])[0].get("content", {}).get("parts", [])
    return "".join(part.get("text", "") for part in parts)


def call_groq(model: str, system: str, user: str) -> str:
    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key:
        sys.exit("GROQ_API_KEY não está definida neste terminal.")

    body = {
        "model": model,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "temperature": 0,
        "response_format": {"type": "json_object"},
    }
    request = urllib.request.Request(
        "https://api.groq.com/openai/v1/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
            # O user agent padrão do urllib costuma ser bloqueado pelo proxy da API.
            "User-Agent": "run-evals/1.0",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=120) as response:
        data = json.loads(response.read().decode("utf-8"))
    return data["choices"][0]["message"].get("content") or ""


def call_anthropic(model: str, system: str, user: str) -> str:
    import anthropic  # importado aqui para o provedor Gemini não exigir o SDK

    client = anthropic.Anthropic()
    response = client.messages.create(
        model=model,
        max_tokens=1024,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    return "".join(block.text for block in response.content if block.type == "text")


QUOTA_PREFIX = "COTA_ESGOTADA"
EXIT_QUOTA = 75  # código de saída quando a cota acaba; o overnight.py usa para decidir esperar
EXIT_INTERRUPTED = 130  # Ctrl+C


def call_with_retry(provider: str, model: str, system: str, user: str) -> tuple[str, str | None]:
    """Devolve (texto, erro).

    Limite por minuto (HTTP 429): espera 60s e tenta de novo. Cota diária esgotada, ou 429 na
    terceira tentativa: devolve um erro com o prefixo COTA_ESGOTADA, que faz o run parar e salvar.
    """
    caller = {"gemini": call_gemini, "groq": call_groq, "anthropic": call_anthropic}[provider]
    for attempt in range(1, 4):
        try:
            return caller(model, system, user), None
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            if exc.code == 429:
                compact = body.lower().replace(" ", "")
                # Gemini diz "PerDay" no quotaId; o Groq diz "tokens per day (TPD)" ou "requests per day (RPD)".
                daily = "perday" in compact or "(tpd)" in compact or "(rpd)" in compact
                if daily or attempt == 3:
                    return "", f"{QUOTA_PREFIX}: {body[:600]}"
                print(f"  limite por minuto atingido, aguardando 60s (tentativa {attempt}/3)...", flush=True)
                time.sleep(60)
                continue
            if exc.code in (500, 502, 503, 504) and attempt < 3:
                print(f"  servidor instável (HTTP {exc.code}), aguardando 30s (tentativa {attempt}/3)...", flush=True)
                time.sleep(30)
                continue
            return "", f"HTTP {exc.code}: {body[:200]}"
        except Exception as exc:  # erro de rede ou do SDK, registrado sem parar o run
            return "", f"{type(exc).__name__}: {str(exc)[:200]}"
    return "", "limite de tentativas excedido"


def grade(case: dict, raw: str, error: str | None, profile: str) -> dict:
    expect = case["expect"]
    result = {
        "id": case["id"],
        "split": case["split"],
        "json_ok": False,
        "decisao_ok": False,
        "area_ok": None,
        "score_ok": None,
        "alertas_ok": None,
        "mensagem_ok": False,
        "dash_ok": (EM_DASH not in raw) and (EN_DASH not in raw),
        "grounding_ok": None,
        "grounding_issues": [],
        "error": error,
        "raw": raw,
    }
    if error:
        return result
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        return result

    result["json_ok"] = True
    decisao = obj.get("decisao")
    result["decisao_ok"] = decisao == expect["decisao"]

    if expect.get("area") is not None:
        result["area_ok"] = obj.get("area") == expect["area"]

    if expect.get("score"):
        low, high = expect["score"]
        score = obj.get("score_compatibilidade")
        # tolerância de ±3 pontos, como definido no eval set
        result["score_ok"] = isinstance(score, (int, float)) and (low - 3) <= score <= (high + 3)

    if expect.get("alertas_nonempty"):
        result["alertas_ok"] = bool(obj.get("alertas"))

    mensagem = obj.get("mensagem_candidatura")
    result["mensagem_ok"] = (decisao == "candidatar") or (mensagem is None)

    result["grounding_issues"] = check_grounding(case, profile, obj)
    result["grounding_ok"] = not result["grounding_issues"]
    return result


def pct(values: list) -> str:
    graded = [v for v in values if v is not None]
    if not graded:
        return "n/a"
    return f"{sum(1 for v in graded if v)}/{len(graded)}"


def summarize(name: str, rows: list) -> None:
    print(f"\n=== {name} ===")
    print(f"Erros de chamada:   {sum(1 for r in rows if r['error'])}")
    print(f"JSON válido:        {pct([r['json_ok'] for r in rows])}")
    print(f"decisao correta:    {pct([r['decisao_ok'] for r in rows])}  (métrica principal)")
    print(f"area correta:       {pct([r['area_ok'] for r in rows])}")
    print(f"score na faixa:     {pct([r['score_ok'] for r in rows])}")
    print(f"alertas (injeção):  {pct([r['alertas_ok'] for r in rows])}")
    print(f"mensagem coerente:  {pct([r['mensagem_ok'] for r in rows])}")
    print(f"sem travessão:      {pct([r['dash_ok'] for r in rows])}")
    print(f"sem exagero:        {pct([r['grounding_ok'] for r in rows])}")
    for row in rows:
        if row["grounding_issues"]:
            print(f"  {row['id']}: " + "; ".join(row["grounding_issues"]))
    for split in ("train", "validation", "holdout"):
        subset = [r for r in rows if r["split"] == split]
        if subset:
            print(f"  decisao {split:<10}: {pct([r['decisao_ok'] for r in subset])}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--prompts", nargs="+", required=True, help="arquivos .md com bloco ```text")
    parser.add_argument("--provider", choices=sorted(PROVIDER_DEFAULTS), default="gemini")
    parser.add_argument("--model", default=None, help="padrão depende do provedor")
    parser.add_argument("--delay", type=float, default=None, help="segundos entre chamadas")
    parser.add_argument("--eval", default=str(DEFAULT_EVAL))
    parser.add_argument("--out", default=str(HERE / "resultados.json"))
    parser.add_argument("--only", nargs="+", default=None, help="roda só os casos com estes ids")
    parser.add_argument("--resume", action="store_true", help="reaproveita casos já salvos em --out, sem erro")
    args = parser.parse_args()

    defaults = PROVIDER_DEFAULTS[args.provider]
    model = args.model or defaults["model"]
    delay = defaults["delay"] if args.delay is None else args.delay

    eval_set = json.loads(pathlib.Path(args.eval).read_text(encoding="utf-8"))
    profile = eval_set["profile"]
    cases = eval_set["cases"]
    if args.only:
        cases = [case for case in cases if case["id"] in args.only]
        if not cases:
            sys.exit(f"Nenhum caso com ids {args.only} no eval set.")

    out_path = pathlib.Path(args.out)
    all_results = {}
    if args.resume and out_path.exists():
        all_results = json.loads(out_path.read_text(encoding="utf-8"))

    def save() -> None:
        out_path.write_text(json.dumps(all_results, ensure_ascii=False, indent=2), encoding="utf-8")

    for prompt_path in args.prompts:
        path = pathlib.Path(prompt_path)
        if not path.is_absolute():
            path = (HERE.parent / path).resolve()
        system = load_system_prompt(path)

        # Com --resume, mantém só os casos que terminaram sem erro e refaz o resto.
        rows = [r for r in all_results.get(path.name, []) if not r["error"]] if args.resume else []
        done = {row["id"] for row in rows}
        all_results[path.name] = rows
        if done:
            print(f"[{path.name}] reaproveitando {len(done)} casos salvos: {sorted(done)}", flush=True)

        try:
            for case in cases:
                if case["id"] in done:
                    continue
                raw, error = call_with_retry(args.provider, model, system, build_user_message(profile, case))
                if error and error.startswith(QUOTA_PREFIX):
                    save()
                    summarize(path.name, rows)
                    print(
                        f"\nCota esgotada em {path.name} no caso {case['id']}. "
                        f"Progresso salvo em {out_path}. Retome depois com --resume (a cota diária zera à meia-noite do Pacífico).",
                        file=sys.stderr,
                    )
                    sys.exit(EXIT_QUOTA)
                rows.append(grade(case, raw, error, profile))
                save()
                status = f"erro: {error}" if error else f"decisao_ok={rows[-1]['decisao_ok']}"
                print(f"[{path.name}] {case['id']}: {status}", flush=True)
                time.sleep(delay)
        except KeyboardInterrupt:
            save()
            summarize(path.name, rows)
            print(f"\nInterrompido. Progresso salvo em {out_path}. Retome com --resume.", file=sys.stderr)
            sys.exit(EXIT_INTERRUPTED)

        summarize(path.name, rows)

    print(f"\nResultados completos em {args.out}")


if __name__ == "__main__":
    main()
