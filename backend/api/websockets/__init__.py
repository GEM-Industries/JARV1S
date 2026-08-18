"""WebSocket package for real-time communication.

Kept as a thin namespace. Importers should reach into the submodules they need
(`.routes` for the FastAPI router, `.types` for WSMessageType, etc.) so that
loading a leaf module (e.g. types) does not eagerly pull in the full handler
graph — that coupling caused a circular import between `delivery.py` and the
orchestrator-via-handlers chain.
"""
