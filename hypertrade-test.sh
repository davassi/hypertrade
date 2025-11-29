#!/usr/bin/env bash

# Export env vars from pass
export HYPERTRADE_MASTER_ADDR=$(pass show hypertrade_test/master_addr | head -n 1)
export HYPERTRADE_API_WALLET_PRIV=$(pass show hypertrade_test/api_wallet_priv | head -n 1)
export HYPERTRADE_SUBACCOUNT_ADDR=
export HYPERTRADE_LISTEN_PORT=6488
export HYPERTRADE_DB_PATH=./hypertrade_test.db

# Launch the server
uvicorn hypertrade.daemon:app --host 0.0.0.0 --port $HYPERTRADE_LISTEN_PORT --reload
