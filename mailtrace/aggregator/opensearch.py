"""OpenSearch log aggregator for the mailtrace application.

This module provides functionality to query and retrieve log entries
from OpenSearch/Elasticsearch indices for email tracing.
"""

import copy
from datetime import datetime

import urllib3
from opensearchpy import OpenSearch as OpenSearchClient
from opensearchpy.exceptions import AuthenticationException
from opensearchpy.exceptions import ConnectionError as OSConnectionError
from opensearchpy.exceptions import TransportError

from mailtrace.aggregator.base import LogAggregator
from mailtrace.config import Config
from mailtrace.exceptions import (
    OpenSearchConnectionError,
    OpenSearchQueryError,
)
from mailtrace.log import logger
from mailtrace.models import LogEntry, LogQuery
from mailtrace.parser import OpensearchParser
from mailtrace.utils import time_range_to_timedelta

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

    _query = {
        "query": {
            "bool": {
                "must": [
                    {"match": {"log.syslog.facility.name": "mail"}},
                ]
            }
        },
        "size": 1000,
    }

    def __init__(self, host: str, config: Config):
        """
        Initialize the OpenSearch log aggregator.

        Args:
            host (str): The hostname to filter logs for.
            config (Config): Configuration object.
        """

        self.host = host
        self.config = config.opensearch_config

        try:
            logger.debug(
                f"Connecting to OpenSearch at {self.config.host}:{self.config.port}"
            )
            self.client = OpenSearchClient(
                hosts=[{"host": self.config.host, "port": self.config.port}],
                http_auth=(self.config.username, self.config.password),
                use_ssl=self.config.use_ssl,
                verify_certs=self.config.verify_certs,
            )
            # Test the connection
            self.client.info()
            logger.info(
                f"Successfully connected to OpenSearch at {self.config.host}"
            )
        except AuthenticationException as e:
            raise OpenSearchConnectionError(
                f"Authentication failed for OpenSearch at {self.config.host}",
                "Check your OpenSearch username and password in the configuration",
            ) from e
        except OSConnectionError as e:
            raise OpenSearchConnectionError(
                f"Cannot connect to OpenSearch at {self.config.host}:{self.config.port}",
                "Verify the host and port are correct and OpenSearch is running",
            ) from e
        except Exception as e:
            raise OpenSearchConnectionError(
                f"Failed to initialize OpenSearch connection: {e}",
                "Check your OpenSearch configuration settings",
            ) from e

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

        opensearch_query = copy.deepcopy(self._query)
        opensearch_query["query"]["bool"]["must"].append(
            {"match": {"host.name": self.host}}
        )
        if query.time and query.time_range:
            time = datetime.fromisoformat(query.time.replace("Z", "+00:00"))
            time_range = time_range_to_timedelta(query.time_range)
            start_time = (time - time_range).strftime("%Y-%m-%dT%H:%M:%S")
            end_time = (time + time_range).strftime("%Y-%m-%dT%H:%M:%S")
            opensearch_query["query"]["bool"]["must"].append(
                {
                    "range": {
                        "@timestamp": {
                            "gte": start_time,
                            "lte": end_time,
                            "time_zone": self.config.time_zone,
                        }
                    }
                }
            )
        if query.keywords:
            for keyword in query.keywords:
                opensearch_query["query"]["bool"]["must"].append(
                    {"wildcard": {"message": f"*{keyword.lower()}*"}}
                )
        if query.mail_id:
            opensearch_query["query"]["bool"]["must"].append(
                {"wildcard": {"message": f"{query.mail_id.lower()}*"}}
            )

        logger.debug(f"OpenSearch query: {opensearch_query}")

        try:
            search_results = self.client.search(
                index=self.config.index,
                body=opensearch_query,
            )
        except OSConnectionError as e:
            raise OpenSearchConnectionError(
                f"Lost connection to OpenSearch during query: {e}",
                "Check network connectivity to OpenSearch",
            ) from e
        except TransportError as e:
            if e.status_code == 404:
                raise OpenSearchQueryError(
                    f"Index not found: {self.config.index}",
                    "Check that the index name in your configuration is correct",
                ) from e
            elif e.status_code == 403:
                raise OpenSearchQueryError(
                    f"Access denied to index: {self.config.index}",
                    "Check that your OpenSearch user has permission to access this index",
                ) from e
            else:
                raise OpenSearchQueryError(
                    f"OpenSearch query failed with status {e.status_code}: {e}",
                    "Check the OpenSearch server logs for more details",
                ) from e
        except Exception as e:
            logger.debug(f"OpenSearch query error details: {e}", exc_info=True)
            raise OpenSearchQueryError(
                f"Failed to execute OpenSearch query: {e}",
                "Check your query parameters and OpenSearch configuration",
            ) from e

        try:
            parsed_entries = [
                OpensearchParser().parse(hit)
                for hit in search_results["hits"]["hits"]
            ]
            logger.info(
                f"Retrieved {len(parsed_entries)} log entries from OpenSearch"
            )
            return parsed_entries
        except (KeyError, ValueError) as e:
            raise OpenSearchQueryError(
                f"Failed to parse OpenSearch results: {e}",
                "The log format from OpenSearch may not match the expected structure",
            ) from e
