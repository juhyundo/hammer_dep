#!/bin/bash
# Test PostgreSQL connection with detailed diagnostics

set -e

echo "=========================================="
echo "PostgreSQL Connection Diagnostic Tool"
echo "=========================================="
echo ""

# Source venv if available
if [ -d ".venv" ]; then
    source .venv/bin/activate
fi

# Get connection string from airflow.cfg
if [ -f "airflow.cfg" ]; then
    CONN_STR=$(grep "^sql_alchemy_conn" airflow.cfg | cut -d'=' -f2 | xargs)
    echo "Connection string from airflow.cfg:"
    echo "  ${CONN_STR//:*@/:***@}"
    echo ""
else
    echo "ERROR: airflow.cfg not found"
    exit 1
fi

# Parse connection string
HOST=$(echo "$CONN_STR" | sed -n 's/.*@\([^:]*\):.*/\1/p')
PORT=$(echo "$CONN_STR" | sed -n 's/.*:\([0-9]*\)\/.*/\1/p')
DB=$(echo "$CONN_STR" | sed -n 's/.*\/\([^?]*\).*/\1/p')
USER=$(echo "$CONN_STR" | sed -n 's/.*:\/\/\([^:]*\):.*/\1/p')

echo "Parsed connection details:"
echo "  Host: $HOST"
echo "  Port: $PORT"
echo "  Database: $DB"
echo "  User: $USER"
echo ""

# Test 1: Network connectivity
echo "Test 1: Network connectivity to $HOST:$PORT"
if command -v nc >/dev/null 2>&1; then
    if nc -zv -w 5 "$HOST" "$PORT" 2>&1 | grep -q "succeeded"; then
        echo "Port is open"
    else
        echo "Port is not accessible"
        echo "Possible causes:"
        echo "  PostgreSQL server is down"
        echo "  Firewall is blocking the connection"
        echo "  Server is not listening on this port"
    fi
elif command -v telnet >/dev/null 2>&1; then
    timeout 3 telnet "$HOST" "$PORT" < /dev/null 2>&1 | head -1
else
    echo "Network test skipped because nc or telnet is not available"
fi
echo ""

# Test 2: DNS resolution
echo "Test 2: DNS resolution"
if host "$HOST" >/dev/null 2>&1; then
    IP=$(host "$HOST" | grep "has address" | head -1 | awk '{print $4}')
    echo "Host resolves to: $IP"
else
    echo "Cannot resolve hostname"
fi
echo ""

# Test 3: Python psycopg2 connection
echo "Test 3: PostgreSQL connection via psycopg2"
python3 << EOF
import sys
import psycopg2
from urllib.parse import urlparse

conn_str = "$CONN_STR"
parsed = urlparse(conn_str)

try:
    print("Attempting connection")
    conn = psycopg2.connect(
        host=parsed.hostname,
        port=parsed.port,
        database=parsed.path[1:],
        user=parsed.username,
        password=parsed.password,
        connect_timeout=10
    )
    print("Connection successful")
    
    cur = conn.cursor()
    cur.execute("SELECT version();")
    version = cur.fetchone()[0]
    print("PostgreSQL version:", version.split(",")[0])
    
    cur.execute("""
        SELECT COUNT(*)
        FROM information_schema.tables
        WHERE table_schema = 'public';
    """)
    table_count = cur.fetchone()[0]
    print("Tables in database:", table_count)
    
    cur.close()
    conn.close()
    sys.exit(0)
except psycopg2.OperationalError as e:
    print("Connection failed:", e)
    sys.exit(1)
except Exception as e:
    print("Error:", type(e).__name__, e)
    sys.exit(1)
EOF

CONN_RESULT=$?
echo ""

# Test 4: Airflow db check
if [ $CONN_RESULT -eq 0 ]; then
    echo "Test 4: Airflow database check"
    if command -v airflow >/dev/null 2>&1; then
        export AIRFLOW_HOME=$(pwd)
        if airflow db check >/dev/null 2>&1; then
            echo "Airflow can connect to database"
        else
            echo "Airflow connection check failed"
            airflow db check 2>&1 | head -5
        fi
    else
        echo "Airflow command not available"
    fi
fi

echo ""
echo "=========================================="
if [ $CONN_RESULT -eq 0 ]; then
    echo "All connection tests passed"
    echo "=========================================="
    exit 0
else
    echo "Connection tests failed"
    echo "=========================================="
    echo ""
    echo "Troubleshooting suggestions:"
    echo "1. Verify PostgreSQL server is running on $HOST:$PORT"
    echo "2. Check if you need to SSH into a compute node"
    echo "3. Verify network connectivity and firewall rules"
    echo "4. Check if database '$DB' exists and user '$USER' has access"
    echo "5. Try connecting from a different machine or node"
    exit 1
fi
