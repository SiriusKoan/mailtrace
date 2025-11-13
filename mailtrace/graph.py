import networkx as nx


class MailGraph:
    def __init__(self):
        self.graph = nx.DiGraph()

    def add_hop(self, from_node: str, to_node: str, label: str):
        self.graph.add_edge(from_node, to_node, label=label)

    def to_dot(self, path: str):
        nx.drawing.nx_pydot.write_dot(self.graph, path)
