"""Heartbeat file helpers shared by long-running services and their healthchecks."""

import os
import time


def beat(path):
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)

    with open(path, 'w') as f:
        f.write(str(int(time.time())))


def check(path, max_age=150):
    if not os.path.exists(path):
        return False

    try:
        with open(path, 'r') as f:
            beat_time = int(f.read().strip())
    except (OSError, ValueError):
        return False

    return (time.time() - beat_time) <= max_age
