import copy
from datetime import datetime

import urllib3
from opensearchpy import OpenSearch

from mailtrace.parser import OpensearchParser

from ..config import Config
from ..log import logger
from ..models import LogEntry, LogQuery
from ..utils import time_range_to_timedelta
from .base import LogAggregator

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


class Opensearch(LogAggregator):
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
        self.host = host
        self.config = config.opensearch_config
        self.client = OpenSearch(
            hosts=[{"host": self.config.host, "port": self.config.port}],
            http_auth=(self.config.username, self.config.password),
            use_ssl=self.config.use_ssl,
            verify_certs=self.config.verify_certs,
        )

    def query_by(self, query: LogQuery) -> list[LogEntry]:
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
        logger.debug(f"Query: {opensearch_query}")
        search_results = self.client.search(
            index=self.config.index,
            body=opensearch_query,
        )
        return [
            OpensearchParser().parse(hit)
            for hit in search_results["hits"]["hits"]
        ]
