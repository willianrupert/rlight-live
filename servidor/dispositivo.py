"""RLight Live — ponte entre o terminal físico (ESP32-S3) e o Gemini Live.

O S3 mantém um WebSocket fixo em /dispositivo?token=… e o servidor faz o resto do que
a página faz no Chrome: abre a sessão no Gemini (a chave nunca vai pro firmware),
executa as ferramentas, grava o histórico em conversas/, encerra por inatividade,
retoma a sessão se a conexão cair e entrega o resultado do agente em segundo plano.

Protocolo (texto = JSON, binário = PCM16 mono 16 kHz little-endian):
  S3 → servidor   {"tipo":"ola","id":"sala","fw":"…"}            ao conectar
                  {"tipo":"inicio","origem":"palavra","palavra":"wn9_alexa"}
                  {"tipo":"vad","fala":true|false}               VAD do AFE
                  {"tipo":"fim","motivo":"…"}                    encerrou no S3
                  {"tipo":"bateria","v":3.95,"ma":184,"pct":78}  INA226, a cada 10 s
                  binário: saída do AFE (já sem eco), só durante a sessão
  servidor → S3   {"tipo":"sessao","origem":"agente"}            abre sessão sem palavra
                  {"tipo":"pronto"}                              Gemini conectado
                  {"tipo":"interrompido"}                        descartar áudio na fila
                  {"tipo":"fim","motivo":"…"}                    voltar a esperar a palavra
                  {"tipo":"texto","quem":"v"|"r","t":"…"}        transcrição p/ a OLED (ASCII)
                  {"tipo":"config","portao":-22}                 ao conectar (ver PORTAO_DBFS)
                  binário: voz do RLight, reamostrada de 24 para 16 kHz (a mesma
                  taxa do mic, para o AEC do S3 usar como referência sem conversão)
"""
import base64
import json
import operator
import os
import threading
import time
import math
import unicodedata
from array import array

import ws_mini

# Portão de eco do terminal: enquanto ele fala, só sobe o mic se o pós-AEC passar disso
# (dBFS); -100 desliga e quem separa eco de voz é o VAD do Gemini, com início de fala menos
# sensível (VAD_INICIO). Firmware antigo ignora e usa o RLIGHT_BARGEIN_DBFS do Kconfig.
PORTAO_DBFS = int(os.environ.get("RLIGHT_PORTAO_DBFS", "-100"))
VAD_INICIO = os.environ.get("RLIGHT_VAD_INICIO", "START_SENSITIVITY_LOW")   # "" = padrão do Gemini

GEMINI_URL = ("wss://generativelanguage.googleapis.com/ws/google.ai.generativelanguage."
              "v1beta.GenerativeService.BidiGenerateContent?key=")
IDLE_S = 12              # igual ao IDLE_MS da página
MAX_RECONEXOES = 5       # igual ao MAX_RECONNECTS da página
BUFFER_MAX = 15 * 32000  # bytes de mic guardados enquanto o Gemini conecta (15 s)
CHUNK_SAIDA = 3200       # bytes por frame para o S3 (100 ms a 16 kHz)
BATERIA_HA_S = 30        # publica a bateria no HA no máximo a cada 30 s
BATERIA_AVISO = 20       # % → aviso na central do HA (uma vez por descarga; rearma acima de 30 %)
EFICIENCIA_BOOST = 0.85  # HW-465A: bateria 3,7 V → 5 V

ctx = None               # preenchido por configurar() (funções do voz_server.py)
_terminais, _lock = {}, threading.Lock()


def configurar(**kw):
    global ctx

    class C:
        pass
    ctx = C()
    ctx.__dict__.update(kw)
    threading.Thread(target=_vigia_inatividade, daemon=True).start()


def ascii(t):
    """A fonte da OLED só tem ASCII: 'Está ligado' → 'Esta ligado'."""
    return unicodedata.normalize("NFKD", t).encode("ascii", "ignore").decode()


