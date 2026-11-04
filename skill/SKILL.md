---
name: ldp-releaser
description: Open the LDP Releaser app in the browser, starting its server first if it is not running, or restart it. Use when the user asks to open, start, launch, show or restart "the releaser" or LDP Releaser, or the release app.
---

# Open LDP Releaser

Run the script and report its one-line result (the URL, and whether it was
already running):

```
~/.claude/skills/ldp-releaser/scripts/open.sh
```

When the user asks to **restart** the server (for example after changing
the code or the config files), pass `--restart`.

When settings are missing, or Jenkins or Jira rejects a saved token, the
server serves its setup page at the same URL instead of the releaser; tell
the user to finish the setup there. Do not run `./ldp-releaser --setup` for
them: it asks for secrets in the terminal.

The script starts the server detached (`setsid nohup`), so do not run it with
`run_in_background` and do not start `ldp-releaser` any other way. The
server log is `~/.local/state/ldp-releaser/server.log`; if the script says the
server did not start, show the lines it printed and stop.
