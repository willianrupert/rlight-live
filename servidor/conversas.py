#!/usr/bin/env python3
"""Navega no histórico do RLight Live (conversas/AAAA-MM-DD/<sessão>.jsonl).

  python3 conversas.py                  lista as últimas 20 sessões
  python3 conversas.py listar 50        lista as últimas 50
  python3 conversas.py ver ultima       mostra uma sessão (id, prefixo do id, 'ultima' ou -N)
  python3 conversas.py buscar bomba     procura em falas, ferramentas e resultados
  python3 conversas.py problemas        sessões com erro, busca vazia ou ativação falsa
"""
import json
import sys
from datetime import datetime
from pathlib import Path

PASTA = Path(__file__).resolve().parent / "conversas"


def sessoes():
    return sorted(PASTA.glob("*/*.jsonl"), key=lambda p: p.stat().st_mtime)   # última atividade


def carregar(p):
    return [json.loads(l) for l in p.read_text().splitlines() if l.strip()]


def resumo(p):
    ev = carregar(p)
    t = [datetime.fromisoformat(e["t"]) for e in ev]
    falas = [e for e in ev if e["tipo"] == "voce"]
    tools = [e for e in ev if e["tipo"] == "ferramenta" or (e["tipo"] == "agente" and e.get("fase") == "pedido")]
    erros = [e for e in ev if e["tipo"] == "ERRO" or (e["tipo"] == "ferramenta" and "erro" in (e.get("resultado") or {}))
             or (e["tipo"] == "agente" and e.get("fase") == "erro")]
    vazias = [e for e in tools if isinstance(e.get("resultado"), dict) and e["resultado"].get("total") == 0]
    inicio = next((e["texto"] for e in ev if e["tipo"] == "inicio"), "")
    fim = next((e["texto"] for e in ev if e["tipo"] == "fim"), "(aberta)")
    return {"id": p.stem, "inicio": t[0] if t else None, "dur": (t[-1] - t[0]).seconds if t else 0,
            "origem": inicio.split()[0].replace("origem=", "") if inicio else "?",
            "falas": len(falas), "tools": len(tools), "erros": len(erros), "vazias": len(vazias),
            "fim": fim, "primeira": falas[0]["texto"] if falas else ""}


def achar(ref):
    todas = sessoes()
    if not todas:
        sys.exit("nenhuma conversa registrada ainda")
    if ref in ("ultima", "última"):
        return todas[-1]
    if ref.lstrip("-").isdigit():
        return todas[int(ref)] if ref.startswith("-") else todas[-int(ref)]
    hits = [p for p in todas if p.stem.startswith(ref)]
    if not hits:
        sys.exit(f"nenhuma sessão começa com {ref!r}")
    return hits[-1]


def listar(n=20):
    for p in sessoes()[-int(n):]:
        r = resumo(p)
        flags = (f" ⚠️{r['erros']}" if r["erros"] else "") + (f" ∅{r['vazias']}" if r["vazias"] else "")
        print(f"{r['id']}  {r['dur']:>4}s  {r['origem']:<7} 🗣️{r['falas']:<2} 🔧{r['tools']:<2}{flags:<8} "
              f"{r['primeira'][:70]}")


def ver(ref):
    p = achar(ref)
    print(f"── {p.stem} ──")
    for e in carregar(p):
        h = e["t"][11:]
        if e["tipo"] == "voce":
            print(f"{h}  🗣️  {e['texto']}")
        elif e["tipo"] == "rlight":
            print(f"{h}  🤖  {e['texto']}")
        elif e["tipo"] == "agente":
            extra = f"  [{e['ms'] // 1000}s" + (f", US$ {e['custo_usd']:.3f}" if e.get("custo_usd") else "") + "]" if e.get("ms") else ""
            print(f"{h}  🧠  agente {e['fase']} {e['job']}: {e.get('texto', '')}{extra}")
            if e.get("proposta"):
                print(f"          proposta → {json.dumps(e['proposta'], ensure_ascii=False)}")
        elif e["tipo"] == "ferramenta":
            res = json.dumps(e.get("resultado"), ensure_ascii=False)
            print(f"{h}  🔧  {e['nome']}({json.dumps(e.get('args'), ensure_ascii=False)})  [{e.get('ms')} ms]\n"
                  f"          → {res[:600]}")
        else:
            print(f"{h}  ·   {e['tipo']}: {e.get('texto', '')}")


def buscar(*termos):
    q = " ".join(termos).lower()
    for p in sessoes():
        for e in carregar(p):
            txt = json.dumps(e, ensure_ascii=False).lower()
            if q in txt:
                print(f"{p.stem}  {e['t'][11:]}  {e['tipo']:<10} {(e.get('texto') or e.get('nome', ''))[:90]}")


def problemas():
    for p in sessoes():
        r = resumo(p)
        motivos = []
        if r["erros"]:
            motivos.append(f"{r['erros']} erro(s)")
        if r["vazias"]:
            motivos.append(f"{r['vazias']} busca(s) sem resultado")
        if r["falas"] == 0:
            motivos.append("ninguém falou (ativação falsa?)")
        if motivos:
            print(f"{r['id']}  {', '.join(motivos):<45} {r['primeira'][:50]}")


if __name__ == "__main__":
    cmd, *args = sys.argv[1:] or ["listar"]
    {"listar": listar, "ver": ver, "buscar": buscar, "problemas": problemas}[cmd](*args)