def publicar_bateria(term, ev):
    """Sensores do terminal no HA (POST /api/states). Somem num restart do HA até a
    próxima leitura (≤ 30 s), o que basta para a automação dos 80 %."""
    v, ma, pct = float(ev.get("v", 0)), float(ev.get("ma", 0)), int(ev.get("pct", -1))
    w = 5.0 * ma / 1000
    nome = term.id
    disp = {"terminal": nome}
    sensores = {
        f"sensor.luma_{nome}_bateria": (pct, {"unit_of_measurement": "%", "device_class": "battery",
                                              "state_class": "measurement", "tensao": round(v, 3),
                                              "friendly_name": f"Luma {nome} bateria"}),
        f"sensor.luma_{nome}_bateria_tensao": (round(v, 3), {"unit_of_measurement": "V", "device_class": "voltage",
                                                            "state_class": "measurement",
                                                            "friendly_name": f"Luma {nome} bateria tensão"}),
        f"sensor.luma_{nome}_consumo": (round(w, 2), {"unit_of_measurement": "W", "device_class": "power",
                                                      "state_class": "measurement", "corrente_5v_ma": round(ma),
                                                      "friendly_name": f"Luma {nome} consumo"}),
        f"sensor.luma_{nome}_corrente_bateria": (
            round(w / max(v, 3.0) / EFICIENCIA_BOOST * 1000) if v else 0,
            {"unit_of_measurement": "mA", "device_class": "current", "state_class": "measurement",
             "estimada": "5 V × corrente da saída ÷ tensão da bateria ÷ 0,85 (só descarga)",
             "friendly_name": f"Luma {nome} corrente da bateria"}),
    }
    if 0 <= pct <= BATERIA_AVISO and not term.avisou_bateria:
        term.avisou_bateria = True
        try:   # central de notificações do HA, nunca push no celular
            ctx.ha("POST", "/api/services/persistent_notification/create",
                   {"title": f"Luma ({nome}): bateria em {pct} %",
                    "message": f"Bateria em {v:.2f} V. Carregue o terminal; abaixo de ~7 % ele dorme "
                               "sozinho para proteger as células.",
                    "notification_id": f"luma_{nome}_bateria"})
        except Exception:
            term.avisou_bateria = False
    elif pct > BATERIA_AVISO + 10:
        term.avisou_bateria = False
    for eid, (estado, attrs) in sensores.items():
        try:
            ctx.ha("POST", f"/api/states/{eid}", {"state": estado, "attributes": attrs | disp})
        except Exception as e:
            print(f"{time.strftime('%H:%M:%S')}   terminal  ⚠️ bateria no HA: {e}", flush=True)
            return


def novo_sid():
    """Mesmo formato da página: 2026-10-29T14-03-12-ab12 → conversas/2026-10-29/."""
    return time.strftime("%Y-%m-%dT%H-%M-%S") + "-" + os.urandom(2).hex()


def madrugada():
    h = time.localtime().tm_hour
    return h >= 23 or h < 7


def texto_anuncio(j, usuario):
    """Mesmo texto do textoAnuncio() da página."""
    t = f'[Resultado da investigação em segundo plano que {usuario} pediu: "{j["tarefa"]}"]\n{j["fala"]}'
    if j.get("proposta"):
        t += "\nProposta: " + json.dumps(j["proposta"], ensure_ascii=False)
    return (t + f'\nConte isso a {usuario} agora, de forma natural, começando por "{usuario}, sobre…".'
            + (" Depois pergunte se ele quer seguir a proposta." if j.get("proposta") else ""))


class Reamostrador:
    """24 kHz → 16 kHz (2/3): sobe ×2, passa-baixa FIR em 7,5 kHz, desce ÷3 (polifásico)."""

    def __init__(self, taps=24):
        fs, fc, m = 48000, 7500, (taps - 1) / 2
        h = []
        for k in range(taps):
            t = 2 * math.pi * fc / fs * (k - m)
            sinc = math.sin(t) / t if t else 1.0
            h.append(sinc * (0.54 - 0.46 * math.cos(2 * math.pi * k / (taps - 1))))
        s = sum(h)
        h = [2 * v / s for v in h]            # ×2 compensa os zeros inseridos
        self.fases = (h[0::2][::-1], h[1::2][::-1])
        self.L = len(self.fases[0])
        self.reset()

    def reset(self):
        self.hist, self.ini, self.n = [0] * self.L, -self.L, 0

    def processar(self, pcm):
        novo = array("h")
        novo.frombytes(pcm[: len(pcm) & ~1])
        x = self.hist + novo.tolist()
        fim = self.ini + len(x) - 1
        out, n, L = array("h"), self.n, self.L
        while (i0 := (3 * n) >> 1) <= fim:
            j = i0 - self.ini
            v = int(sum(map(operator.mul, self.fases[n & 1], x[j - L + 1: j + 1])))
            out.append(32767 if v > 32767 else -32768 if v < -32768 else v)
            n += 1
        self.n, self.hist, self.ini = n, x[-L:], fim - L + 1
        return out.tobytes()


