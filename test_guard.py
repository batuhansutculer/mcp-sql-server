from server import run_query

print(run_query("SELECT * FROM payment_methods"))
print(run_query("SELECT * FROM customers"))
print(run_query("DROP TABLE orders"))