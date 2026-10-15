#!/usr/bin/env bash
# Wrapper for cron: loads .env and ships one BigFix inventory snapshot.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${PROJECT_DIR}"

if [[ ! -f .env ]]; then
  echo "ERROR: .env not found in ${PROJECT_DIR}. Copy .env.example to .env and configure." >&2
  exit 1
fi

if [[ ! -x .venv/bin/python ]]; then
  echo "ERROR: .venv not found. Run: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt" >&2
  exit 1
fi

set -a
# shellcheck disable=SC1091
source .env
set +a

exec .venv/bin/python bigfix_inventory_to_coralogix.py
