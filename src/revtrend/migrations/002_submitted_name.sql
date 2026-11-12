-- Display name of the submitter at ingest time; submitted_by stays the stable key.
ALTER TABLE measurement ADD COLUMN submitted_name TEXT;
