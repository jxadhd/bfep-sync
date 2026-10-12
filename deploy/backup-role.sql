-- Optional read-only role for "bfep backup". Run once as a superuser on the
-- database server, with your own password:
--   psql -v pw="'a-long-random-password'" -f backup-role.sql
-- pg_read_all_data (PostgreSQL 14+) can read every table, view and sequence
-- in every database, and write nothing.
CREATE ROLE bfep_backup LOGIN PASSWORD :pw;
GRANT pg_read_all_data TO bfep_backup;
