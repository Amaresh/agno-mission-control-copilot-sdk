-- Mission Control Database Initialization
-- This script runs on first PostgreSQL startup

-- Enable pgvector extension
CREATE EXTENSION IF NOT EXISTS vector;

-- Enable UUID extension
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- Grant permissions
GRANT ALL PRIVILEGES ON DATABASE mission_control TO postgres;

-- Log initialization
DO $$
BEGIN
    RAISE NOTICE 'Mission Control database initialized with pgvector support';
END $$;
