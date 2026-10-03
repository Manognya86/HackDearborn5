-- Push notifications for the live dashboard (consumed by lifelog/realtime.py via LISTEN lifelog).

CREATE OR REPLACE FUNCTION notify_readings() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    PERFORM pg_notify('lifelog', '{"type":"readings"}');
    RETURN NULL;
END $$;

DROP TRIGGER IF EXISTS readings_notify ON readings;
CREATE TRIGGER readings_notify AFTER INSERT ON readings
    FOR EACH STATEMENT EXECUTE FUNCTION notify_readings();

CREATE OR REPLACE FUNCTION notify_alert() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'UPDATE' AND NEW.message IS NOT DISTINCT FROM OLD.message
       AND NEW.resolved_at IS NOT DISTINCT FROM OLD.resolved_at THEN
        RETURN NULL;
    END IF;
    PERFORM pg_notify('lifelog', json_build_object(
        'type', 'alert', 'id', NEW.id, 'item_id', NEW.item_id, 'kind', NEW.kind,
        'severity', NEW.severity, 'message', NEW.message, 'resolved', NEW.resolved_at IS NOT NULL)::text);
    RETURN NULL;
END $$;

DROP TRIGGER IF EXISTS alerts_notify ON alerts;
CREATE TRIGGER alerts_notify AFTER INSERT OR UPDATE ON alerts
    FOR EACH ROW EXECUTE FUNCTION notify_alert();
