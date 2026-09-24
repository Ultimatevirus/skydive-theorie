#!/bin/sh
set -eu

python -m db
exec "$@"