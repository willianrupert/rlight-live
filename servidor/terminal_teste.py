#!/usr/bin/env python3
"""Terminal S3 de mentira: testa o /dispositivo do voz_server.py sem a placa.

Fala frases (WAV 16 kHz mono, ex. geradas com o `say` do Mac) em tempo real como se
fosse o mic do S3, grava a voz do RLight em resposta.wav e imprime os eventos.
Com --interromper, a 2ª frase entra 1,5 s depois de o RLight começar a falar.

  say -v Luciana -o /tmp/f1.aiff "Oi, quantos graus faz lá fora?"
  afconvert -f WAVE -d LEI16@16000 -c 1 /tmp/f1.aiff /tmp/f1.wav
  python3 terminal_teste.py ws://127.0.0.1:8765 /tmp/f1.wav [/tmp/f2.wav --interromper]
"""
import json
import os
import re
import sys
import threading
import time
import wave
from pathlib import Path

import ws_mini

args = [a for a in sys.argv[1:] if not a.startswith("--")]
url, frases = args[0], args[1:]
env = Path(__file__).resolve().parent / "config.env"
tok = os.environ.get("RLIGHT_TERMINAL_TOKEN") or re.search(
    r"^RLIGHT_TERMINAL_TOKEN=(.+)$", env.read_text() if env.exists() else "", re.M).group(1).strip()
ws = ws_mini.conectar(f"{url}/dispositivo?token={tok}")
ws.enviar(json.dumps({"tipo": "ola", "id": "teste", "fw": "fake"}))

saida, fim, falando = bytearray(), threading.Event(), threading.Event()
t0 = time.time()


def ler():
    try:
        while True:
            m = ws.receber()
            if isinstance(m, bytes):
                if not falando.is_set():
                    print(f"{time.time() - t0:6.1f}s  🔊 começou a falar")
                falando.set()
                saida.extend(m)
                continue
            print(f"{time.time() - t0:6.1f}s  ← {m}")
            ev = json.loads(m)
            if ev["tipo"] == "interrompido":
                falando.clear()
            if ev["tipo"] == "fim":
                fim.set()
    except ws_mini.Fechado:
        fim.set()


def tocar(caminho):
    with wave.open(caminho) as w:
        assert w.getframerate() == 16000 and w.getnchannels() == 1, "precisa ser 16 kHz mono"
        pcm = w.readframes(w.getnframes())
    pcm += bytes(32000)    # 1 s de silêncio no fim, como o mic real
    for i in range(0, len(pcm), 1024):          # 32 ms por frame, como o AFE
        ws.enviar(pcm[i:i + 1024])
        time.sleep(0.032)


def silencio_ate(ev):
    while not ev.is_set() and not fim.is_set():
        ws.enviar(bytes(1024))
        time.sleep(0.032)


threading.Thread(target=ler, daemon=True).start()
ws.enviar(json.dumps({"tipo": "inicio", "origem": "palavra", "palavra": "teste"}))
tocar(frases[0])
for f in frases[1:]:
    if "--interromper" in sys.argv:
        silencio_ate(falando)
        for _ in range(int(1.5 / 0.032)):
            ws.enviar(bytes(1024))
            time.sleep(0.032)
        print(f"{time.time() - t0:6.1f}s  🗣️ interrompendo com {f}")
    tocar(f)
threading.Thread(target=silencio_ate, args=(fim,), daemon=True).start()
fim.wait(90)
with wave.open("resposta.wav", "wb") as w:
    w.setnchannels(1), w.setsampwidth(2), w.setframerate(16000)
    w.writeframes(bytes(saida))
print(f"resposta.wav: {len(saida) / 32000:.1f} s de voz")
ws.fechar()
