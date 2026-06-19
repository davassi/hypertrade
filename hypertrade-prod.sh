#!/usr/bin/env bash

# Pre-flight: guided setup if required secrets are not in pass yet.
if ! pass show hypertrade/master_addr >/dev/null 2>&1; then
  HYPERTRADE_ENVIRONMENT=prod python -m hypertrade.setup || exit 1
fi

# Export env vars from pass
export HYPERTRADE_ENVIRONMENT=prod
export HYPERTRADE_MASTER_ADDR=$(pass show hypertrade/master_addr | head -n 1)
export HYPERTRADE_API_WALLET_PRIV=$(pass show hypertrade/api_wallet_priv | head -n 1)
export HYPERTRADE_SUBACCOUNT_ADDR=$(pass show hypertrade/subaccount_addr | head -n 1)
export HYPERTRADE_WEBHOOK_SECRET=$(pass show hypertrade/webhook_secret 2>/dev/null | head -n 1)
export HYPERTRADE_LISTEN_PORT=6487
export HYPERTRADE_DB_PATH=./hypertrade_prod.db

# Launch the server
uvicorn hypertrade.daemon:app --host 0.0.0.0 --port $HYPERTRADE_LISTEN_PORT --workers 1 --loop uvloop --http httptools --log-level info --access-log --use-colors --limit-concurrency 1000