class Sessao:
    """Uma conversa no Gemini Live, do lado do servidor (o que a página faz no Chrome)."""

    def __init__(self, term, origem, palavra="", anunciar=None):
        self.term, self.origem, self.anunciar = term, origem, anunciar
        self.sid = novo_sid()
        self.ws, self.ready, self.handle, self.reconexoes = None, False, None, 0
        self.buffer, self.enc, self.sobra = bytearray(), False, b""
        self.ultimo = self.toca_ate = time.time()
        self.falando, self.cur_user, self.cur_asst = False, "", ""
        self.ream = Reamostrador()
        self.cfg = ctx.config()     # relê memórias e horário a cada sessão
        # o prompt.md fala da página; no terminal ele é chamado pela palavra do firmware
        self.cfg["system"] += ('\n\nAgora você está falando pelo terminal de voz da casa (alto-falante). '
                               'Lá a palavra de ativação do aparelho é "Alexa": é só o gatilho, você '
                               'continua sendo o mesmo assistente (não a Alexa da Amazon); o começo do '
                               'áudio pode conter ela, não comente isso.')
        ctx.registrar(self.sid, "inicio", texto=f"origem={origem} voz={self.cfg['voice']} "
                      f"terminal={term.id}" + (f' ouvido="{palavra}"' if palavra else ""))
        threading.Thread(target=self.conectar, daemon=True).start()

    # ---------- Gemini ----------
    def conectar(self):
        self.ready = False
        try:
            ws = ws_mini.conectar(GEMINI_URL + self.cfg["key"])
        except Exception as e:
            return self._caiu(None, f"falha ao conectar no Gemini: {e}")
        if self.enc:
            return ws.fechar()
        self.ws = ws
        cfg = self.cfg
        ws.enviar(json.dumps({"setup": {
            "model": cfg["model"],
            "generationConfig": {"responseModalities": ["AUDIO"], "speechConfig": {
                "voiceConfig": {"prebuiltVoiceConfig": {"voiceName": cfg["voice"]}}}},
            "systemInstruction": {"parts": [{"text": cfg["system"]}]},
            "tools": [{"functionDeclarations": cfg["tools"]}],
            "inputAudioTranscription": {}, "outputAudioTranscription": {},
            "sessionResumption": {"handle": self.handle} if self.handle else {},
            "contextWindowCompression": {"slidingWindow": {}},
            **({"realtimeInputConfig": {"automaticActivityDetection": {
                "startOfSpeechSensitivity": VAD_INICIO}}} if VAD_INICIO else {}),
        }}))
        try:
            while True:
                self.tratar(json.loads(ws.receber()))
        except ws_mini.Fechado as e:
            self._caiu(ws, str(e))
        except Exception as e:
            self._caiu(ws, f"{type(e).__name__}: {e}")

    def _caiu(self, ws, motivo):
        if self.enc or (ws is not None and ws is not self.ws):
            return                       # encerrada de propósito ou socket já substituído
        self.ready = False
        if "session not found" in motivo and self.handle and self.reconexoes < MAX_RECONEXOES:
            self.reconexoes += 1
            # o Gemini descartou a sessão (ex. depois de um 1007): retomar não adianta, abre outra
            # do zero — perde o contexto da conversa, mas você continua falando sem reativar
            self.handle = None
            ctx.registrar(self.sid, "info", texto="🆕 sessão expirou no Gemini — abrindo uma nova")
            threading.Thread(target=self.conectar, daemon=True).start()
            return
        if self.handle and self.reconexoes < MAX_RECONEXOES:
            self.reconexoes += 1
            ctx.registrar(self.sid, "info", texto=f"🔄 conexão caiu ({motivo}) — retomando sessão")
            threading.Thread(target=self.conectar, daemon=True).start()
        else:
            ctx.registrar(self.sid, "ERRO", texto=f"sessão encerrada ({motivo})")
            self.encerrar(f"sessão encerrada ({motivo})")

    def _g(self, msg):
        try:
            self.ws.enviar(json.dumps(msg, ensure_ascii=False))
        except (OSError, AttributeError):
            pass

    def audio(self, pcm):
        # PCM 16 bits com meia amostra derruba a sessão (1007 invalid argument): realinha
        pcm = self.sobra + pcm
        corte = len(pcm) & ~1
        pcm, self.sobra = pcm[:corte], pcm[corte:]
        if not pcm:
            return
        if self.ready:
            return self._g({"realtimeInput": {"audio": {
                "data": base64.b64encode(pcm).decode(), "mimeType": "audio/pcm;rate=16000"}}})
        self.buffer += pcm
        del self.buffer[:-BUFFER_MAX]

    def texto(self, t):
        self.ultimo = time.time()
        self._g({"realtimeInput": {"text": t}})

    def _flush(self, quem):
        if quem == "voce" and self.cur_user.strip():
            ctx.registrar(self.sid, "voce", texto=self.cur_user.strip())
            self.cur_user = ""
        if quem == "rlight" and self.cur_asst.strip():
            ctx.registrar(self.sid, "rlight", texto=self.cur_asst.strip())
            self.cur_asst = ""

    def tratar(self, msg):
        if "setupComplete" in msg:
            self.ready, self.reconexoes = True, 0
            pend, self.buffer = bytes(self.buffer), bytearray()
            for i in range(0, len(pend), 3200):     # mic gravado durante a conexão
                self.audio(pend[i:i + 3200])
            if self.anunciar:
                j, self.anunciar = self.anunciar, None
                self.texto(texto_anuncio(j, self.cfg["usuario"]))
                ctx.agente_ack(j["id"], "sessão curta (terminal)")
            self.ultimo = time.time()
            self.term.enviar({"tipo": "pronto"})
            return
        upd = msg.get("sessionResumptionUpdate") or {}
        if upd.get("resumable") and upd.get("newHandle"):
            self.handle = upd["newHandle"]
        if "goAway" in msg:
            ctx.registrar(self.sid, "info", texto=f"⏳ servidor pediu troca de conexão ({msg['goAway'].get('timeLeft')})")
            if self.handle:
                velho = self.ws
                threading.Thread(target=self.conectar, daemon=True).start()
                threading.Timer(3, velho.fechar).start()
        sc = msg.get("serverContent")
        if sc:
            if sc.get("interrupted"):
                self.toca_ate = time.time()
                self.ream.reset()
                self.term.enviar({"tipo": "interrompido"})
            for p in (sc.get("modelTurn") or {}).get("parts", []):
                if (p.get("inlineData") or {}).get("data"):
                    self.falar(base64.b64decode(p["inlineData"]["data"]))
            if (sc.get("inputTranscription") or {}).get("text"):
                self.ultimo = time.time()
                self._flush("rlight")
                self.cur_user += sc["inputTranscription"]["text"]
                self.term.enviar({"tipo": "texto", "quem": "v", "t": ascii(self.cur_user.strip())[-150:]})
            if (sc.get("outputTranscription") or {}).get("text"):
                self._flush("voce")
                self.cur_asst += sc["outputTranscription"]["text"]
                self.term.enviar({"tipo": "texto", "quem": "r", "t": ascii(self.cur_asst.strip())[-470:]})
            if sc.get("turnComplete"):
                self._flush("voce")
                self._flush("rlight")
        if "toolCall" in msg:
            self.ultimo = time.time()
            resp = []
            for fc in msg["toolCall"].get("functionCalls", []):
                args = fc.get("args") or {}
                if fc["name"] == "pedir_ao_agente":    # responde na hora; resultado chega depois
                    r = ctx.agente_pedir(self.sid, args.get("tarefa", ""), terminal=self.term.id)
                else:
                    r = ctx.executar(self.sid, fc["name"], args)
                resp.append({"id": fc.get("id"), "name": fc["name"], "response": r})
            if resp:
                self._g({"toolResponse": {"functionResponses": resp}})
            self.ultimo = time.time()
        if "error" in msg:
            ctx.registrar(self.sid, "ERRO", texto=json.dumps(msg["error"], ensure_ascii=False))

    def falar(self, pcm24):
        pcm = self.ream.processar(pcm24)
        agora = time.time()
        self.toca_ate = max(self.toca_ate, agora) + len(pcm) / 32000
        self.ultimo = agora
        for i in range(0, len(pcm), CHUNK_SAIDA):
            self.term.enviar(pcm[i:i + CHUNK_SAIDA])

    def ociosa(self):
        agora = time.time()
        return (self.ready and not self.falando and agora > self.toca_ate
                and agora - max(self.ultimo, self.toca_ate) > IDLE_S)

    def encerrar(self, motivo, avisar=True):
        if self.enc:
            return
        self.enc, self.ready = True, False
        self._flush("voce")
        self._flush("rlight")
        ctx.registrar(self.sid, "fim", texto=motivo)
        if self.ws:
            self.ws.fechar()
        if self.term.sessao is self:
            self.term.sessao = None
        if avisar:
            self.term.enviar({"tipo": "fim", "motivo": motivo})


