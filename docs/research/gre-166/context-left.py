#!/usr/bin/env python3
"""Diz quanto de contexto sobrou numa sessao de agente.

Uso:
  context-left.py claude <transcript.jsonl> [janela]
  context-left.py codex  <rollout.jsonl>

Sai com codigo 0 sempre; imprime JSON numa linha:
  {"used": 134840, "window": 200000, "left_pct": 32.6, "model": "..."}
"""
import json, sys, os

# Claude Code nao grava o tamanho da janela no transcript. Tabela minima.
# 1M so vale se a sessao rodou com o beta de contexto estendido.
CLAUDE_WINDOWS = {"default": 200_000, "1m": 1_000_000}


def claude(path, window=None):
    used, model = 0, None
    for line in open(path, errors="ignore"):
        try:
            d = json.loads(line)
        except Exception:
            continue
        m = d.get("message")
        if isinstance(m, dict) and isinstance(m.get("usage"), dict):
            u = m["usage"]
            used = ((u.get("input_tokens") or 0)
                    + (u.get("cache_read_input_tokens") or 0)
                    + (u.get("cache_creation_input_tokens") or 0))
            model = m.get("model") or model
    win = window or CLAUDE_WINDOWS["default"]
    return used, win, model


def codex(path):
    """O rollout do Codex traz a janela de graca. Nao precisa de tabela."""
    used, win, model = 0, None, None
    for line in open(path, errors="ignore"):
        try:
            d = json.loads(line)
        except Exception:
            continue
        p = d.get("payload") if isinstance(d.get("payload"), dict) else {}
        if d.get("type") == "turn_context":
            model = p.get("model") or model
        if p.get("type") == "token_count" and isinstance(p.get("info"), dict):
            i = p["info"]
            used = (i.get("last_token_usage") or {}).get("total_tokens", used)
            win = i.get("model_context_window") or win
        # compaction zera a contagem util: a janela recomecou
        if d.get("type") == "compacted":
            used = 0
    return used, win or 0, model


kind, path = sys.argv[1], sys.argv[2]
window = int(sys.argv[3]) if len(sys.argv) > 3 else None
used, win, model = (claude(path, window) if kind == "claude" else codex(path))
left = 100.0 * (1 - used / win) if win else -1.0
print(json.dumps({"used": used, "window": win,
                  "left_pct": round(left, 1), "model": model}))
