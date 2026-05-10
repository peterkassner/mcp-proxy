#!/bin/zsh
# Thin wrapper — implementation is getStatus.py (avoids shell+Python stdin mixups).
exec /usr/bin/env python3 "${0:A:h}/getStatus.py" "$@"
