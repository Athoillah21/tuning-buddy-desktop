# Demo database for the benchmark suite: PostgreSQL 16 with pgvector (from the base image)
# and PostGIS, so spatial and vector cases can be exercised alongside the relational ones.
FROM pgvector/pgvector:pg16

RUN apt-get update \
    && apt-get install -y --no-install-recommends postgresql-16-postgis-3 postgresql-16-postgis-3-scripts \
    && rm -rf /var/lib/apt/lists/*
