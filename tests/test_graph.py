"""Tests for mailtrace.graph module."""

from mailtrace.graph import MailGraph


class TestMailGraph:
    """Tests for MailGraph class."""

    def test_init_creates_empty_graph(self):
        """MailGraph initializes with empty graph."""
        graph = MailGraph()
        assert len(graph.graph.nodes()) == 0
        assert len(graph.graph.edges()) == 0

    def test_add_hop_creates_edge(self):
        """add_hop creates an edge between nodes with queue_id label."""
        graph = MailGraph()
        graph.add_hop("host1", "host2", "ABC123")

        assert "host1" in graph.graph.nodes()
        assert "host2" in graph.graph.nodes()
        assert graph.graph.has_edge("host1", "host2")

        edges = list(graph.graph.edges(data=True))
        assert len(edges) == 1
        assert edges[0][2]["label"] == "ABC123"

    def test_add_multiple_hops(self):
        """Can add multiple hops to build a chain."""
        graph = MailGraph()
        graph.add_hop("host1", "host2", "ID1")
        graph.add_hop("host2", "host3", "ID2")
        graph.add_hop("host3", "host4", "ID3")

        assert len(graph.graph.nodes()) == 4
        assert len(graph.graph.edges()) == 3

    def test_add_parallel_edges(self):
        """Can add multiple edges between same nodes (MultiDiGraph)."""
        graph = MailGraph()
        graph.add_hop("host1", "host2", "ID1")
        graph.add_hop("host1", "host2", "ID2")

        edges = list(graph.graph.edges(data=True))
        assert len(edges) == 2

    def test_to_dict_empty_graph(self):
        """to_dict returns correct structure for empty graph."""
        graph = MailGraph()
        result = graph.to_dict()

        assert result["nodes"] == []
        assert result["edges"] == []
        assert result["hop_count"] == 0
        assert "graph_dot" in result

    def test_to_dict_with_hops(self):
        """to_dict returns correct structure with hops."""
        graph = MailGraph()
        graph.add_hop("mx1", "relay1", "MAIL123")
        graph.add_hop("relay1", "final", "MAIL456")

        result = graph.to_dict()

        assert set(result["nodes"]) == {"mx1", "relay1", "final"}
        assert len(result["edges"]) == 2
        assert result["hop_count"] == 2

        edge1 = next(e for e in result["edges"] if e["mail_id"] == "MAIL123")
        assert edge1["from"] == "mx1"
        assert edge1["to"] == "relay1"

    def test_to_dict_contains_dot_format(self):
        """to_dict includes DOT format string."""
        graph = MailGraph()
        graph.add_hop("host1", "host2", "ID1")

        result = graph.to_dict()

        assert "digraph" in result["graph_dot"]
        assert "host1" in result["graph_dot"]
        assert "host2" in result["graph_dot"]

    def test_to_dot_stdout(self, capsys):
        """to_dot with None or '-' writes to stdout."""
        graph = MailGraph()
        graph.add_hop("src", "dst", "QUEUEID")

        graph.to_dot(None)
        captured = capsys.readouterr()
        assert "digraph" in captured.out
        assert "src" in captured.out

        graph.to_dot("-")
        captured = capsys.readouterr()
        assert "digraph" in captured.out

    def test_to_dot_file(self, tmp_path):
        """to_dot with path writes to file."""
        graph = MailGraph()
        graph.add_hop("src", "dst", "QUEUEID")

        output_file = tmp_path / "graph.dot"
        graph.to_dot(str(output_file))

        content = output_file.read_text()
        assert "digraph" in content
        assert "src" in content
        assert "dst" in content

    def test_complex_graph_structure(self):
        """Tests a more complex graph with branches."""
        graph = MailGraph()
        graph.add_hop("ingress", "relay1", "M1")
        graph.add_hop("relay1", "delivery", "M2")
        graph.add_hop("ingress", "relay2", "M3")
        graph.add_hop("relay2", "delivery", "M4")

        result = graph.to_dict()

        assert len(result["nodes"]) == 4
        assert len(result["edges"]) == 4
        assert result["hop_count"] == 4

    def test_self_loop(self):
        """Can add self-loop edge."""
        graph = MailGraph()
        graph.add_hop("host1", "host1", "LOOP123")

        assert graph.graph.has_edge("host1", "host1")
        edges = list(graph.graph.edges(data=True))
        assert len(edges) == 1
