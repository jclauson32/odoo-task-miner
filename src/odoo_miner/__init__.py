"""odoo-miner: record, replay and analyze Odoo workflows.

Replays a Chrome Recorder session against Odoo to capture the backend call
behind every click, then runs a pipeline of agents that finds the friction,
explains it against Odoo's source, and plans a change for a person to approve.
"""

__version__ = "0.1.0"
