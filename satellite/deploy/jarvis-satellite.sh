#!/bin/sh
export PATH="$HOME/.local/bin:$PATH"
exec uv run --directory "$HOME/.jarvis-satellite/app" python -m jarvis_satellite "$@"
