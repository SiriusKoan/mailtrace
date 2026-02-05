"""MX record auto-discovery for cluster definitions."""

import logging
import time

import dns.resolver

from mailtrace.config import MXDiscoveryConfig

logger = logging.getLogger(__name__)


class MXDiscovery:
    """Resolves MX records for cluster auto-discovery."""

    def __init__(self, config: MXDiscoveryConfig) -> None:
        self.config = config
        self._cache: dict[str, tuple[list[str], float]] = {}

    def expand_entry(self, entry: str) -> list[str]:
        """Expand a cluster entry, resolving mx: prefixed domains.

        Args:
            entry: A hostname or mx:domain string

        Returns:
            List of hostnames. For static entries, returns [entry].
            For mx: entries, returns resolved MX hostnames.
        """
        if entry.startswith("mx:"):
            domain = entry[3:]
            return self.resolve(domain)
        return [entry]

    def resolve(self, domain: str) -> list[str]:
        """Resolve MX records for a domain.

        Args:
            domain: Domain name to query MX records for

        Returns:
            List of MX hostnames, empty list on failure
        """
        # Check cache if caching enabled
        if self.config.cache_ttl > 0:
            cached = self._cache.get(domain)
            if cached:
                hosts, timestamp = cached
                if time.time() - timestamp < self.config.cache_ttl:
                    return hosts

        # Configure resolver
        resolver = dns.resolver.Resolver()
        if self.config.servers:
            resolver.nameservers = self.config.servers
        resolver.timeout = self.config.timeout
        resolver.lifetime = self.config.timeout

        try:
            answers = resolver.resolve(domain, "MX")
            hosts = [str(rdata.exchange).rstrip(".") for rdata in answers]

            # Cache result if caching enabled
            if self.config.cache_ttl > 0:
                self._cache[domain] = (hosts, time.time())

            return hosts
        except dns.resolver.Timeout:
            logger.warning(
                f"MX lookup failed for {domain}: Timeout after {self.config.timeout}s"
            )
            return []
        except dns.resolver.NXDOMAIN:
            logger.warning(f"MX lookup failed for {domain}: NXDOMAIN")
            return []
        except dns.resolver.NoAnswer:
            logger.warning(f"No MX records found for {domain}")
            return []
        except Exception as e:
            logger.warning(f"MX lookup failed for {domain}: {e}")
            return []
