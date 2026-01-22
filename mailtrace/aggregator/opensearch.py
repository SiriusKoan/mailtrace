import logging
from datetime import datetime

import urllib3
from opensearchpy import OpenSearch as OpenSearchClient
from opensearchpy.helpers.search import Search

from mailtrace.aggregator.base import LogAggregator
from mailtrace.config import Config, OpenSearchConfig
from mailtrace.models import LogEntry, LogQuery
from mailtrace.parser import OpensearchParser
from mailtrace.utils import get_hosts, time_range_to_timedelta

logger = logging.getLogger("mailtrace")

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


class OpenSearch(LogAggregator):
    """
    OpenSearch log aggregator for querying mail system logs.

    This class provides functionality to search and retrieve mail-related log entries
    from an OpenSearch cluster. It constructs queries based on various criteria such as
    time ranges, keywords, and mail IDs.

    Attributes:
        _query (dict): Base query template for OpenSearch requests.
    """

    def __init__(self, host: str, config: Config):
        """
        Initialize the OpenSearch log aggregator.

        Args:
            host (str): The hostname or cluster name to filter logs for.
            config (Config): Configuration object.
        """

        self.host = host
        self.config: OpenSearchConfig = config.opensearch_config
        self.hosts = get_hosts(
            config.cluster_to_hosts(host) or [host], config.domain
        )
        self.client = OpenSearchClient(
            hosts=[{"host": self.config.host, "port": self.config.port}],
            http_auth=(self.config.username, self.config.password),
            use_ssl=self.config.use_ssl,
            verify_certs=self.config.verify_certs,
            timeout=self.config.timeout,
        )

    def query_by(self, query: LogQuery) -> list[LogEntry]:
        """
        Query OpenSearch for log entries matching the specified criteria.

        Builds an OpenSearch query based on the provided LogQuery parameters and
        executes it against the configured index. The query filters for mail facility
        logs from the specified host and applies additional filters for time range,
        keywords, and mail IDs as specified.

        Args:
            query (LogQuery): Query parameters including time range, keywords, and mail ID.

        Returns:
            list[LogEntry]: List of parsed log entries matching the query criteria.
        """

        search = Search(using=self.client, index=self.config.index)
        search = search.extra(size=1000)

        facility_field = self.config.mapping.facility
        if facility_field:
            search = search.query("match", **{facility_field: "mail"})

        search = search.query(
            "terms", **{self.config.mapping.hostname: self.hosts}
        )

        if query.time and query.time_range:
            time = datetime.fromisoformat(query.time.replace("Z", "+00:00"))
            time_range = time_range_to_timedelta(query.time_range)
            start_time = (time - time_range).strftime("%Y-%m-%dT%H:%M:%S")
            end_time = (time + time_range).strftime("%Y-%m-%dT%H:%M:%S")
            search = search.filter(
                "range",
                **{
                    self.config.mapping.timestamp: {
                        "gte": start_time,
                        "lte": end_time,
                        "time_zone": self.config.time_zone,
                    }
                },
            )

        if query.keywords:
            for keyword in query.keywords:
                search = search.query(
                    "wildcard",
                    **{self.config.mapping.message: f"*{keyword.lower()}*"},
                )

        if query.mail_id:
            # Use structured queueid field if available, otherwise fallback to message search
            queueid_field = self.config.mapping.queueid
            if queueid_field:
                search = search.query("term", **{queueid_field: query.mail_id})
            else:
                search = search.query(
                    "wildcard",
                    **{
                        self.config.mapping.message: f"{query.mail_id.lower()}*"
                    },
                )

        logger.debug(f"Query: {search.to_dict()}")
        response = search.execute()
        logger.debug(
            f"Opensearch Response:\n{[hit.to_dict() for hit in response]}"
        )

        parser = OpensearchParser(mapping=self.config.mapping)
        parsed_log_entries = [parser.parse_with_enrichment(hit.to_dict()) for hit in response]
        logger.debug(
            f"Found {len(parsed_log_entries)} log entries.\n{parsed_log_entries}"
        )

        return parsed_log_entries
