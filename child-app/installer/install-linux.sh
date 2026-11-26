#!/bin/sh
set -eu

package=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd -P)
exec "$package/bin/kotoyose-child-app" install-linux "$@"
