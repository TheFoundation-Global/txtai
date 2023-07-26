"""
Search module
"""

import logging

# Logging configuration
logger = logging.getLogger(__name__)


class Search:
    """
    Executes a batch search action. A search can be both index and/or database driven.
    """

    def __init__(self, embeddings, indexids=False):
        """
        Creates a new search action.

        Args:
            embeddings: embeddings instance
            indexids: searches return indexids when True, otherwise run standard search
        """

        self.embeddings = embeddings
        self.indexids = indexids

        # Alias embeddings attributes
        self.ann = embeddings.ann
        self.batchtransform = embeddings.batchtransform
        self.config = embeddings.config
        self.database = embeddings.database
        self.query = embeddings.query
        self.scoring = embeddings.scoring if embeddings.issparse() else None

    def __call__(self, queries, limit=None, weights=None):
        """
        Executes a batch search for queries. This method will run either an index search or an index + database search
        depending on if a database is available.

        Args:
            queries: list of queries
            limit: maximum results
            weights: hybrid score weights

        Returns:
            list of (id, score) per query for index search, list of dict per query for an index+database search
        """

        # Default input parameters
        limit = limit if limit else 3
        weights = weights if weights is not None else 0.5

        # Return empty results when indexes are not available
        if not self.ann and not self.scoring:
            return [[]] * len(queries)

        if not self.indexids and self.database:
            return self.dbsearch(queries, limit, weights)

        # Default is vector index query (sparse, dense or hybrid)
        return self.search(queries, limit, weights)

    def search(self, queries, limit, weights):
        """
        Executes an index search. When only a sparse index is enabled, this is a a keyword search. When only
        a dense index is enabled, this is an ann search. When both are enabled, this is a hybrid search.

        Args:
            queries: list of queries
            limit: maximum results
            weights: hybrid score weights

        Returns:
            list of (id, score) per query
        """

        dense = self.dense(queries, limit) if self.ann else None
        sparse = self.sparse(queries, limit) if self.scoring else None

        # Combine scores together
        if dense and sparse:
            # Create weights array if single number passed
            if isinstance(weights, (int, float)):
                weights = [weights, 1 - weights]

            # Create weighted scores
            results = []
            for vectors in zip(dense, sparse):
                uids = {}
                for v, scores in enumerate(vectors):
                    for r, (uid, score) in enumerate(scores if weights[v] > 0 else []):
                        # Initialize score
                        if uid not in uids:
                            uids[uid] = 0.0

                        # Create hybrid score
                        #  - Convex Combination when sparse scores are normalized
                        #  - Reciprocal Rank Fusion (RRF) when sparse scores aren't normalized
                        if self.scoring.isnormalized():
                            uids[uid] += score * weights[v]
                        else:
                            uids[uid] += (1.0 / (r + 1)) * weights[v]

                results.append(sorted(uids.items(), key=lambda x: x[1], reverse=True)[:limit])

            return results

        # Return single query results
        return dense if dense else sparse

    def dense(self, queries, limit):
        """
        Executes an dense vector search with an approximate nearest neighbor index.

        Args:
            queries: list of queries
            limit: maximum results

        Returns:
            list of (id, score) per query
        """

        # Convert queries to embedding vectors
        embeddings = self.batchtransform((None, query, None) for query in queries)

        # Search approximate nearest neighbor index
        results = self.ann.search(embeddings, limit)

        # Require scores to be greater than 0
        results = [[(i, score) for i, score in r if score > 0] for r in results]

        return self.resolve(results)

    def sparse(self, queries, limit):
        """
        Executes a sparse vector search with a term frequency sparse array.

        Args:
            queries: list of queries
            limit: maximum results

        Returns:
            list of (id, score) per query
        """

        # Search term frequency sparse index
        return self.resolve(self.scoring.batchsearch(queries, limit))

    def resolve(self, results):
        """
        Resolves index ids. This is only executed when content is disabled.

        Args:
            results: results

        Returns:
            results with resolved ids
        """

        # Map indexids to ids if "ids" available
        if not self.indexids and "ids" in self.config:
            lookup = self.config["ids"]
            return [[(lookup[i], score) for i, score in r] for r in results]

        return results

    def dbsearch(self, queries, limit, weights):
        """
        Executes an index + database search.

        Args:
            queries: list of queries
            limit: maximum results
            weights: hybrid score weights

        Returns:
            list of dict per query
        """

        # Parse queries
        queries = self.parse(queries)

        # Override limit with query limit, if applicable
        limit = max(limit, self.limit(queries))

        # Extract embeddings queries as single batch across all queries
        equeries, candidates = self.extract(queries, limit)

        # Bulk index search
        search = self.search([query for _, query in equeries], candidates, weights) if equeries else []

        # Combine index search results with database search results
        results = []
        for x, query in enumerate(queries):
            # Get search indices for this query within bulk query
            indices = [i for i, (y, _) in enumerate(equeries) if x == y]

            # Run the database query
            result = self.database.search(query, [s for i, s in enumerate(search) if i in indices], limit)
            results.append(result)

        return results

    def parse(self, queries):
        """
        Parses a list of database queries.

        Args:
            queries: list of queries

        Returns:
            parsed queries
        """

        # Parsed queries
        parsed = []

        for query in queries:
            # Parse query
            parse = self.database.parse(query)

            # Transform query if SQL not parsed and reparse
            if self.query and "select" not in parse:
                # Generate query
                query = self.query(query)
                logger.debug(query)

                # Reparse query
                parse = self.database.parse(query)

            parsed.append(parse)

        return parsed

    def limit(self, queries):
        """
        Parses the largest LIMIT clause from queries.

        Args:
            queries: list of queries

        Returns:
            largest limit number or 0 if not found
        """

        # Override limit with largest limit from database queries
        qlimit = 0
        for query in queries:
            # Parse out qlimit
            l = query.get("limit")
            if l and l.isdigit():
                l = int(l)

            qlimit = l if l and l > qlimit else qlimit

        return qlimit

    def extract(self, queries, limit):
        """
        Extract embeddings queries text and number of candidates from a list of parsed queries.

        The number of candidates are the number of results to bring back from index queries. This is an optional
        second argument to similar() clauses. For a single query filter clause, the default is the query limit.
        With multiple filtering clauses, the default is 10x the query limit. This ensures that limit results
        are still returned with additional filtering after an index query.

        Args:
            queries: list of parsed queries
            limit: maximum results

        Returns:
            (list of embeddings queries, number of candidates)
        """

        # Extract embeddings queries as single batch across all queries
        equeries, candidates = [], 0
        for x, query in enumerate(queries):
            if "similar" in query:
                # Get handle to similar queries
                for params in query["similar"]:
                    # Store the query index and similar query argument (first argument)
                    equeries.append((x, params[0]))

                    # Second argument is similar candidate limit
                    if len(params) > 1 and params[1].isdigit():
                        # Get largest candidate limit across all queries
                        candidates = int(params[1]) if int(params[1]) > candidates else candidates

        # Default number of candidates, if not specified
        if not candidates:
            multitoken = any(query.get("where") and len(query["where"].split()) > 1 for query in queries)
            candidates = limit * 10 if multitoken else limit

        return (equeries, candidates)
