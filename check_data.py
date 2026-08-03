import sqlite3

connection = sqlite3.connect("business.db")
cursor = connection.cursor()

cursor.execute("""
    SELECT customers.name, products.name, orders.quantity
    FROM orders
    JOIN customers ON orders.customer_id = customers.id
    JOIN products ON orders.product_id = products.id
""")
rows = cursor.fetchall()

for row in rows:
    print(row)

connection.close()