"""Live monitoring for locally-spawned Claude agents.

A resident server (server.py) spawns `claude` runs and parses their
stream-json output (events.py) into progress records, which both a web
dashboard and a terminal TUI (tui.py) render in real time.
"""
