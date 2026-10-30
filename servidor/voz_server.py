#!/usr/bin/env python3
"""RLight Live — servidor do assistente de voz (Gemini Live + Home Assistant).

Serve a página (index.html), entrega a config ao navegador, executa as ferramentas
do HA (o token do HA nunca sai daqui) e faz a ponte do terminal ESP32-S3
(dispositivo.py). Sem dependências: só a stdlib.

Configuração: config.env (copie de config.example.env) ou variáveis de ambiente.
Personalidade e contexto da casa: prompt.md (copie de prompt.example.md).

Uso:  python3 voz_server.py   →  abrir http://localhost:8765 no Chrome
O terminal físico conecta em ws://<servidor>:8765/dispositivo?token=…
"""
import json
import os
import queue
import re
import subprocess
import threading
import time
import unicodedata
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import dispositivo

HERE = Path(__file__).resolve().parent


def _carregar_env(arquivo):
    """KEY=valor por linha; variáveis de ambiente já definidas têm prioridade."""
    if arquivo.exists():
        for linha in arquivo.read_text().splitlines():
            k, sep, v = linha.strip().partition("=")
            if sep and not k.startswith("#"):
                os.environ.setdefault(k.strip(), v.strip().strip('"'))


_carregar_env(HERE / "config.env")

HA_URL = os.environ.get("HA_URL", "http://homeassistant.local:8123").rstrip("/")
HA_TOKEN = os.environ["HA_TOKEN"]
GEMINI_KEY = os.environ["GEMINI_API_KEY"]
USUARIO = os.environ.get("RLIGHT_USUARIO", "você")

MODEL = os.environ.get("RLIGHT_MODELO", "models/gemini-3.8-live")
VOICE = os.environ.get("RLIGHT_VOZ", "Leda")
PORT = int(os.environ.get("RLIGHT_PORT", 8765))
# 127.0.0.1 só para a página; 0.0.0.0 para o terminal S3 alcançar. Mesmo assim, de fora
# do localhost só o /dispositivo responde (com token): /config tem a chave do Gemini.
BIND = os.environ.get("RLIGHT_BIND", "127.0.0.1")
DISP_TOKEN = os.environ.get("RLIGHT_TERMINAL_TOKEN") or None
# firmware do terminal para o OTA (firmware/ota.sh coloca o .bin aqui)
FIRMWARE = Path(os.environ.get("RLIGHT_FIRMWARE", "/var/lib/rlight-live/rlight_terminal.bin"))

# Domínios que o assistente pode acionar. Fora disso, só leitura.
ACTION_DOMAINS = {
    "switch", "light", "climate", "cover", "scene", "script", "fan",
    "input_boolean", "button", "vacuum", "media_player", "automation",
    "input_number", "select", "number",
}
# Ruído que não ajuda numa conversa.
HIDDEN_DOMAINS = {"update", "image", "event", "tts", "stt", "conversation", "zone"}


