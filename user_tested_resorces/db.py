import psycopg

from dotenv import load_dotenv
import os

load_dotenv()
DATABASE_URL = os.environ["DATABASE_URL"]   
try:
    with psycopg.connect(DATABASE_URL, connect_timeout=5) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT version(), current_database(), current_user")
            version, database, user = cur.fetchone()

            print("Connection successful!")
            print(f"Database: {database}")
            print(f"User: {user}")
            print(f"PostgreSQL: {version}")

except Exception as e:
    print(f"Connection failed: {e}")