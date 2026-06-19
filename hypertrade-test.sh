#!/usr/bin/env bash

# Pre-flight: guided setup if required secrets are not in pass yet.
if ! pass show hypertrade_test/master_addr >/dev/null 2>&1; then
  HYPERTRADE_ENVIRONMENT=test python -m hypertrade.setup || exit 1
fi

# Export env vars from pass
export HYPERTRADE_ENVIRONMENT=test
export HYPERTRADE_MASTER_ADDR=$(pass show hypertrade_test/master_addr | head -n 1)
export HYPERTRADE_API_WALLET_PRIV=$(pass show hypertrade_test/api_wallet_priv | head -n 1)
export HYPERTRADE_SUBACCOUNT_ADDR=
export HYPERTRADE_WEBHOOK_SECRET=$(pass show hypertrade_test/webhook_secret 2>/dev/null | head -n 1)
export HYPERTRADE_LISTEN_PORT=6488
export HYPERTRADE_DB_PATH=./hypertrade_test.db

# Launch the server
uvicorn hypertrade.daemon:app --host 0.0.0.0 --port $HYPERTRADE_LISTEN_PORT --reload
