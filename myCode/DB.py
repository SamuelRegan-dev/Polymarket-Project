import duckdb
'''
# Create a database file (or use in-memory with ':memory:')
conn = duckdb.connect('my_data.duckdb')

# Create a table for your API data
conn.execute("""
    CREATE TABLE IF NOT EXISTS market_data (
        timestamp TIMESTAMP,
        symbol VARCHAR,
        price DOUBLE,
        volume DOUBLE,
        -- add whatever other fields you need
    )
""")

# Insert data from a pandas DataFrame
import pandas as pd
df = pd.DataFrame({
    'timestamp': ['2025-02-08 10:00:00'],
    'symbol': ['AAPL'],
    'price': [150.25],
    'volume': [1000000]
})

conn.execute("INSERT INTO market_data SELECT * FROM df")

# Query it
result = conn.execute("SELECT * FROM market_data").df()
print(result)

# Close when done
conn.close()
'''