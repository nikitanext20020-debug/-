from utils.database import Database
db = Database()
with db.get_connection() as conn:
    cursor = conn.cursor()
    cursor.execute("DELETE FROM processed_posts")
    print(f"Deleted {cursor.rowcount} rows")
