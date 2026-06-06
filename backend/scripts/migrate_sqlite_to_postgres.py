from __future__ import annotations

import argparse
import sqlite3
from dataclasses import dataclass
from pathlib import Path
import sys
from typing import Any

import psycopg

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.core.config import AppConfig
from app.core.storage import PostgresStore


@dataclass(frozen=True)
class TablePlan:
    name: str
    scope: str
    conflict_columns: tuple[str, ...]


TABLE_PLANS: list[TablePlan] = [
    TablePlan('schema_versions', 'full', ('id',)),
    TablePlan('schema_proposals', 'full', ('proposal_id',)),
    TablePlan('schema_reviews', 'full', ('id',)),
    TablePlan('schema_activations', 'full', ('id',)),
    TablePlan('schema_approval_policies', 'full', ('policy_id',)),
    TablePlan('schema_field_risk_profiles', 'full', ('profile_id',)),
    TablePlan('schema_policy_audits', 'full', ('audit_id',)),
    TablePlan('web_page_cache', 'full', ('url',)),
    TablePlan('runs', 'run_id', ('run_id',)),
    TablePlan('events', 'run_id', ('id',)),
    TablePlan('evidence_raw_contents', 'run_id', ('content_hash',)),
    TablePlan('agent_runs', 'run_id', ('id',)),
    TablePlan('agent_io', 'run_id', ('id',)),
    TablePlan('run_checkpoints', 'run_id', ('id',)),
    TablePlan('manual_interventions', 'run_id', ('id',)),
    TablePlan('stage_handoffs', 'run_id', ('id',)),
    TablePlan('llm_calls', 'run_id', ('trace_id',)),
    TablePlan('subagent_runs', 'parent_run_id', ('subagent_id',)),
    TablePlan('run_conversations', 'run_id', ('conversation_id',)),
    TablePlan('conversation_turns', 'run_id', ('turn_id',)),
    TablePlan('conversation_messages', 'run_id', ('message_id',)),
    TablePlan('conversation_memory', 'run_id', ('conversation_id',)),
    TablePlan('report_revisions', 'run_id', ('revision_id',)),
    TablePlan('run_comparison_corpus_links', 'run_id', ('run_id', 'corpus_id', 'usage_type')),
    TablePlan('comparison_corpus_documents', 'corpus_id', ('source_url', 'content_hash')),
]

SERIAL_ID_TABLES = {
    'schema_versions',
    'schema_reviews',
    'schema_activations',
    'events',
    'agent_runs',
    'agent_io',
    'run_checkpoints',
    'manual_interventions',
    'stage_handoffs',
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='Migrate a SQLite sample into PostgreSQL for Competitor_Analysis.')
    parser.add_argument('--source', type=Path, help='Path to the source SQLite database.')
    parser.add_argument('--host', default='localhost')
    parser.add_argument('--port', type=int, default=5433)
    parser.add_argument('--user', default='postgres')
    parser.add_argument('--password', default='root')
    parser.add_argument('--database', default='competitor_analysis')
    parser.add_argument('--sample-runs', type=int, default=5, help='Number of latest runs to migrate with their dependent rows.')
    parser.add_argument('--include-tables', default='', help='Comma separated table allow-list.')
    parser.add_argument('--exclude-tables', default='', help='Comma separated table deny-list.')
    parser.add_argument('--dry-run', action='store_true', help='Only print planned counts without inserting.')
    return parser.parse_args()


def load_config(args: argparse.Namespace) -> AppConfig:
    return AppConfig(
        postgres_host=args.host,
        postgres_port=args.port,
        postgres_user=args.user,
        postgres_password=args.password,
        postgres_db=args.database,
        sqlite_path=str(args.source) if args.source else AppConfig().sqlite_path,
    )


def sqlite_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    rows = conn.execute(f'PRAGMA table_info({table})').fetchall()
    return [str(row[1]) for row in rows]


def csv_arg(raw: str) -> set[str]:
    return {item.strip() for item in raw.split(',') if item.strip()}


def select_run_ids(conn: sqlite3.Connection, sample_runs: int) -> list[str]:
    if sample_runs <= 0:
        return []
    rows = conn.execute(
        '''
        SELECT run_id
        FROM runs
        ORDER BY updated_at DESC
        LIMIT ?
        ''',
        (sample_runs,),
    ).fetchall()
    return [str(row[0]) for row in rows]


def build_scope_query(plan: TablePlan, run_ids: list[str], corpus_ids: list[str]) -> tuple[str, list[Any]]:
    sql = f'SELECT * FROM {plan.name}'
    params: list[Any] = []
    if plan.scope == 'run_id' and run_ids:
        placeholders = ', '.join('?' for _ in run_ids)
        sql += f' WHERE run_id IN ({placeholders})'
        params.extend(run_ids)
    elif plan.scope == 'parent_run_id' and run_ids:
        placeholders = ', '.join('?' for _ in run_ids)
        sql += f' WHERE parent_run_id IN ({placeholders})'
        params.extend(run_ids)
    elif plan.scope == 'corpus_id':
        if not corpus_ids:
            return sql + ' WHERE 1 = 0', []
        placeholders = ', '.join('?' for _ in corpus_ids)
        sql += f' WHERE corpus_id IN ({placeholders})'
        params.extend(corpus_ids)
    return sql, params


