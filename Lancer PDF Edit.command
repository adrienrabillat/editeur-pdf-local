#!/bin/bash
# Double-clique sur ce fichier pour lancer l'éditeur PDF.
cd "$(dirname "$0")"
PORT=8765
if [ ! -x .venv/bin/python ]; then
  echo "Première installation (une seule fois)…"
  PY=$(command -v python3.13 || command -v python3.12 || command -v python3)
  "$PY" -m venv .venv && .venv/bin/pip install -q --upgrade pip && .venv/bin/pip install -q -r requirements.txt || { echo "Installation impossible"; read; exit 1; }
fi
if lsof -i :$PORT -sTCP:LISTEN >/dev/null 2>&1; then
  open "http://localhost:$PORT"
  exit 0
fi
( sleep 1.5; open "http://localhost:$PORT" ) &
echo "PDF Edit tourne sur http://localhost:$PORT — ferme cette fenêtre pour l'arrêter."
exec .venv/bin/python -m uvicorn server.app:app --host 127.0.0.1 --port $PORT
