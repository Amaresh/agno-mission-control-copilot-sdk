-- Add mission_type and mission_config columns to tasks table
-- Safe to re-run: uses IF NOT EXISTS / column-existence checks

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'tasks' AND column_name = 'mission_type'
    ) THEN
        ALTER TABLE tasks ADD COLUMN mission_type VARCHAR(30) NOT NULL DEFAULT 'build';
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'tasks' AND column_name = 'mission_config'
    ) THEN
        ALTER TABLE tasks ADD COLUMN mission_config JSONB NOT NULL DEFAULT '{}';
    END IF;
END $$;

-- Backfill: extract repository from description into mission_config
UPDATE tasks
SET mission_config = jsonb_build_object(
    'repository', substring(description FROM 'Repository:\s*(\S+)')
)
WHERE mission_config = '{}'::jsonb
  AND description LIKE '%Repository:%';

-- Mark review-of-PR tasks as 'verify' mission type
UPDATE tasks
SET mission_type = 'verify'
WHERE status = 'REVIEW'
  AND title ILIKE ANY(ARRAY['%review pr#%', '%review batch:%', '%[review pr#%']);
