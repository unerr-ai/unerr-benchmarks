# ─────────────────────────────────────────────────────────────────────────────
# unerr — in-agent demo (VHS tape)
#
# Records a REAL Claude Code session in THIS repo. A user types a natural,
# slightly under-specified refactor prompt; the agent has to figure out what
# "trace functionality" means, explore extractFilePath, and attempt a signature
# change. unerr's cascade guard fires INLINE before the Edit runs (extractFilePath
# has 12 callers); the agent then updates every caller; the turn closes with
# unerr's end-of-turn receipt. This is unerr working *inside* the coding agent —
# not the external `unerr` CLI.
#
# Render (MP4 only — no GIF):
#   vhs demo/unerr-cascade-demo.tape      →  demo/unerr-cascade.mp4
#
# ── PREREQUISITES (read before recording) ───────────────────────────────────
#   1. REBUILD so the running binary has the 60s drift-write timeout that rides
#      out RocksDB compaction stalls. The shipped binary that logs
#      "CozoDB write timeout after 10000ms" is stale; while a stall is active the
#      cascade query falls back to the static nudge and NO ⚡ line appears.
#         pnpm run build   then restart your IDE / Claude Code session.
#   2. WARM THE GRAPH: open Claude Code in this repo once and let it idle ~10s so
#      the per-repo proxy is up and the graph is loaded. `unerr status` must NOT
#      say "Proxy Not running". Record when the graph is warm AND idle (no active
#      "Removing N orphaned entities" compaction).
#   3. ZERO-PROMPT SETUP (so no approval dialog stalls the recording):
#        a. MCP-trust + folder-trust are already handled — this repo's
#           .claude/settings.local.json sets  enableAllProjectMcpServers: true
#           and enabledMcpjsonServers: ["unerr"], and the repo is git-trusted.
#           So neither the "trust this folder" nor the "approve MCP servers"
#           dialog will appear.
#        b. Per-tool prompts are suppressed by launching with
#           `claude --dangerously-skip-permissions` (done below, inside Hide so
#           the flag is NEVER recorded). This is transient — it does NOT weaken
#           your normal-session permissions the way a persistent
#           settings.json "defaultMode": "bypassPermissions" would.
#        c. The bypass-mode warning (Yes/No) IS shown on launch — "No, exit" is
#           the default. The tape clears it automatically inside Hide via
#           Down → Enter (selects "Yes, I accept"), so no manual pre-accept is
#           needed and the dialog is never recorded.
#   4. THIS MAKES REAL EDITS to src/hooks/navigation-hooks.ts (and its callers).
#      After recording:   git checkout -- src/hooks/navigation-hooks.ts
#      (the agent may touch caller files too — `git status` then revert all).
#   5. It is a LIVE agent: output and exact file changes vary per run. The ⚡
#      cascade line is deterministic (hook-driven). The end-of-turn receipt
#      depends on the agent following its close-out contract (it almost always
#      does, but it is not hook-guaranteed).
#
# ── TUNE ─────────────────────────────────────────────────────────────────────
#   The tape is sized to run ≳10 min so the live turn is never cut off. The
#   Sleep lines marked  # ⟵ TUNE  must outlast the agent's real turn on your
#   machine. If the agent finishes well before the window ends, the tail is
#   harmless idle at a ready prompt — trim it in post, or shorten the bulk Sleep.
#   If VHS sends Ctrl+C before the ⚡ line or the receipt appears, increase it.
#   Do ONE dry run and adjust.
# ─────────────────────────────────────────────────────────────────────────────

Output demo/unerr-cascade.mp4

Require claude

Set Shell zsh
Set Theme "Dracula"
Set FontSize 18
Set Width 1400
Set Height 900
Set Padding 24
Set BorderRadius 10
Set WindowBar Colorful
Set TypingSpeed 55ms

# Start clean, in the repo, and launch the agent WITHOUT recording the setup.
# The launch carries two hidden flags (never recorded — they run inside Hide):
#   --dangerously-skip-permissions : suppresses every per-tool approval dialog.
#   --append-system-prompt "..."   : forces the agent to run the turn
#       autonomously — no clarifying questions, no option menus, no plan-mode
#       approval, no "agree on the shape first" pause. WITHOUT this, the
#       deliberately ambiguous prompt below makes the agent STOP and ask the
#       user which interpretation to take, which hangs an unattended recording.
Hide
Type "cd /Users/jaswanth/IdeaProjects/unerr-cli"
Enter
Type "clear"
Enter
Type 'claude --dangerously-skip-permissions --append-system-prompt "You are being recorded in an automated demo with no human available to answer. Execute the task end to end autonomously. Never ask clarifying questions. Never present options for the user to choose. Never call AskUserQuestion. Never stop to confirm or seek approval. Never enter plan mode. Pick the most reasonable interpretation and implement it directly, including updating every caller."'
Enter
Sleep 2s          # bypass-mode warning (Yes/No) appears; "No, exit" is default
Down              # move selection to "Yes, I accept"
Enter             # accept the bypass-mode warning
Sleep 8s          # wait for the TUI to boot to a ready prompt (hidden)
Show

Sleep 2s

# A real user prompt: deliberately under-specified. "trace functionality" could
# mean a flag, a parameter, or logging — the agent has to decide, explore
# extractFilePath, and attempt the change. That exploration is what surfaces
# unerr's tools, and the signature edit is what trips the cascade guard.
Type "Add an optional trace functionality to the file extraction of navigation hooks"
Sleep 1500ms
Enter

# The agent reads the file and attempts the signature edit. BEFORE the Edit tool
# runs, unerr's pre-edit hook injects the cascade guard inline, e.g.:
#
#   ⚡ unerr · cascade guard: editing src/hooks/navigation-hooks.ts changes a
#   signature with callers that must be updated in the same change —
#   extractFilePath (parameter_added): 12 caller(s) at risk across 4 file(s)
#   (12 source, 0 test). Call get_references({key:'extractFilePath',
#   direction:'callers'}) and update every caller before finishing.
#
# The agent then makes the real edit and accounts for every caller. With the
# autonomous system-prompt above, NO question/option dialog appears — so the
# tape just WAITS. Do NOT add Enter presses here: a blind Enter would pick an
# option, submit an empty message, or interrupt the turn.
Sleep 60s         # ⟵ TUNE  explore → ⚡ cascade guard fires
Sleep 900s        # ⟵ TUNE  real edit + 12-caller pass + typecheck (bulk of turn)

# Let the closing message and unerr's end-of-turn receipt land, e.g.:
#   unerr » this turn: 1 catch · ≈ 4.2k tokens saved · +5 turns of headroom
Sleep 45s         # ⟵ TUNE

# Stop any in-flight work for a clean final frame, then end the recording.
Ctrl+C
Sleep 600ms
Ctrl+C
Sleep 1500ms
