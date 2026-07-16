"""
get_la_cookies.py — refresh the LabArchives session cookie via WashU SSO (shim).

Usage:
    python get_la_cookies.py

Implementation lives in lab_agent/cli/cookies.py.
"""
from lab_agent.cli.cookies import main

if __name__ == "__main__":
    main()
