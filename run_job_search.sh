#!/bin/bash
# Wrapper for scheduled (cron) runs. Cron doesn't load your shell environment,
# so this script moves into the project folder and loads the API key from .env.

cd "$(dirname "$0")" || exit 1

# Load ANTHROPIC_API_KEY from .env (never hard-code the key in this file)
if [ -f .env ]; then
  set -a; source .env; set +a
fi

python3 job_search_agent.py
