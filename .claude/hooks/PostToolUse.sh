#!/bin/bash
# unerr PostToolUse hook — graph-aware output compression
# Installed by: unerr init
# Removes: unerr uninstall

# Only compress large outputs (>2KB)
if [ ${#TOOL_OUTPUT} -gt 2048 ]; then
  echo "$TOOL_OUTPUT" | unerr compress-output
else
  echo "$TOOL_OUTPUT"
fi
