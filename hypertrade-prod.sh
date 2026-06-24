#!/usr/bin/env bash
# ALWAYS run from the project .venv: the system / pyenv-global interpreter can carry
# an incompatible hyperliquid SDK (the SDK breaks across minors — see the pyproject
# pin). Build the venv once with:  python -m venv .venv && .venv/bin/pip install -e .
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="$DIR/.venv/bin/python"; UVICORN="$DIR/.venv/bin/uvicorn"
[ -x "$UVICORN" ] || { echo "missing $DIR/.venv — run: python -m venv .venv && .venv/bin/pip install -e ." >&2; exit 1; }

# Pre-flight: guided setup if required secrets are not in pass yet.
if ! pass show hypertrade/master_addr >/dev/null 2>&1; then
  HYPERTRADE_ENVIRONMENT=prod "$PY" -m hypertrade.setup || exit 1
fi

# Export env vars from pass
export HYPERTRADE_ENVIRONMENT=prod
export HYPERTRADE_MASTER_ADDR=$(pass show hypertrade/master_addr | head -n 1)
export HYPERTRADE_API_WALLET_PRIV=$(pass show hypertrade/api_wallet_priv | head -n 1)
export HYPERTRADE_SUBACCOUNT_ADDR=$(pass show hypertrade/subaccount_addr 2>/dev/null | head -n 1)
export HYPERTRADE_WEBHOOK_SECRET=$(pass show hypertrade/webhook_secret 2>/dev/null | head -n 1)
export HYPERTRADE_LISTEN_PORT=6487
export HYPERTRADE_DB_PATH=./hypertrade_prod.db

# Launch the server from the project venv (uvicorn must be the .venv one, not a
# pyenv shim). --host 0.0.0.0 is kept for external-webhook setups; for the
# Cointegration Desk (same-box localhost) the systemd unit binds 127.0.0.1.
exec "$UVICORN" hypertrade.daemon:app --host 0.0.0.0 --port "$HYPERTRADE_LISTEN_PORT" --workers 1 --loop uvloop --http httptools --log-level info --access-log --use-colors --limit-concurrency 1000
