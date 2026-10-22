#!/usr/bin/env bash
# Prints the hand-off key: HKDF-SHA256 (RFC 5869, one 32-byte block).
#   IKM_HANDOFF  secrets.SQUADHQ_HANDOFF_KEY (preferred) — or —
#   IKM_LLM      the single LLM credential for this run's auth mode (never a
#                pool account's: prep does not hold those — a pool needs
#                SQUADHQ_HANDOFF_KEY unless the single credential is also set)
#   SALT         "<run id>:<artifact name>"
# The same script runs inline (copied) in the agent and post jobs, which hold
# the same secret; nothing key-related is ever written to outputs or logs.
set -euo pipefail
/usr/bin/python3 -I -c '
import hashlib, hmac, os, sys
ikm = (os.environ.get("IKM_HANDOFF") or os.environ.get("IKM_LLM") or "").encode()
if not ikm:
    sys.exit("hand-off: no key material (set SQUADHQ_HANDOFF_KEY; required with an account pool)")
prk = hmac.new(os.environ["SALT"].encode(), ikm, hashlib.sha256).digest()
print(hmac.new(prk, b"squadhq-handoff-v1\x01", hashlib.sha256).hexdigest())
'
