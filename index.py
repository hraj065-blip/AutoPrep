"""Vercel's Flask entry point. Local development can continue using run.py."""

from app import create_app

app = create_app()
