"""Entry point for `langgraph dev` / LangGraph Studio.

The LangGraph server loads the file named in langgraph.json by path, outside
its package, so that file cannot use relative imports. This one imports the
pipeline absolutely; Studio supplies its own persistence.
"""

from odoo_miner.pipeline.graph import build_graph

graph = build_graph()
