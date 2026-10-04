-- Privacy enforced by the database itself (Postgres row-level security).
-- Signed-in API requests run as the restricted role lifelog_app with lifelog.user_id set for the transaction
-- (see lifelog/db.py). Policies then hide every other person's medicines and everything attached to them,
-- even if an application query forgets a WHERE clause. Background jobs, seeding and the explicitly
-- community features (outage rescue, porch heat) run as the table owner, which policies don't restrict.

-- Hosted databases may not allow CREATE ROLE: then the policies are still created, the app detects that it can't
-- switch role (lifelog/db.py rls_available) and falls back to filtering in its queries; /api/health reports which.
DO $$ BEGIN
    CREATE ROLE lifelog_app NOLOGIN;
EXCEPTION WHEN duplicate_object THEN NULL;
          WHEN insufficient_privilege THEN RAISE NOTICE 'cannot create role lifelog_app; row-level security not enforced';
END $$;

DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'lifelog_app') THEN RETURN; END IF;
    -- the connecting user must be allowed to SET ROLE lifelog_app (PostgreSQL 16+ needs SET TRUE)
    BEGIN
        EXECUTE format('GRANT lifelog_app TO %I WITH SET TRUE, INHERIT FALSE', current_user);
    EXCEPTION WHEN OTHERS THEN
        BEGIN
            EXECUTE format('GRANT lifelog_app TO %I', current_user);
        EXCEPTION WHEN OTHERS THEN NULL;
        END;
    END;
    GRANT USAGE ON SCHEMA public TO lifelog_app;
    GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO lifelog_app;
    GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO lifelog_app;
    -- accounts: the app role can read who lives where (for the opt-in outage rescue map) but never emails,
    -- password hashes or session tokens; sign-in runs as the owner in lifelog/auth.py
    REVOKE ALL ON users FROM lifelog_app;
    GRANT SELECT (id, name, is_me, can_host, lat, lon, geom, synthetic, role, credentials) ON users TO lifelog_app;
    REVOKE ALL ON sessions FROM lifelog_app;
END $$;

CREATE OR REPLACE FUNCTION lifelog_uid() RETURNS INT LANGUAGE sql STABLE AS $$
    SELECT nullif(current_setting('lifelog.user_id', true), '')::int
$$;

ALTER TABLE items ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS own_items ON items;
CREATE POLICY own_items ON items USING (user_id = lifelog_uid()) WITH CHECK (user_id = lifelog_uid());

ALTER TABLE webhooks ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS own_webhooks ON webhooks;
CREATE POLICY own_webhooks ON webhooks USING (user_id = lifelog_uid()) WITH CHECK (user_id = lifelog_uid());

ALTER TABLE shares ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS own_shares ON shares;
CREATE POLICY own_shares ON shares USING (user_id = lifelog_uid()) WITH CHECK (user_id = lifelog_uid());

-- everything that hangs off an item is visible exactly when the item is (the subquery is itself filtered)
DO $$
DECLARE t TEXT;
BEGIN
    FOREACH t IN ARRAY ARRAY['alerts', 'receipts', 'devices', 'doses', 'ml_models'] LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
        EXECUTE format('DROP POLICY IF EXISTS via_item ON %I', t);
        EXECUTE format('CREATE POLICY via_item ON %I USING (EXISTS (SELECT 1 FROM items i WHERE i.id = %I.item_id))', t, t);
    END LOOP;
END $$;

-- caregiver links and power reports belong to one account
DO $$
DECLARE t TEXT;
BEGIN
    FOREACH t IN ARRAY ARRAY['power_reports'] LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
        EXECUTE format('DROP POLICY IF EXISTS own_rows ON %I', t);
        EXECUTE format('CREATE POLICY own_rows ON %I USING (user_id = lifelog_uid()) WITH CHECK (user_id = lifelog_uid())', t);
    END LOOP;
END $$;
ALTER TABLE care_links ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS own_links ON care_links;
CREATE POLICY own_links ON care_links USING (caregiver_id = lifelog_uid()) WITH CHECK (caregiver_id = lifelog_uid());
