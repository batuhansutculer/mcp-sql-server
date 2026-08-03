import sqlite3

connection = sqlite3.connect("business.db")
cursor = connection.cursor()

# --- Create tables ---

cursor.execute("""
    CREATE TABLE IF NOT EXISTS customers (
        id INTEGER PRIMARY KEY,
        name TEXT,
        email TEXT,
        city TEXT,
        signup_date TEXT
    )
""")

cursor.execute("""
    CREATE TABLE IF NOT EXISTS products (
        id INTEGER PRIMARY KEY,
        name TEXT,
        price REAL,
        category TEXT
    )
""")

cursor.execute("""
    CREATE TABLE IF NOT EXISTS orders (
        id INTEGER PRIMARY KEY,
        customer_id INTEGER,
        product_id INTEGER,
        quantity INTEGER,
        order_date TEXT
    )
""")

cursor.execute("""
    CREATE TABLE IF NOT EXISTS payment_methods (
        id INTEGER PRIMARY KEY,
        customer_id INTEGER,
        card_type TEXT,
        last_four TEXT
    )
""")

# --- Insert sample data ---

customers = [
    (1, "Anna Schmidt", "anna@example.com", "Berlin", "2024-01-15"),
    (2, "Luca Rossi", "luca@example.com", "Munich", "2024-02-20"),
    (3, "Marie Dubois", "marie@example.com", "Hamburg", "2024-03-10"),
    (4, "Tom Becker", "tom@example.com", "Berlin", "2024-05-05"),
]
cursor.executemany("INSERT OR IGNORE INTO customers VALUES (?, ?, ?, ?, ?)", customers)

products = [
    (1, "Standard Photo Shoot", 299.00, "Photography"),
    (2, "Premium Video Package", 899.00, "Video"),
    (3, "Drone Aerial Shots", 499.00, "Photography"),
    (4, "Floor Plan Scan", 149.00, "Scanning"),
]
cursor.executemany("INSERT OR IGNORE INTO products VALUES (?, ?, ?, ?)", products)

orders = [
    (1, 1, 1, 2, "2024-06-01"),
    (2, 1, 3, 1, "2024-06-15"),
    (3, 2, 2, 1, "2024-06-20"),
    (4, 3, 4, 3, "2024-07-01"),
    (5, 4, 1, 1, "2024-07-10"),
]
cursor.executemany("INSERT OR IGNORE INTO orders VALUES (?, ?, ?, ?, ?)", orders)

payment_methods = [
    (1, 1, "Visa", "4242"),
    (2, 2, "Mastercard", "5555"),
    (3, 3, "Visa", "1234"),
]
cursor.executemany("INSERT OR IGNORE INTO payment_methods VALUES (?, ?, ?, ?)", payment_methods)

connection.commit()
connection.close()

print("Database setup complete. All four tables created and populated.")