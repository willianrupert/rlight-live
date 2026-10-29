#!/usr/bin/env python3
"""Helper SOMENTE LEITURA usado pelo agente em segundo plano do RLight Live.

O `claude -p` do worker só tem permissão para Read/Grep/Glob e para rodar este
script. Por isso nada aqui pode mudar estado: só GET (e o /api/template do HA,
que só renderiza).

  python3 servidor/agente_ler.py buscar ar sala
  python3 servidor/agente_ler.py estado sensor.x [sensor.y …]
  python3 servidor/agente_ler.py historico sensor.x [horas]
  python3 servidor/agente_ler.py logbook [horas] [entity_id]
  python3 servidor/agente_ler.py template '{{ ... }}'
  python3 servidor/agente_ler.py ha /api/<caminho>          # GET qualquer endpoint do HA
"""
import json
import sys
import time

sys.path.insert(0, __import__("os").path.dirname(__file__))
import voz_server as vs  # noqa: E402  (reaproveita token, cache de áreas e funções de leitura)

MAX = 15000


def out(obj):
    txt = obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False, indent=1)
    print(txt[:MAX] + ("\n…(cortado)" if len(txt) > MAX else ""))


def main(cmd, *a):
    if cmd == "buscar":
        return out(vs.buscar_entidades(" ".join(a), limite=40))
    if cmd == "estado":
        return out(vs.ler_estado(list(a)))
    if cmd == "historico":
        return out(vs.historico(a[0], float(a[1]) if len(a) > 1 else 24))
    if cmd == "logbook":
        horas = float(a[0]) if a else 24
        start = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time() - horas * 3600))
        q = f"?entity={a[1]}" if len(a) > 1 else ""
        ev = vs.ha("GET", f"/api/logbook/{start}Z{q}")
        return out([{k: e.get(k) for k in ("when", "name", "entity_id", "state", "message")} for e in ev][-200:])
    if cmd == "template":
        return out(vs.ha("POST", "/api/template", {"template": a[0]}))
    if cmd == "ha":
        return out(vs.ha("GET", a[0]))
    sys.exit(__doc__)


if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--ajuda", "--help"):
        sys.exit(__doc__)
    try:
        main(*sys.argv[1:])
    except Exception as e:
        sys.exit(f"erro: {type(e).__name__}: {e}")
