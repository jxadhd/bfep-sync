-- Keeps what the platform last showed for each fundraiser, and why the sync
-- did not update it, so staff can see a held figure (suspicious_drop) on the
-- site and accept it in one step. Run once, as the schema owner (data):
--   psql -d <your database> -v ON_ERROR_STOP=1 -f sync-details.sql
-- Safe to run again. Until it is run the sync works as before and only
-- prints these figures in its log.

ALTER TABLE public.campaign_fundraisers
  ADD COLUMN IF NOT EXISTS platform_raised numeric,
  ADD COLUMN IF NOT EXISTS platform_goal numeric,
  ADD COLUMN IF NOT EXISTS platform_currency varchar(3),
  ADD COLUMN IF NOT EXISTS platform_donations integer,
  ADD COLUMN IF NOT EXISTS platform_checked_at timestamptz,
  ADD COLUMN IF NOT EXISTS sync_detail text;

COMMENT ON COLUMN public.campaign_fundraisers.platform_raised IS
  'Raised amount the platform showed at platform_checked_at (written by the sync, even when it held the figure)';
COMMENT ON COLUMN public.campaign_fundraisers.sync_detail IS
  'Why the sync did not update this fundraiser on its last check; empty when it did';

DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'bfep_sync') THEN
    -- bfep sync problems and bfep sync accept read the status and the
    -- campaign's line number; the nightly run writes the new columns.
    GRANT SELECT (line_number) ON public.campaigns TO bfep_sync;
    GRANT SELECT (sync_status, last_success_at, platform_raised, platform_goal,
                  platform_currency, platform_donations, platform_checked_at, sync_detail)
      ON public.campaign_fundraisers TO bfep_sync;
    GRANT UPDATE (platform_raised, platform_goal, platform_currency,
                  platform_donations, platform_checked_at, sync_detail)
      ON public.campaign_fundraisers TO bfep_sync;
  END IF;
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'bfep_app') THEN
    -- The website shows the held figures and its "use these figures" button
    -- copies them over the stored ones.
    GRANT SELECT (platform_raised, platform_goal, platform_currency,
                  platform_donations, platform_checked_at, sync_detail)
      ON public.campaign_fundraisers TO bfep_app;
    GRANT UPDATE (sync_detail) ON public.campaign_fundraisers TO bfep_app;
  END IF;
END $$;
