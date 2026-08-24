"""Extracted from main.py so routers (e.g. agent.py) can apply per-route
overrides without importing back from main.py, which imports every router
and would create a circular import.
"""

from slowapi import Limiter
from slowapi.util import get_remote_address

# Applies to every route via default_limits, no per-route decorators needed
# except where a route overrides it (see agent.py's tighter 6/minute limit).
# Keyed by client IP - see the Dockerfile's --proxy-headers flag, without
# which every request behind Render's proxy would share one IP and thus one
# bucket. 60/minute comfortably covers real usage (a handful of page loads
# and refreshes per session) while still capping abusive/bot traffic - the
# real risk on a personal, allowlist-gated app is a leaked token spamming
# the yfinance-backed endpoints (Yahoo can rate-limit or block the whole
# outbound IP for that) or bots probing public URLs and burning Render's
# free-tier compute, not deliberate multi-user abuse.
limiter = Limiter(key_func=get_remote_address, default_limits=["60/minute"])
