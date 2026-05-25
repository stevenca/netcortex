"""Decoupled ingest pipeline.

Adapters push discovered GraphData payloads onto a Redis Stream.  One or more
ingest workers consume the stream and write to Neo4j.  This decouples
discovery latency from ingest latency so a slow Neo4j write does not back up
the entire discovery loop.

Two modes are supported, selected by the INGEST_MODE env var:

  direct (default):
    Adapters call ingest_graph_data() inline.  Same behaviour as before.

  stream:
    Adapters call publish_graph_data() which enqueues onto Redis.  Ingest
    workers (run via `python -m netcortex.ingest.worker`) consume the
    stream and write to Neo4j.

The producer falls back to direct ingest if Redis is unreachable, so the
system stays online during Redis outages.
"""

from netcortex.ingest.queue import publish_graph_data, consume_graph_data
from netcortex.ingest.hash import node_hash, edge_hash

__all__ = ["publish_graph_data", "consume_graph_data", "node_hash", "edge_hash"]