class Terminal:
    """Uma conexão WebSocket de um S3. Vive enquanto o S3 estiver ligado."""

    def __init__(self, ws, endereco):
        self.ws, self.id, self.sessao = ws, endereco, None
        self.bateria_em = 0
        self.avisou_bateria = False

    def enviar(self, msg):
        try:
            self.ws.enviar(json.dumps(msg) if isinstance(msg, dict) else msg)
        except OSError:
            pass

    def abrir(self, origem, palavra="", anunciar=None):
        if self.sessao:
            self.sessao.encerrar("substituída por nova ativação", avisar=False)
        self.sessao = Sessao(self, origem, palavra, anunciar)

    def rodar(self):
        try:
            while True:
                m = self.ws.receber()
                s = self.sessao
                if isinstance(m, bytes):
                    if s:
                        s.audio(m)
                    continue
                ev = json.loads(m)
                tipo = ev.get("tipo")
                if tipo == "ola":
                    self.id = ev.get("id") or self.id
                    print(f"{time.strftime('%H:%M:%S')}   terminal  🔌 {self.id} conectado (fw {ev.get('fw')})", flush=True)
                    with _lock:
                        _terminais[self.id] = self
                    self.enviar({"tipo": "config", "portao": PORTAO_DBFS})
                elif tipo == "inicio":
                    self.abrir(ev.get("origem", "palavra"), ev.get("palavra", ""))
                elif tipo == "vad" and s:
                    s.falando = bool(ev.get("fala"))
                    s.ultimo = time.time()
                elif tipo == "bateria" and time.time() - self.bateria_em >= BATERIA_HA_S:
                    self.bateria_em = time.time()
                    threading.Thread(target=publicar_bateria, args=(self, ev), daemon=True).start()
                elif tipo == "ota":
                    r = f"✅ gravado em {ev.get('particao')}, reiniciando" if ev.get("ok") else f"❌ {ev.get('erro')}"
                    print(f"{time.strftime('%H:%M:%S')}   terminal  📦 {self.id} OTA: {r}", flush=True)
                elif tipo == "fim" and s:
                    s.encerrar(ev.get("motivo") or "encerrado no terminal", avisar=False)
        except (ws_mini.Fechado, OSError, ValueError) as e:
            print(f"{time.strftime('%H:%M:%S')}   terminal  🔌 {self.id} desconectou ({e})", flush=True)
        finally:
            with _lock:
                if _terminais.get(self.id) is self:
                    del _terminais[self.id]
            if self.sessao:
                self.sessao.encerrar("terminal desconectou", avisar=False)
            self.ws.fechar()


