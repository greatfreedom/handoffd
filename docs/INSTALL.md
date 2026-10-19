# handoffd — install, update, uninstall, known limitations

macOS + Warp only, experimental. Every step below that edits a `settings.json` has an undo step, and all of them rely on one baseline backup that you make **once, before you start**. Nothing here needs `sudo`.

## Before you start: back up once

Run this in the project directory. It copies each settings file to `<file>.handoffd-backup` **only if that backup does not exist yet**, so the first backup — your “before handoffd” baseline — is never overwritten, however many times you repeat the procedure or re-run these lines:

```bash
for F in .claude/settings.json .claude/settings.local.json ~/.claude/settings.json; do
  if [ -f "$F" ] && [ ! -e "$F.handoffd-backup" ]; then cp "$F" "$F.handoffd-backup"; fi
done
```

To undo, restore the keys you changed (`statusLine`, `permissions.allow`, `hooks`) **from the `.handoffd-backup` file** — do not copy the whole file back over a settings file that may have legitimately changed since. Do not make further backups under the same name.

## Install (as a Claude Code plugin)

```text
/plugin marketplace add greatfreedom/handoffd
/plugin install handoffd@handoffd
```

(or one step on Claude Code ≥ 2.1.275: `/plugin install handoffd --marketplace greatfreedom/handoffd`). The plugin registers the 11 hook events and the `handoffd` skill. A plugin **cannot** set `statusLine` or `permissions.allow`, and its `bin/` is only on the PATH of Claude’s Bash tool (not your terminal), so finish the setup by hand. `<handoffd-install-dir>` below is the plugin directory `~/.claude/plugins/cache/…/handoffd/<version>/` (or a clone of this repository).

1. **Terminal command.** A symlink does **not** work (the script resolves its own path), so create a small wrapper:
   ```bash
   mkdir -p ~/.local/bin
   cat > ~/.local/bin/handoffd <<'EOF'
   #!/bin/bash
   exec /usr/bin/python3 "<handoffd-install-dir>/bin/handoffd" "$@"
   EOF
   chmod +x ~/.local/bin/handoffd
   command -v handoffd      # must print ~/.local/bin/handoffd
   ```
   If `command -v handoffd` prints nothing, add `export PATH="$HOME/.local/bin:$PATH"` to `~/.zshrc` and open a new terminal. Then, inside any project, `handoffd status` must run (it is read-only).
2. **Merge permissions.** In the project’s `.claude/settings.json` (or `settings.local.json`; baseline backup already made above) add these four strings to the **existing** `permissions.allow` array — merge, never replace the array, and keep every entry that is already there. Write down which of the four were not there before; only those are removed on uninstall.
   ```json
   "Bash(handoffd:*)", "SendMessage", "ListAgents", "Read(//private/tmp/claude-<your-uid>/**)"
   ```
   `<your-uid>` is the output of `id -u`. Do not enable any bypass-permissions mode.
3. **Status line — required, not optional.** handoffd reads the context percentage and A’s model/effort from a snapshot that the status-line tap writes. Without the tap there is no snapshot, the context window size is unknown, and the 65% / 76% triggers never fire (`cdm/ctx.py` `context_percent` returns “unknown-window”; `cdm/hook_nudge.py` then skips the reminder). Setting `CTX_WINDOW` by hand is not a substitute: a wrong value mis-triggers 1M-context sessions.
   - First look at what you have: `grep -n -A3 statusLine .claude/settings.json ~/.claude/settings.json`. **Copy the existing `statusLine` value somewhere safe** (it is also in the baseline backup of whichever settings file holds it).
   - **No existing `statusLine`:** set `"statusLine": {"type": "command", "command": "/bin/bash <handoffd-install-dir>/bin/statusline_tap.sh"}`. (The tap forwards to the `claude-hud` plugin if you have it; otherwise it just records the snapshot and prints nothing.)
   - **You already have a `statusLine`:** do not just overwrite it. `statusline_tap.sh` only forwards to `claude-hud`, so put the tap *in front of your own command* with your own small script, and point `statusLine` at that script:
     ```bash
     #!/bin/bash
     input=$(cat)
     printf '%s' "$input" | /usr/bin/python3 "<handoffd-install-dir>/cdm/ctx.py" tap "${CLAUDE_PROJECT_DIR:-$PWD}" >/dev/null 2>&1
     printf '%s' "$input" | <your original statusLine command, copied verbatim>
     ```
   - Undo (see Uninstall): put the saved original value back, or delete the key if you had none.
4. **Optional project config** `<project>/.claude/handoffd.json` (unknown keys are rejected). See the Chinese section of the README for the fields; `b_verify_commands` must match the commands in your kit **character for character**.
5. Check: `handoffd status` (read-only). Then, in your own session, `!handoffd on`. Only a human may run `on` / `adopt`; the hooks block the AI from doing it.

## Update the plugin

