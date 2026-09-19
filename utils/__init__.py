"""Shared utilities for the travel-agent tools.

Keeping this directory as an explicit package prevents ms-swift's
``swift/cli/utils.py`` from shadowing imports such as ``utils.markdown`` when
tools are loaded inside the rollout server.
"""