def fetch_source_count(conn: sqlite3.Connection, plan: TablePlan, run_ids: list[str], corpus_ids: list[str]) -> int:
    sql, params = build_scope_query(plan, run_ids, corpus_ids)
    count_sql = f'SELECT COUNT(*) FROM ({sql}) AS scoped_rows'
    row = conn.execute(count_sql, params).fetchone()
    return int(row[0] if row else 0)


def fetch_rows(conn: sqlite3.Connection, plan: TablePlan, run_ids: list[str], corpus_ids: list[str]) -> list[sqlite3.Row]:
    sql, params = build_scope_query(plan, run_ids, corpus_ids)
    return conn.execute(sql, params).fetchall()


def insert_rows(pg_conn: psycopg.Connection, table: str, columns: list[str], rows: list[sqlite3.Row], conflict_columns: tuple[str, ...]) -> int:
    if not rows:
        return 0
    placeholders = ', '.join(['%s'] * len(columns))
    column_list = ', '.join(columns)
    conflict = ', '.join(conflict_columns)
    sql = f'INSERT INTO {table} ({column_list}) VALUES ({placeholders}) ON CONFLICT ({conflict}) DO NOTHING'
    values = [tuple(row[column] for column in columns) for row in rows]
    with pg_conn.cursor() as cur:
        cur.executemany(sql, values)
    return len(rows)


def target_count(pg_conn: psycopg.Connection, plan: TablePlan, run_ids: list[str], corpus_ids: list[str]) -> int:
    sql = f'SELECT COUNT(*) FROM {plan.name}'
    params: list[Any] = []
    if plan.scope == 'run_id' and run_ids:
        placeholders = ', '.join(['%s'] * len(run_ids))
        sql += f' WHERE run_id IN ({placeholders})'
        params.extend(run_ids)
    elif plan.scope == 'parent_run_id' and run_ids:
        placeholders = ', '.join(['%s'] * len(run_ids))
        sql += f' WHERE parent_run_id IN ({placeholders})'
        params.extend(run_ids)
    elif plan.scope == 'corpus_id':
        if not corpus_ids:
            return 0
        placeholders = ', '.join(['%s'] * len(corpus_ids))
        sql += f' WHERE corpus_id IN ({placeholders})'
        params.extend(corpus_ids)
    with pg_conn.cursor() as cur:
        row = cur.execute(sql, params).fetchone()
    return int(row[0] if row else 0)


def reset_sequences(pg_conn: psycopg.Connection) -> None:
    with pg_conn.cursor() as cur:
        for table in sorted(SERIAL_ID_TABLES):
            cur.execute(
                '''
                SELECT setval(
                    pg_get_serial_sequence(%s, 'id'),
                    COALESCE((SELECT MAX(id) FROM {table_name}), 1),
                    true
                )
                '''.replace('{table_name}', table),
                (table,),
            )


def collect_corpus_ids(conn: sqlite3.Connection, run_ids: list[str]) -> list[str]:
    if not run_ids:
        return []
    placeholders = ', '.join('?' for _ in run_ids)
    rows = conn.execute(
        f'''
        SELECT DISTINCT corpus_id
        FROM run_comparison_corpus_links
        WHERE run_id IN ({placeholders})
        ORDER BY corpus_id ASC
        ''',
        run_ids,
    ).fetchall()
    return [str(row[0]) for row in rows]


def main() -> None:
    args = parse_args()
    include_tables = csv_arg(args.include_tables)
    exclude_tables = csv_arg(args.exclude_tables)
    config = load_config(args)
    source_path = args.source or config.sqlite_path_obj

    if not source_path.exists():
        raise FileNotFoundError(f'SQLite source not found: {source_path}')

    PostgresStore(config.postgres_dsn)

    with sqlite3.connect(source_path) as source_conn:
        source_conn.row_factory = sqlite3.Row
        run_ids = select_run_ids(source_conn, args.sample_runs)
        corpus_ids = collect_corpus_ids(source_conn, run_ids)

        plans = [
            plan for plan in TABLE_PLANS
            if (not include_tables or plan.name in include_tables) and plan.name not in exclude_tables
        ]

        with psycopg.connect(config.postgres_dsn) as pg_conn:
            print(f'Source SQLite: {source_path}')
            print(f'Target PostgreSQL: {config.postgres_user}@{config.postgres_host}:{config.postgres_port}/{config.postgres_db}')
            print(f'Selected run_ids ({len(run_ids)}): {run_ids}')
            print(f'Selected corpus_ids ({len(corpus_ids)}): {corpus_ids}')
            print('')

            for plan in plans:
                source_count = fetch_source_count(source_conn, plan, run_ids, corpus_ids)
                migrated_count = 0
                if not args.dry_run and source_count:
                    rows = fetch_rows(source_conn, plan, run_ids, corpus_ids)
                    columns = sqlite_columns(source_conn, plan.name)
                    migrated_count = insert_rows(pg_conn, plan.name, columns, rows, plan.conflict_columns)
                target_rows = target_count(pg_conn, plan, run_ids, corpus_ids)
                print(
                    f'{plan.name:<30} source={source_count:<6} migrated={migrated_count:<6} target={target_rows:<6} scope={plan.scope}'
                )

            if not args.dry_run:
                reset_sequences(pg_conn)
                pg_conn.commit()


if __name__ == '__main__':
    main()
