import sys

import networkx as nx


class MailGraph:
    """
    Represents a mail flow graph using NetworkX MultiDiGraph.

    Nodes represent mail server hostnames.
    Edges represent mail flow between hosts, labeled with source mail queue IDs.
    Multiple edges between the same host pair indicate multiple mail messages.
    """

    def __init__(self):
        self.graph = nx.MultiDiGraph()

    def add_hop(self, from_host: str, to_host: str, queue_id: str):
        """
        Add a mail hop to the graph.

        Args:
            from_host: Source hostname (without mail ID)
            to_host: Destination hostname (without mail ID)
            queue_id: Mail queue ID at the source host
        """
        self.graph.add_edge(from_host, to_host, label=queue_id)

    def to_dot(self, path: str | None = None):
        """
        Write graph to DOT format.

        Args:
            path: Output file path, or None for stdout
        """
        if path is None or path == "-":
            # Write to stdout
            import io

            buffer = io.StringIO()
            nx.drawing.nx_pydot.write_dot(self.graph, buffer)
            buffer.seek(0)
            sys.stdout.write(buffer.read())
        else:
            nx.drawing.nx_pydot.write_dot(self.graph, path)
