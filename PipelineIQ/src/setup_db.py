import os
import sys
import csv
import sqlite3

# Make emoji-rich output safe on Windows consoles (cp1252 can't encode them).
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

SRC_DIR = os.path.dirname(os.path.abspath(__file__))
BASE_DIR = os.path.dirname(SRC_DIR)
DB_PATH = os.path.join(BASE_DIR, "data", "source_warehouse.db")
SOURCES_DIR = os.path.join(BASE_DIR, "data", "sources")

def create_mock_data():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # Create Users Table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            name TEXT,
            email TEXT,
            signup_date DATE
        )
    ''')

    # Create Orders Table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS orders (
            order_id INTEGER PRIMARY KEY,
            user_id INTEGER,
            amount REAL,
            order_date DATE,
            FOREIGN KEY(user_id) REFERENCES users(user_id)
        )
    ''')

    # Insert Sample Data safely
    cursor.executemany("INSERT OR IGNORE INTO users VALUES (?,?,?,?)", [
        (1, 'Alice Smith', 'alice@example.com', '2026-01-15'),
        (2, 'Bob Jones', 'bob@example.com', '2026-02-20'),
        (3, 'Charlie Brown', 'charlie@example.com', '2026-03-05')
    ])

    cursor.executemany("INSERT OR IGNORE INTO orders VALUES (?,?,?,?)", [
        (101, 1, 150.50, '2026-05-01'),
        (102, 2, 99.00, '2026-05-02'),
        (103, 1, 45.25, '2026-05-03'),
        (104, 3, 210.00, '2026-05-04')
    ])

    conn.commit()
    conn.close()
    print(f"✅ Mock source database created successfully at '{DB_PATH}'")


def create_sample_source_file():
    """Seed an external source file so the 'load from a file' path is demoable."""
    os.makedirs(SOURCES_DIR, exist_ok=True)
    products_path = os.path.join(SOURCES_DIR, "products.csv")

    # order_id links back to the orders table so the AI can join file → DB tables.
    rows = [
        ("order_id", "product_name", "category", "unit_price"),
        (101, "Wireless Mouse", "Electronics", 29.99),
        (102, "Coffee Mug", "Kitchen", 12.50),
        (103, "Notebook", "Stationery", 5.25),
        (104, "Desk Lamp", "Home", 34.00),
    ]
    with open(products_path, "w", newline="", encoding="utf-8") as f:
        csv.writer(f).writerows(rows)

    print(f"✅ Sample source file created at '{products_path}'")


if __name__ == '__main__':
    create_mock_data()
    create_sample_source_file()