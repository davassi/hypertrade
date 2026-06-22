#!/usr/bin/env fish

# Pre-flight: guided setup if required secrets are not in pass yet.
if not pass show hypertrade_test/master_addr >/dev/null 2>&1
  env HYPERTRADE_ENVIRONMENT=test python -m hypertrade.setup; or exit 1
end

# Export env vars from pass
set -x HYPERTRADE_ENVIRONMENT     test
set -x HYPERTRADE_MASTER_ADDR     (pass show hypertrade_test/master_addr | head -n 1)
set -x HYPERTRADE_API_WALLET_PRIV (pass show hypertrade_test/api_wallet_priv | head -n 1)
set -x HYPERTRADE_SUBACCOUNT_ADDR
set -x HYPERTRADE_WEBHOOK_SECRET (pass show hypertrade_test/webhook_secret 2>/dev/null | head -n 1)
set -x HYPERTRADE_LISTEN_PORT     6488
set -x HYPERTRADE_DB_PATH         ./hypertrade_test.db

# Launch the server
uvicorn hypertrade.daemon:app --host 0.0.0.0 --port $HYPERTRADE_LISTEN_PORT --reload

