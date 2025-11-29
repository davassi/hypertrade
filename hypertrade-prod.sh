#!/usr/bin/env bash

# Export env vars from pass
export HYPERTRADE_MASTER_ADDR=$(pass show hypertrade/master_addr | head -n 1)
export HYPERTRADE_API_WALLET_PRIV=$(pass show hypertrade/api_wallet_priv | head -n 1)
export HYPERTRADE_SUBACCOUNT_ADDR=$(pass show hypertrade/subaccount_addr | head -n 1)
export HYPERTRADE_LISTEN_PORT=6487
export HYPERTRADE_DB_PATH=./hypertrade_prod.db

# Launch the server
uvicorn hypertrade.daemon:app --host 0.0.0.0 --port $HYPERTRADE_LISTEN_PORT --workers 1 --loop uvloop --http httptools --log-level info --access-log --use-colors --limit-concurrency 1000