def atender(handler):
    """GET /dispositivo (já autenticado): faz o upgrade e segura a conexão."""
    Terminal(ws_mini.aceitar(handler), handler.client_address[0]).rodar()


def ota(terminal=None):
    """Manda o(s) terminal(is) baixar o firmware novo de /firmware.bin (firmware/ota.sh)."""
    with _lock:
        alvos = [t for i, t in _terminais.items() if terminal in (None, "", i)]
    for t in alvos:
        print(f"{time.strftime('%H:%M:%S')}   terminal  📦 {t.id}: OTA pedido", flush=True)
        t.enviar({"tipo": "ota"})
    return [t.id for t in alvos]


def entregar(j):
    """Resultado do agente pedido por um terminal. False = deixa a página/HA cuidar."""
    with _lock:
        term = _terminais.get(j.get("terminal")) or next(iter(_terminais.values()), None)
    if not term:
        return False
    s = term.sessao
    if s and not s.enc:                                   # (a) sessão aberta (ou abrindo)
        if s.ready:
            s.texto(texto_anuncio(j, s.cfg["usuario"]))
            ctx.agente_ack(j["id"], "sessão aberta (terminal)")
        else:
            s.anunciar = j                                # vai junto no setupComplete
        return True
    if madrugada():                                       # (c) central do HA
        ctx.agente_ack(j["id"], "madrugada (terminal)", False)
        return True
    term.enviar({"tipo": "sessao", "origem": "agente"})  # (b) sessão curta só pra falar
    term.abrir("agente", anunciar=j)
    return True


def _vigia_inatividade():
    while True:
        time.sleep(1)
        with _lock:
            sessoes = [t.sessao for t in _terminais.values() if t.sessao]
        for s in sessoes:
            if s.ociosa():
                s.encerrar("encerrado por inatividade")
