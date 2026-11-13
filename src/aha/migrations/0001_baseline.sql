-- Baseline: the schema_migrations table is created by the runner itself, so this migration has
-- nothing to add yet. It exists so that every database carries a version from the start, and so
-- that the first real migration is 0002. Real migrations follow expand/contract: add, never drop or
-- rename in the same release as the code that stops using the old shape (see docs/migrations.md).
SELECT 1;