The plugin directory name contains the version, so after `/plugin update` three things still point at the old directory (it is removed about 14 days later): your `~/.local/bin/handoffd` wrapper, your `statusLine` command (or your own tap script, step 3), and the launchd job created by `handoffd on`. The recommended fix is tracked in the maintainer notes and is **not** implemented in 0.1.0, so do this by hand:

1. In every project that is on: `handoffd off`, and repeat it until it reports the daemon stopped (see Uninstall step 1).
2. Edit the path in the wrapper (step 1) **and** in `statusLine` / your tap script (step 3).
3. `/reload-plugins` (hooks switch to the new directory), then `handoffd status`, then `!handoffd on`.

## Alternative: manual install without the plugin (untested)

Clone the repository somewhere stable and make the baseline backup (“Before you start”). Then copy the entries of the `hooks` block of `hooks/hooks.json` into your settings file **appending to your existing hooks**, changing **only** the literal `${CLAUDE_PLUGIN_ROOT}` to the absolute clone path and nothing else (the uninstall script below recognises exactly those entries). Then follow steps 1–5 with `<handoffd-install-dir>` = the clone. An `install.sh` helper is not included in 0.1.0.

## Uninstall

1. **Stop the daemon, completely.** In every project you turned on: `handoffd off`. If it says the daemon is kept because something is unfinished, wait until `handoffd status` shows nothing pending, then run `handoffd off` **again** — the daemon exiting by itself does not unload its launchd job; only a later `off` does. Repeat until `off` reports the daemon stopped.
2. **Verify:** `launchctl list | grep local.handoffd` (read-only) must print nothing. If it still lists a job, do **not** remove the plugin yet; go back to step 1.
3. **Undo the settings you changed** (use the `*.handoffd-backup` files as the reference):
   - `statusLine`: restore the original value you saved in install step 3, or delete the key if you had none. If you made your own tap script, delete it after restoring.
   - `permissions.allow`: remove only the entries that you added (step 2); keep everything else.
   - Manual-install users only — remove only the 11 hooks that you copied from `hooks/hooks.json`, keeping every other hook (including any of yours that merely mention `hook_relay.py`). The script rebuilds the exact 11 entries from the clone’s `hooks/hooks.json` (with `${CLAUDE_PLUGIN_ROOT}` replaced by the clone path) and removes only hooks identical to them; absolute paths are compared in normalised form on both sides, so it does not matter whether you wrote the clone path with `//`, `/./` or a trailing slash at install time. Run it **before** deleting the clone, with `D` = the same directory you used at install; an entry you edited after copying is left alone (delete it by hand — the script says how many it removed):
     ```bash
     S=.claude/settings.json; D=<handoffd-install-dir>
     cp "$S" "$S.before-handoffd-removal-$(date +%Y%m%d-%H%M%S)"
     /usr/bin/python3 - "$S" "$D" <<'PY'
     import json, os, re, sys
     path, root = sys.argv[1], os.path.abspath(sys.argv[2])

     def fill(value):
         if isinstance(value, str):
             return value.replace("${CLAUDE_PLUGIN_ROOT}", root)
         if isinstance(value, list):
             return [fill(item) for item in value]
         if isinstance(value, dict):
             return {key: fill(item) for key, item in value.items()}
         return value

     def norm(value):
         # same rule for both sides, so `D//cdm/…`, `D/./cdm/…` or a trailing slash still match
         if isinstance(value, str):
             return os.path.normpath(re.sub(r"^/+", "/", value)) if value.startswith("/") else value
         if isinstance(value, list):
             return [norm(item) for item in value]
         if isinstance(value, dict):
             return {key: norm(item) for key, item in value.items()}
         return value

     def canon(hook):
         return json.dumps(norm(hook), sort_keys=True, ensure_ascii=False)

     with open(os.path.join(root, "hooks", "hooks.json"), encoding="utf-8") as stream:
         shipped = json.load(stream)["hooks"]
     ours = {canon(fill(hook)) for groups in shipped.values() for group in groups for hook in group["hooks"]}
     with open(path, encoding="utf-8") as stream:
         settings = json.load(stream)
     hooks = settings.get("hooks", {})
     removed = 0
     for event in list(hooks):
         groups = []
         for group in hooks[event]:
             kept = [hook for hook in group.get("hooks", []) if canon(hook) not in ours]
             removed += len(group.get("hooks", [])) - len(kept)
             group["hooks"] = kept
             if kept:
                 groups.append(group)
         if groups:
             hooks[event] = groups
         else:
             del hooks[event]
     if not hooks:
         settings.pop("hooks", None)
     with open(path, "w", encoding="utf-8") as stream:
         json.dump(settings, stream, indent=2, ensure_ascii=False)
     print("removed %d of %d handoffd hooks" % (removed, len(ours)))
     PY
     ```
     Leaving them behind is harmful: once the clone is deleted the stale hook commands fail on every event.
4. `/plugin uninstall handoffd@handoffd`; delete `~/.local/bin/handoffd`; optionally delete the state directory (`<project>/.git/handoffd/`, or `<project>/.claude/handoffd/` outside git) and `.claude/handoffd.json`.
