"""
slack_listener.py — Slack event listener for the lab-agent pipeline (shim).

Usage:
    "<PYTHON from lab_config.md>" slack_listener.py   (it refuses any other interpreter)

Implementation lives in lab_agent/slack/ (listener, history, sessions, api).
"""
from lab_agent.slack.listener import main

if __name__ == "__main__":
    main()
