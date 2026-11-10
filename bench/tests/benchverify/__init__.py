"""Verifier framework of dev-agent-bench (shared by all bench steps).

Runs as root inside the task container. Everything that executes or parses
agent-written code is started as the unprivileged `agent` user (see proc.py).
"""
