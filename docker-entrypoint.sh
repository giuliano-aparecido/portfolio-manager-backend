#!/bin/sh
set -e

# Render (and any other container deploy) runs no migration step of its
# own, so apply any pending Alembic migrations before the app starts.
# alembic/env.py reads DATABASE_URL from the app's own settings — the same
# connection string the app uses — so nothing extra to configure here.
#
# `set -e` above means a failed migration aborts startup and fails the
# deploy loudly, rather than booting an app against a schema it expects
# columns/tables to exist in.
#
# Assumes a single web instance (see README — in-memory rate limiting, no
# Redis): every container runs this on deploy, and there's no advisory
# lock around it. If instance count is ever raised, move `alembic upgrade
# head` to a Render pre-deploy command instead.
echo "==> alembic upgrade head"
alembic upgrade head

echo "==> starting: $*"
exec "$@"
