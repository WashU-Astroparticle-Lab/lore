"""
slack_listener.py — Slack event listener for the lab-agent pipeline (shim).

Usage:
    python slack_listener.py

Implementation lives in lab_agent/slack/ (listener, history, sessions, api).
"""
from lab_agent.slack.listener import main

if __name__ == "__main__":
    main()
