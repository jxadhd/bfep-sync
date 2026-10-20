"""PostgreSQL access. The only module that imports psycopg2.

Connection settings come from the standard PGHOST, PGPORT, PGDATABASE, PGUSER
and PGPASSWORD environment variables, as before.
"""
from __future__ import annotations

from .providers import Figures, Provider
from .runner import Row

# Different from the website's add-campaign lock (42633701).
LOCK_ID = 42633702

SELECT = """
SELECT cf.id, cf.url, cf.currency_code, cf.goal_amount, cf.donated_amount,
       cf.consecutive_failures, cf.campaign_id
FROM campaign_fundraisers cf
JOIN campaigns c ON c.id = cf.campaign_id
WHERE cf.is_active AND cf.auto_sync AND c.deleted_at IS NULL
  AND cf.url ~* %s
ORDER BY cf.last_checked_at NULLS FIRST, cf.id
"""


class AlreadyRunning(Exception):
    pass


class PostgresStore:
    def __init__(self, conn):
        self.conn = conn
        self.cur = conn.cursor()

    @classmethod
    def connect(cls) -> 'PostgresStore':
        import psycopg2
        conn = psycopg2.connect(connect_timeout=10)
        conn.autocommit = False
        return cls(conn)

    def lock(self) -> None:
        """Stops two runs (cron overlap, a manual run) working at once."""
        self.cur.execute('SELECT pg_try_advisory_lock(%s)', (LOCK_ID,))
        got = self.cur.fetchone()[0]
        self.conn.commit()
        if not got:
            raise AlreadyRunning('another sync run holds the lock')

    def select(self, providers: list[Provider], limit: int) -> list[Row]:
        if not providers:
            return []
        pattern = '|'.join(f'({p.url_pattern})' for p in providers)
        sql = SELECT + (' LIMIT %s' if limit else '')
        self.cur.execute(sql, (pattern, limit) if limit else (pattern,))
        rows = [Row(*r) for r in self.cur.fetchall()]
        self.conn.rollback()
        return rows

    def record_ok(self, rid: int, fig: Figures) -> None:
        self.cur.execute(
            """UPDATE campaign_fundraisers
               SET donated_amount = %s, goal_amount = COALESCE(%s, goal_amount),
                   currency_code = %s, last_checked_at = now(), last_success_at = now(),
                   sync_status = 'ok', consecutive_failures = 0
               WHERE id = %s""",
            (fig.raised, fig.goal, fig.currency, rid))
        self.conn.commit()

    def record_problem(self, rid: int, status: str, failures: int) -> None:
        self.cur.execute(
            """UPDATE campaign_fundraisers
               SET last_checked_at = now(), sync_status = %s, consecutive_failures = %s
               WHERE id = %s""",
            (status, failures, rid))
        self.conn.commit()
        if status == 'not_found':
            self.turn_off(rid)

    def turn_off(self, rid: int) -> None:
        """Stops syncing a fundraiser whose page was not found NOT_FOUND_AFTER
        nights running; staff see it under the "page not found" data check.

        Needs UPDATE on auto_sync (grants.sql). Without it the status is still
        recorded and the run carries on, with a warning.
        """
        import psycopg2
        try:
            self.cur.execute('UPDATE campaign_fundraisers SET auto_sync = false WHERE id = %s', (rid,))
            self.conn.commit()
        except psycopg2.errors.InsufficientPrivilege:
            self.conn.rollback()
            print(f'{rid}: could not turn sync off (the database user needs UPDATE on auto_sync, see deploy/grants.sql)')
