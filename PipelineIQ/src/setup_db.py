import sqlite3

def create_mock_data():
    conn = sqlite3.connect('source_warehouse.db')
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
    print("✅ Mock source database created successfully as 'source_warehouse.db'")

if __name__ == '__main__':
    create_mock_data()