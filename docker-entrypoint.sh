#!/bin/bash
# market-data-service entrypoint.
#
# Just execs the supplied command (default: uvicorn). Previously this
# script also booted IB Gateway + IBC and waited on 127.0.0.1:4004;
# after the Longbridge swap those steps are gone — the app talks to
# Longbridge's public endpoints over the network directly.

set -Eeo pipefail

echo "==> Launching market-data-service"
exec "$@"