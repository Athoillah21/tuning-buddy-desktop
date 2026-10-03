"""
Environment-driven settings for the analyzer service.
"""
import os

AI_SERVICE_URL = os.environ.get("AI_SERVICE_URL", "http://localhost:8002").rstrip("/")
# Seconds the analyzer waits for the AI service (it may try several providers)
AI_REQUEST_TIMEOUT = float(os.environ.get("AI_REQUEST_TIMEOUT", "900"))

# Timeout settings for connections to the user's database
DB_CONNECTION_TIMEOUT = int(os.environ.get("DB_CONNECTION_TIMEOUT", "5"))  # seconds
QUERY_EXECUTION_TIMEOUT = int(os.environ.get("QUERY_EXECUTION_TIMEOUT", "300"))  # seconds

# Database explorer: catalog pages, table previews and the read-only SQL console
EXPLORER_TIMEOUT = int(os.environ.get("EXPLORER_TIMEOUT", "15"))  # seconds per statement
EXPLORER_PREVIEW_MAX_ROWS = 500
EXPLORER_CONSOLE_MAX_ROWS = 1000
EXPLORER_MAX_CELL_CHARS = 500  # long values (vectors, documents) are cut for display
# The query tool with "Allow changes" on: a script may build indexes or load data
QUERY_TOOL_WRITE_TIMEOUT = int(os.environ.get("QUERY_TOOL_WRITE_TIMEOUT", "300"))  # seconds

# Test data generator: the most it will add to one table
DATAGEN_MAX_BYTES = int(os.environ.get("DATAGEN_MAX_GB", "100")) * 1024 ** 3

# Every timing is measured this many times and reduced to its median. A single run is
# dominated by cache state, which is how a 0.2ms query can look "80% faster".
MEASUREMENT_REPEATS = int(os.environ.get("MEASUREMENT_REPEATS", "3"))

# Queries already faster than this cannot be meaningfully improved: at this scale the
# difference between two runs is cache and scheduling noise, not optimization.
NOISE_FLOOR_MS = float(os.environ.get("NOISE_FLOOR_MS", "5"))
