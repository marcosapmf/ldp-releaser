#!/usr/bin/env bash
# Opens LDP Releaser in the browser, starting the server first when it is
# not running. The server is detached, so it outlives the Claude session.

set -euo pipefail

# The skill is a link to the skill folder of the checkout.

app="$(dirname "$(readlink -f "$0")")/../../ldp-releaser"
log=~/.local/state/ldp-releaser/server.log

port=$(python3 -c '
import pathlib, tomllib
path = pathlib.Path.home() / ".config/ldp-releaser/config.toml"
print(tomllib.loads(path.read_text()).get("server", {}).get("port", 8766) if path.exists() else 8766)')
url="http://127.0.0.1:${port}/"

running() {
	curl -s -o /dev/null --max-time 2 "$url"
}

if [ "${1:-}" = "--restart" ]; then
	pkill -f "python3 -m ldp_releaser" || true

	while running; do
		sleep 0.2
	done
fi

if running; then
	echo "Already running at $url"
else
	mkdir -p "$(dirname "$log")"
	setsid nohup "$app" < /dev/null >> "$log" 2>&1 &

	for _ in $(seq 50); do
		if running; then
			break
		fi

		sleep 0.2
	done

	if ! running; then
		echo "The server did not start; last lines of $log:" >&2
		tail -20 "$log" >&2

		exit 1
	fi

	echo "Started at $url (log: $log)"
fi

xdg-open "$url" > /dev/null 2>&1 &
