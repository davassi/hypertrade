#!/usr/bin/env fish

# Export env vars from pass
set -x HYPERTRADE_MASTER_ADDR     (pass show hypertrade_test/master_addr | head -n 1)
set -x HYPERTRADE_API_WALLET_PRIV (pass show hypertrade_test/api_wallet_priv | head -n 1)
set -x HYPERTRADE_SUBACCOUNT_ADDR

# Launch the server
uvicorn hypertrade.daemon:app --host 0.0.0.0 --port 6488 --reload

