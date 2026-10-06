"""Entry point for `langgraph dev` and LangGraph Studio.

The server loads this file by path, outside the package, so it imports the
pipeline absolutely. Studio provides its own persistence.
"""

from odoo_miner.pipeline.graph import build_graph

graph = build_graph()
