CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS repos (
    id           SERIAL PRIMARY KEY,
    url          TEXT UNIQUE NOT NULL,
    name         TEXT NOT NULL,
    commit_sha   TEXT,
    status       TEXT NOT NULL DEFAULT 'pending',
    error        TEXT,
    file_count   INT NOT NULL DEFAULT 0,
    chunk_count  INT NOT NULL DEFAULT 0,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    indexed_at   TIMESTAMPTZ
);

-- Full file contents live in the DB so the API container stays stateless.
CREATE TABLE IF NOT EXISTS files (
    repo_id     INT NOT NULL REFERENCES repos(id) ON DELETE CASCADE,
    path        TEXT NOT NULL,
    content     TEXT NOT NULL,
    line_count  INT NOT NULL,
    PRIMARY KEY (repo_id, path)
);

CREATE TABLE IF NOT EXISTS chunks (
    id           BIGSERIAL PRIMARY KEY,
    repo_id      INT NOT NULL REFERENCES repos(id) ON DELETE CASCADE,
    path         TEXT NOT NULL,
    symbol       TEXT NOT NULL,
    kind         TEXT NOT NULL,
    start_line   INT NOT NULL,
    end_line     INT NOT NULL,
    content      TEXT NOT NULL,
    search_text  TEXT NOT NULL,
    -- Symbol names weigh most, then paths, then body text: a chunk that *defines*
    -- `total_with_tax` should outrank one that merely calls it.
    tsv          TSVECTOR GENERATED ALWAYS AS (
                     setweight(to_tsvector('simple', replace(symbol, '.', ' ')), 'A') ||
                     setweight(to_tsvector('simple', regexp_replace(path, '[/._-]+', ' ', 'g')), 'B') ||
                     to_tsvector('simple', search_text)
                 ) STORED,
    embedding    VECTOR({dim}) NOT NULL
);

CREATE INDEX IF NOT EXISTS chunks_repo_idx ON chunks (repo_id);
CREATE INDEX IF NOT EXISTS chunks_tsv_idx ON chunks USING GIN (tsv);
CREATE INDEX IF NOT EXISTS chunks_embedding_idx ON chunks USING hnsw (embedding vector_cosine_ops);

-- One row per question: the observability trail (steps, tokens, cost, latency).
CREATE TABLE IF NOT EXISTS query_logs (
    id             BIGSERIAL PRIMARY KEY,
    repo_id        INT REFERENCES repos(id) ON DELETE SET NULL,
    question       TEXT NOT NULL,
    mode           TEXT NOT NULL,
    steps          JSONB NOT NULL DEFAULT '[]',
    input_tokens   INT NOT NULL DEFAULT 0,
    output_tokens  INT NOT NULL DEFAULT 0,
    cost_usd       NUMERIC(10, 5) NOT NULL DEFAULT 0,
    latency_ms     INT NOT NULL DEFAULT 0,
    error          TEXT,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Added with multi-provider support; idempotent for databases created earlier.
ALTER TABLE query_logs ADD COLUMN IF NOT EXISTS provider TEXT;
ALTER TABLE query_logs ADD COLUMN IF NOT EXISTS model TEXT;
ALTER TABLE query_logs ADD COLUMN IF NOT EXISTS cache_read_tokens INT NOT NULL DEFAULT 0;
ALTER TABLE query_logs ADD COLUMN IF NOT EXISTS cache_write_tokens INT NOT NULL DEFAULT 0;

-- Embedding progress, for the progress bar while a repo is being indexed.
ALTER TABLE repos ADD COLUMN IF NOT EXISTS embedded_count INT NOT NULL DEFAULT 0;
ALTER TABLE repos ADD COLUMN IF NOT EXISTS embedding_started_at TIMESTAMPTZ;
