-- Optional: a dedicated, least-privilege database role for the sync.
-- Run as the schema owner (data) or a superuser:
--   psql -d <your database> -v ON_ERROR_STOP=1 -f grants.sql
--   psql -d <your database> -c '\password bfep_sync'
-- It can read only the columns the sync selects and update only the columns
-- it writes. It cannot insert, delete, or see any other table.
-- Tables are assumed to be in the public schema; change it below if not.

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'bfep_sync') THEN
    CREATE ROLE bfep_sync LOGIN;
  END IF;
  EXECUTE format('GRANT CONNECT ON DATABASE %I TO bfep_sync', current_database());
END $$;

GRANT USAGE ON SCHEMA public TO bfep_sync;

GRANT SELECT (id, deleted_at) ON public.campaigns TO bfep_sync;

GRANT SELECT (id, campaign_id, url, currency_code, goal_amount, donated_amount,
              is_active, auto_sync, last_checked_at, consecutive_failures)
  ON public.campaign_fundraisers TO bfep_sync;

-- auto_sync: turned off after a page is not found on 3 runs in a row.
GRANT UPDATE (donated_amount, goal_amount, currency_code, last_checked_at,
              last_success_at, sync_status, consecutive_failures, auto_sync)
  ON public.campaign_fundraisers TO bfep_sync;
