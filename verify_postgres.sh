#!/bin/bash
# Verify PostgreSQL database connection and schema for Airflow
# This script checks the health and status of the PostgreSQL database

set -e

# --- Set Airflow home directory (only if not already set) ---
if [ -z "$AIRFLOW_HOME" ]; then
    export AIRFLOW_HOME=$(pwd)
    echo "AIRFLOW_HOME directory set to: $AIRFLOW_HOME"
else
    echo "AIRFLOW_HOME already set to: $AIRFLOW_HOME"
fi

# --- Check if virtual environment is activated ---
if [ -z "$VIRTUAL_ENV" ]; then
    if [ -d "$AIRFLOW_HOME/.venv" ]; then
        echo "Activating virtual environment"
        source "$AIRFLOW_HOME/.venv/bin/activate"
    else
        echo "WARNING: No virtual environment detected. Make sure Airflow is installed."
    fi
else
    echo "Virtual environment already active: $VIRTUAL_ENV"
fi

# --- Display connection information ---
echo "=========================================="
echo "PostgreSQL Database Verification"
echo "=========================================="
echo ""

echo "Connection Configuration:"
CONN_STRING=$(grep "^sql_alchemy_conn" "$AIRFLOW_HOME/airflow.cfg" 2>/dev/null | cut -d'=' -f2 | xargs)
if [ -n "$CONN_STRING" ]; then
    MASKED_CONN=$(echo "$CONN_STRING" | sed 's/:[^@]*@/:***@/')
    echo "  Connection: $MASKED_CONN"
else
    echo "  WARNING: Connection string not found in airflow.cfg"
fi
echo ""

# --- Test 1: Basic connection ---
echo "Test 1: Basic Database Connection"
echo "-----------------------------------"
if airflow db check > /dev/null 2>&1; then
    echo "Connection successful"
    CONNECTION_OK=true
else
    echo "Connection failed"
    echo ""
    echo "Error details:"
    airflow db check 2>&1 | head -10
    CONNECTION_OK=false
fi
echo ""

if [ "$CONNECTION_OK" = false ]; then
    echo "=========================================="
    echo "Verification failed: Cannot connect to database"
    echo "=========================================="
    exit 1
fi

# --- Test 2: Schema/Migration status ---
echo "Test 2: Database Schema Status"
echo "-----------------------------------"
MIGRATION_OUTPUT=$(airflow db check-migrations 2>&1 || true)

if echo "$MIGRATION_OUTPUT" | grep -q "All migrations have been applied"; then
    echo "All migrations applied. Schema is up to date"
    SCHEMA_OK=true
elif echo "$MIGRATION_OUTPUT" | grep -q "Migration Head(s) in DB: set()"; then
    echo "Database is empty. Schema not initialized"
    echo "Run ./init_postgres_db.sh to initialize the database"
    SCHEMA_OK=false
elif echo "$MIGRATION_OUTPUT" | grep -q "There are still unapplied migrations"; then
    echo "Migrations pending. Schema needs update"
    echo "Run airflow db migrate to apply migrations"
    SCHEMA_OK=false
else
    echo "Unknown migration state"
    echo "$MIGRATION_OUTPUT" | head -5
    SCHEMA_OK=false
fi
echo ""

# --- Test 3: Query database tables (if schema exists) ---
if [ "$SCHEMA_OK" = true ]; then
    echo "Test 3: Database Tables"
    echo "-----------------------------------"
    python3 << 'PYTHON_SCRIPT'
import os
from sqlalchemy import create_engine, inspect
from airflow.configuration import conf

try:
    conn_string = conf.get('database', 'sql_alchemy_conn')
    engine = create_engine(conn_string)
    inspector = inspect(engine)
    tables = inspector.get_table_names()

    airflow_tables = [
        t for t in tables
        if t.startswith('dag')
        or t.startswith('task')
        or t.startswith('job')
        or t.startswith('log')
        or t.startswith('xcom')
        or t.startswith('variable')
        or t.startswith('connection')
    ]

    print(f"Found {len(tables)} total tables")
    print(f"Airflow metadata tables: {len(airflow_tables)}")

    if len(airflow_tables) > 0:
        print("Sample tables:", ", ".join(airflow_tables[:5]))
        if len(airflow_tables) > 5:
            print(f"And {len(airflow_tables) - 5} more")
    else:
        print("WARNING: No Airflow metadata tables found")

except Exception as e:
    print("Error querying tables:", e)
PYTHON_SCRIPT
    echo ""
fi

# --- Summary ---
echo "=========================================="
if [ "$CONNECTION_OK" = true ] && [ "$SCHEMA_OK" = true ]; then
    echo "Verification passed"
    echo "PostgreSQL database is ready for Airflow"
    echo "=========================================="
    exit 0
else
    echo "Verification completed with warnings"
    if [ "$SCHEMA_OK" = false ]; then
        echo "Action required: Initialize or migrate database schema"
    fi
    echo "=========================================="
    exit 1
fi