def ha(method, path, body=None):
    req = urllib.request.Request(
        HA_URL + path,
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"Bearer {HA_TOKEN}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        raw = r.read().decode()
    return json.loads(raw) if raw and raw[0] in "[{" else raw


def norm(s):
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()
    return s.lower()


_areas = {"t": 0, "map": {}}


def area_map():
    """entity_id -> área (cache de 10 min, o template é caro)."""
    if time.time() - _areas["t"] > 600:
        tpl = ("{% for s in states %}{% set a = area_name(s.entity_id) %}"
               "{% if a %}{{ s.entity_id }}={{ a }}\n{% endif %}{% endfor %}")
        out = ha("POST", "/api/template", {"template": tpl})
        _areas["map"] = dict(l.split("=", 1) for l in out.splitlines() if "=" in l)
        _areas["t"] = time.time()
    return _areas["map"]


def compact(s, areas):
    a = s["attributes"]
    d = {"id": s["entity_id"], "nome": a.get("friendly_name"), "estado": s["state"]}
    if a.get("unit_of_measurement"):
        d["unidade"] = a["unit_of_measurement"]
    if s["entity_id"] in areas:
        d["area"] = areas[s["entity_id"]]
    return d


def _local(iso):
    """ISO UTC do HA → 'dd/mm HH:MM' no fuso local do servidor (defina TZ no serviço)."""
    if not iso:
        return None
    from datetime import datetime
    return datetime.fromisoformat(iso).astimezone().strftime("%d/%m %H:%M")


def detail(s, areas):
    d = compact(s, areas)
    keep = ("current_temperature", "temperature", "hvac_modes", "fan_mode",
            "brightness", "current_position", "options", "min", "max", "battery_level")
    d.update({k: s["attributes"][k] for k in keep if k in s["attributes"]})
    d["mudou_em"] = _local(s["last_changed"])
    return d


# ---------- ferramentas ----------

def buscar_entidades(consulta, dominio=None, limite=20):
    areas = area_map()
    termos = norm(consulta).split()
    # muitas luzes são relés (switch.luz_*), não light.*; e "light" também pega LEDs de
    # botão que não iluminam nada
    doms = ({dominio, "switch"} if dominio == "light" else {dominio}) if dominio else None
    hits = []
    for s in ha("GET", "/api/states"):
        dom = s["entity_id"].split(".")[0]
        if dom in HIDDEN_DOMAINS or (doms and dom not in doms):
            continue
        txt = norm(" ".join([s["entity_id"], s["attributes"].get("friendly_name", ""),
                             areas.get(s["entity_id"], "")]))
        score = sum(t in txt for t in termos)
        if score and dominio == "light":
            if dom == "switch" and "luz" not in txt:
                continue
            score += 0.5 * ("luz" in txt) - 0.5 * any(w in txt for w in ("botao", "backlight"))
        if score:
            hits.append((score, s))
    hits.sort(key=lambda h: -h[0])
    return {"resultados": [compact(s, areas) for _, s in hits[:limite]],
            "total": len(hits)}


def ler_estado(entity_ids):
    areas = area_map()
    out = []
    for eid in entity_ids:
        try:
            out.append(detail(ha("GET", f"/api/states/{eid}"), areas))
        except Exception as e:
            out.append({"id": eid, "erro": str(e)})
    return {"estados": out}


def acionar(servico, entity_id, dados=None):
    dom, _, svc = servico.partition(".")
    if dom not in ACTION_DOMAINS:
        return {"erro": f"domínio '{dom}' não é permitido para ação"}
    body = dict(dados or {}, entity_id=entity_id)
    ids = [entity_id] if isinstance(entity_id, str) else entity_id
    antes = [ha("GET", f"/api/states/{e}").get("last_updated") for e in ids]
    ha("POST", f"/api/services/{dom}/{svc}", body)
    # relê até o HA registrar a mudança: Zigbee leva ~0,5 s, mas os ares da LG (nuvem) e os
    # IR confirmam em alguns segundos; reler cedo demais fazia ela dizer "não ligou"
    fim = time.time() + 6
    while True:
        time.sleep(0.5)
        agora = [ha("GET", f"/api/states/{e}").get("last_updated") for e in ids]
        if all(x != y for x, y in zip(agora, antes)) or time.time() > fim:
            break
    r = {"ok": True, "depois": ler_estado(ids)}
    if any(x == y for x, y in zip(agora, antes)):
        r["obs"] = ("comando enviado, mas o HA não registrou mudança em 6 s: ou já estava assim, ou o "
                    "aparelho ainda vai confirmar (nuvem/IR); não diga que falhou")
    return r


def historico(entity_id, horas=24):
    start = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time() - horas * 3600))
    data = ha("GET", f"/api/history/period/{start}Z?filter_entity_id={entity_id}"
                     "&minimal_response&no_attributes")
    pts = data[0] if data else []
    if len(pts) > 40:  # amostra pra não estourar contexto
        pts = pts[:: len(pts) // 40 + 1] + [pts[-1]]
    return {"pontos": [{"t": _local(p.get("last_changed")), "v": p["state"]} for p in pts]}


MEMORIA = HERE / "memoria.json"


def _memorias():
    return json.loads(MEMORIA.read_text()) if MEMORIA.exists() else []


def lembrar(fato):
    mem = _memorias()
    mem.append({"fato": fato.strip(), "em": time.strftime("%Y-%m-%d")})
    MEMORIA.write_text(json.dumps(mem, ensure_ascii=False, indent=1))
    return {"ok": True, "total": len(mem)}


def esquecer(trecho):
    mem = _memorias()
    fica = [m for m in mem if norm(trecho) not in norm(m["fato"])]
    MEMORIA.write_text(json.dumps(fica, ensure_ascii=False, indent=1))
    return {"removidas": len(mem) - len(fica)}


TOOLS = {"buscar_entidades": buscar_entidades, "ler_estado": ler_estado,
         "acionar": acionar, "historico": historico, "lembrar": lembrar, "esquecer": esquecer}

DECLS = [
    {"name": "buscar_entidades",
     "description": "Procura dispositivos/sensores da casa por palavras (nome, cômodo ou id). "
                    "Use antes de agir quando não souber o entity_id. Retorna id, nome, estado e área.",
     "parameters": {"type": "OBJECT", "properties": {
         "consulta": {"type": "STRING", "description": "palavras-chave, ex: 'bomba', 'ar suite', 'temperatura piscina'"},
         "dominio": {"type": "STRING", "description": "opcional: switch, light, climate, sensor, binary_sensor, scene..."}},
         "required": ["consulta"]}},
    {"name": "ler_estado",
     "description": "Lê o estado atual detalhado de uma ou mais entidades.",
     "parameters": {"type": "OBJECT", "properties": {
         "entity_ids": {"type": "ARRAY", "items": {"type": "STRING"}}},
         "required": ["entity_ids"]}},
    {"name": "acionar",
     "description": "Executa um serviço do Home Assistant, ex: switch.turn_on, light.turn_off, "
                    "climate.set_temperature (dados: {temperature: 23}), climate.set_hvac_mode "
                    "(dados: {hvac_mode: 'cool'}), scene.turn_on. Retorna o estado depois.",
     "parameters": {"type": "OBJECT", "properties": {
         "servico": {"type": "STRING", "description": "dominio.servico"},
         "entity_id": {"type": "STRING"},
         "dados": {"type": "OBJECT", "description": "parâmetros extras do serviço (opcional)"}},
         "required": ["servico", "entity_id"]}},
    {"name": "historico",
     "description": "Histórico de uma entidade nas últimas N horas (amostrado).",
     "parameters": {"type": "OBJECT", "properties": {
         "entity_id": {"type": "STRING"},
         "horas": {"type": "NUMBER"}},
         "required": ["entity_id"]}},
    {"name": "lembrar",
     "description": "Guarda um fato duradouro sobre o morador, a família ou a casa para lembrar "
                    "em conversas futuras (preferências, apelidos, rotinas). Use quando pedirem "
                    "para lembrar ou quando surgir algo claramente útil depois.",
     "parameters": {"type": "OBJECT", "properties": {
         "fato": {"type": "STRING", "description": "frase curta e autoexplicativa"}},
         "required": ["fato"]}},
    {"name": "pedir_ao_agente",
     "description": "Delega ao Claude (agente em segundo plano, o Claude Code) tudo "
                    "que as outras ferramentas não resolvem: verificar/investigar algo, por que algo "
                    "caiu ou parou, diagnósticos, histórico da casa. "
                    "Use também sempre que pedirem 'verifica', 'investiga' ou 'pergunta pro Claude'. "
                    "Leva de 1 a alguns minutos. Retorna na hora só a confirmação: "
                    "diga algo curto como 'vou ver isso' e siga a conversa; o resultado chega "
                    "sozinho depois como uma mensagem [Resultado da investigação…].",
     "parameters": {"type": "OBJECT", "properties": {
         "tarefa": {"type": "STRING", "description": "o pedido completo, com o contexto que o morador deu"}},
         "required": ["tarefa"]}},
    {"name": "esquecer",
     "description": "Apaga memórias que contenham o trecho informado.",
     "parameters": {"type": "OBJECT", "properties": {
         "trecho": {"type": "STRING"}},
         "required": ["trecho"]}},
]

PROMPT = HERE / "prompt.md" if (HERE / "prompt.md").exists() else HERE / "prompt.example.md"



def config():
    areas = sorted(set(area_map().values()))
    mem = "\n".join(f"- {m['fato']}" for m in _memorias()) or "- (nada ainda)"
    system = (PROMPT.read_text().replace("{usuario}", USUARIO).replace("{areas}", ", ".join(areas))
              .replace("{agora}", time.strftime("%A %d/%m/%Y %H:%M"))
              .replace("{memorias}", mem))
    tools = DECLS if AGENTE else [d for d in DECLS if d["name"] != "pedir_ao_agente"]
    return {"key": GEMINI_KEY, "model": MODEL, "voice": VOICE, "system": system, "tools": tools,
            "usuario": USUARIO}


def executar(sid, name, args):
    t0 = time.time()
    try:
        res = TOOLS[name](**args)
    except Exception as e:
        res = {"erro": f"{type(e).__name__}: {e}"}
    registrar(sid, "ferramenta", nome=name, args=args, resultado=res,
              ms=int((time.time() - t0) * 1000))
    return res


class H(BaseHTTPRequestHandler):
    def log_message(self, fmt, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _sse(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        q = queue.Queue()
        with _lock:
            _clientes.append(q)
            # resultado que ficou sem dono (página recarregada no meio)
            for j in _jobs.values():
                if j["estado"] == "pronto":
                    q.put(evento_resultado(j))
        try:
            while True:
                try:
                    msg = q.get(timeout=15)
                    self.wfile.write(f"data: {json.dumps(msg, ensure_ascii=False)}\n\n".encode())
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            with _lock:
                _clientes.remove(q)

    def _de_fora(self):
        if self.client_address[0] in ("127.0.0.1", "::1"):
            return False
        self._send(403, {"erro": "só localhost"})
        return True

    def do_GET(self):
        if self.path.startswith("/dispositivo"):
            tok = parse_qs(urlsplit(self.path).query).get("token", [""])[0]
            if not DISP_TOKEN or tok != DISP_TOKEN or self.headers.get("Upgrade", "").lower() != "websocket":
                return self._send(403, {"erro": "token do terminal inválido"})
            return dispositivo.atender(self)
        if self.path.startswith("/firmware.bin"):        # OTA do terminal
            tok = parse_qs(urlsplit(self.path).query).get("token", [""])[0]
            if not DISP_TOKEN or tok != DISP_TOKEN:
                return self._send(403, {"erro": "token do terminal inválido"})
            if not FIRMWARE.exists():
                return self._send(404, {"erro": f"sem {FIRMWARE}"})
            return self._send(200, FIRMWARE.read_bytes(), "application/octet-stream")
        if self._de_fora():
            return
        if self.path in ("/", "/index.html"):
            self._send(200, (HERE / "index.html").read_bytes(), "text/html; charset=utf-8")
        elif self.path == "/eventos":
            return self._sse()
        elif self.path == "/config":
            self._send(200, config())
        else:
            self._send(404, {"erro": "não encontrado"})

    def do_POST(self):
        if self._de_fora():
            return
        req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        sid = req.get("sid") or "sem-sessao"
        if self.path == "/log":
            registrar(sid, req.get("tipo", "?"), texto=req.get("texto", ""))
            return self._send(200, {"ok": True})
        if self.path == "/wake":
            pasta = HERE / "wake_log"
            pasta.mkdir(exist_ok=True)
            ev = {"t": time.strftime("%H:%M:%S"), "ativou": bool(req.get("ativou")), "alts": req.get("alts", [])}
            with open(pasta / f"{time.strftime('%Y-%m-%d')}.jsonl", "a") as f:
                f.write(json.dumps(ev, ensure_ascii=False) + "\n")
            print(f"{ev['t']} {'👂 ATIVOU' if ev['ativou'] else '👂 ouviu':>10}  {' | '.join(ev['alts'])}", flush=True)
            return self._send(200, {"ok": True})
        if self.path == "/ota":
            return self._send(200, {"avisados": dispositivo.ota(req.get("terminal"))})
        if self.path == "/carga":
            return self._send(200, {"avisados": dispositivo.carga(req.get("forcar", -1), req.get("terminal"))})
        if self.path == "/agente":
            return self._send(200, agente_pedir(sid, req.get("tarefa", "")))
        if self.path == "/agente/ack":
            return self._send(200, agente_ack(req.get("job"), req.get("via"), req.get("ok", True)))
        if self.path != "/tool":
            return self._send(404, {"erro": "não encontrado"})
        self._send(200, executar(sid, req.get("name"), req.get("args") or {}))


# ---------- histórico de conversas ----------
# conversas/AAAA-MM-DD/<sessão>.jsonl — um evento por linha. Navegar com conversas.py.
CONVERSAS = HERE / "conversas"


def registrar(sid, tipo, **campos):
    ev = {"t": time.strftime("%Y-%m-%dT%H:%M:%S"), "tipo": tipo, **campos}
    if "resultado" in ev and len(json.dumps(ev["resultado"], ensure_ascii=False)) > 4000:
        ev["resultado"] = json.dumps(ev["resultado"], ensure_ascii=False)[:4000] + "…"
    sid = re.sub(r"[^\w-]", "", sid)
    pasta = CONVERSAS / sid[:10] if re.match(r"\d{4}-\d{2}-\d{2}", sid) else CONVERSAS / "outros"
    pasta.mkdir(parents=True, exist_ok=True)
    with open(pasta / f"{sid}.jsonl", "a") as f:
        f.write(json.dumps(ev, ensure_ascii=False) + "\n")
    if tipo == "agente":
        resumo = f"[{campos.get('fase')} {campos.get('job')}] {campos.get('texto', '')}"
    else:
        resumo = campos.get("texto") or f"{campos.get('nome')}({json.dumps(campos.get('args'), ensure_ascii=False)}) " \
            f"→ {json.dumps(campos.get('resultado'), ensure_ascii=False)[:300]}  [{campos.get('ms')} ms]"
    print(f"{ev['t'][11:]} {tipo:>10}  {resumo}", flush=True)


# ---------- agente em segundo plano ("pensador lento") ----------
# O Gemini Live é o falador rápido; investigações demoradas vão para um Claude Code
# headless SOMENTE LEITURA. O resultado volta pela página (SSE /eventos): na sessão
# aberta, numa sessão curta só pra falar, ou — sem página/de madrugada — na central
# de notificações do HA.
REPO = HERE.parent
AGENTE = os.environ.get("RLIGHT_AGENTE", "0") == "1"   # precisa do Claude Code instalado
AGENTE_TIMEOUT = 600          # s
ACK_TIMEOUT = 30              # s sem a página confirmar a entrega → notificação no HA
AGENTE_TETO_USD = 0.50        # teto por investigação (--max-budget-usd, custo equivalente)
_jobs, _clientes, _lock = {}, [], threading.Lock()
_fila = queue.Queue()

AGENTE_PROMPT = """Você é o agente em segundo plano do RLight Live (assistente de voz da casa
de {usuario}). Pedido por voz: "{tarefa}"
Agora são {agora} (horário local do servidor). Os horários que a API do HA devolve
(last_changed, history, logbook) estão em UTC: converta para o horário local antes de falar.

Use o helper SOMENTE LEITURA do Home Assistant (sem argumentos mostra o uso):
  python3 servidor/agente_ler.py
(buscar, estado, historico, logbook, template, ha GET). Seja econômico: poucos comandos,
pare assim que tiver a resposta. Não altere nada; mudança vira proposta.

Responda APENAS com JSON, sem markdown:
{{"fala": "até 3 frases curtas (~50 palavras) em português do Brasil, para voz: o que
achou e o que fazer. Sem ids, sem markdown, números arredondados.",
 "proposta": null ou {{"descricao": "o que mudar e onde",
   "acao_ha": null ou {{"servico": "dominio.servico", "entity_id": "...", "dados": {{}}}}}}}}}}
acao_ha só se a correção for uma única chamada de serviço do HA."""


def agente_pedir(sid, tarefa, terminal=None):
    jid = time.strftime("%H%M%S") + "-" + os.urandom(2).hex()
    job = {"id": jid, "sid": sid, "tarefa": tarefa.strip(), "terminal": terminal,
           "estado": "na_fila", "pedido_em": time.time()}
    with _lock:
        _jobs[jid] = job
    registrar(sid, "agente", fase="pedido", job=jid, texto=tarefa)
    _fila.put(jid)
    return {"job": jid, "status": "investigando em segundo plano"}


def _rodar_claude(tarefa):
    env = {k: v for k, v in os.environ.items()
           if not (k.startswith("CLAUDE") or k in ("AI_AGENT", "BAGGAGE"))}
    if os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):   # `claude setup-token` (não expira como o login)
        env["CLAUDE_CODE_OAUTH_TOKEN"] = os.environ["CLAUDE_CODE_OAUTH_TOKEN"]
    if os.environ.get("RLIGHT_AGENTE_FAKE"):   # teste da entrega sem gastar o agente
        time.sleep(float(os.environ["RLIGHT_AGENTE_FAKE"]))
        return {"fala": f"Teste do agente: recebi o pedido '{tarefa}'. Tudo certo com a entrega.",
                "proposta": None, "custo_usd": 0}
    # custo: sonnet + esforço baixo + sem MCPs (Notion/Gmail… inflavam o contexto) + teto
    # de gasto. "a fundo"/"com calma" no pedido libera o opus.
    profundo = re.search(r"a fundo|com calma|detalhad|profund", norm(tarefa))
    modelo = "opus" if profundo else os.environ.get("RLIGHT_AGENTE_MODELO", "sonnet")
    cmd = ["claude", "-p", "--output-format", "json", "--no-session-persistence",
           "--model", modelo, "--effort", "medium" if profundo else "low",
           "--strict-mcp-config", "--max-budget-usd", "1.00" if profundo else str(AGENTE_TETO_USD),
           "--permission-mode", "default",
           "--allowedTools", "Read", "Grep", "Glob",
           "Bash(python3 servidor/agente_ler.py:*)",
           "--disallowedTools", "Edit", "Write", "NotebookEdit", "WebFetch", "WebSearch"]
    p = subprocess.run(cmd, input=AGENTE_PROMPT.format(tarefa=tarefa, usuario=USUARIO, agora=time.strftime("%d/%m %H:%M")), cwd=REPO, env=env,
                       capture_output=True, text=True, timeout=AGENTE_TIMEOUT)
    meta = json.loads(p.stdout or "{}")
    if meta.get("subtype") == "error_max_budget_usd":
        return {"fala": "Parei a investigação porque ia ficar cara demais. Se quiser, me peça "
                        "de novo dizendo 'investiga a fundo'.", "proposta": None,
                "custo_usd": meta.get("total_cost_usd"), "modelo": modelo}
    if p.returncode or meta.get("is_error"):
        raise RuntimeError((meta.get("result") or p.stderr or p.stdout)[:500])
    txt = meta.get("result", "")
    m = re.search(r"\{.*\}", txt, re.S)
    try:
        res = json.loads(m.group(0)) if m else {}
    except json.JSONDecodeError:
        res = {}
    if not res.get("fala"):
        res = {"fala": txt.strip()[:800], "proposta": None}
    res["custo_usd"] = meta.get("total_cost_usd")
    res["modelo"] = modelo
    return res


def evento_resultado(j):
    return {"tipo": "agente_resultado", "job": j["id"], "sid": j["sid"],
            "tarefa": j["tarefa"], "fala": j["fala"], "proposta": j.get("proposta")}


def _worker():
    while True:
        jid = _fila.get()
        j = _jobs[jid]
        j["estado"] = "rodando"
        registrar(j["sid"], "agente", fase="inicio", job=jid, texto=j["tarefa"])
        t0 = time.time()
        try:
            r = _rodar_claude(j["tarefa"])
            j.update(fala=r["fala"], proposta=r.get("proposta"))
            registrar(j["sid"], "agente", fase="resultado", job=jid, texto=r["fala"],
                      proposta=r.get("proposta"), custo_usd=r.get("custo_usd"), modelo=r.get("modelo"),
                      ms=int((time.time() - t0) * 1000))
        except Exception as e:
            j.update(fala=f"Não consegui terminar a investigação sobre isso: {e}"[:400], proposta=None)
            registrar(j["sid"], "agente", fase="erro", job=jid, texto=str(e)[:500],
                      ms=int((time.time() - t0) * 1000))
        with _lock:
            j["estado"] = "pronto"
        # pedido feito no terminal S3 → volta pelo terminal; senão (ou se ele estiver
        # desligado) segue pela página, como antes
        if not (j.get("terminal") and dispositivo.entregar(j)):
            with _lock:
                for q in _clientes:
                    q.put(evento_resultado(j))
        threading.Timer(ACK_TIMEOUT, _sem_ack, args=(jid,)).start()


def agente_ack(jid, via, ok=True):
    j = _jobs.get(jid)
    if not j or j["estado"] != "pronto":
        return {"ok": False}
    if not ok:                       # página recusou (madrugada, áudio bloqueado…)
        return _notificar_ha(j, via)
    j["estado"] = "entregue"
    registrar(j["sid"], "agente", fase="entregue", job=jid, texto=via)
    return {"ok": True}


def _sem_ack(jid):
    j = _jobs.get(jid)
    if j and j["estado"] == "pronto":
        _notificar_ha(j, "sem página aberta")


def _notificar_ha(j, motivo):
    with _lock:
        if j["estado"] != "pronto":
            return {"ok": True}
        j["estado"] = "entregue"
    msg = j["fala"]
    if j.get("proposta"):
        msg += "\n\n**Proposta:** " + j["proposta"].get("descricao", "")
    try:   # central de notificações do HA (nunca push no celular)
        ha("POST", "/api/services/persistent_notification/create",
           {"title": f"RLight investigou: {j['tarefa'][:60]}", "message": msg,
            "notification_id": f"rlight_agente_{j['id']}"})
        registrar(j["sid"], "agente", fase="entregue", job=j["id"], texto=f"notificação HA ({motivo})")
    except Exception as e:
        registrar(j["sid"], "agente", fase="erro", job=j["id"], texto=f"falha ao notificar HA: {e}")
    return {"ok": True}


threading.Thread(target=_worker, daemon=True).start()
dispositivo.configurar(config=config, executar=executar, registrar=registrar, ha=ha,
                       agente_pedir=agente_pedir, agente_ack=agente_ack)


if __name__ == "__main__":
    print(f"RLight Live em http://localhost:{PORT}  (modelo {MODEL}, voz {VOICE}, agente "
          f"{'ligado' if AGENTE else 'desligado'}; "
          f"terminal: ws://{BIND}:{PORT}/dispositivo{'' if DISP_TOKEN else ' — SEM TOKEN, desligado'})", flush=True)
    ThreadingHTTPServer.daemon_threads = True
    ThreadingHTTPServer((BIND, PORT), H).serve_forever()
