"""Tick-driven schedule evaluation and durable dispatch (AD-5, AD-13)."""

from .role import run_dispatcher
from .schedule import matches_tick
from .worker import Dispatcher, command_for

__all__ = ["Dispatcher", "command_for", "matches_tick", "run_dispatcher"]
