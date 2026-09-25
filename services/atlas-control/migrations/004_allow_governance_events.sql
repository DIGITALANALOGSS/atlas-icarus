ALTER TABLE events
  DROP CONSTRAINT IF EXISTS events_event_type_check;

ALTER TABLE events
  ADD CONSTRAINT events_event_type_check
  CHECK (
    event_type ~ '^(research|governance)\.[a-z0-9_]+(\.[a-z0-9_]+)*$'
  );
