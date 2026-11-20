"""Glitch planner for the CMP 170HX goldilocks bench: ledger, log parser, never-rules and next-shot planner.

Nothing in this package talks to the bench, the Pico or a card. It reads runner logs and a JSONL ledger and prints
commands for an operator (or hunt.py) to run.
"""
__version__ = "0.1.0"
