"""migui: the web UI of 'migrate' (DESIGN.md, "UI"). Two processes, standard library only.

migui.runner  root, unix socket /run/migui/runner.sock: the only process that starts 'migrate'.
migui.web     user migui, HTTP on 127.0.0.1:8787: login, CSRF, two-step confirmation, audit, static pages.
"""
__version__ = "1.0.1"
