import duckdb
conn = duckdb.connect("polymarket.duckdb")
print(conn.execute("""
    SELECT 
        COUNT(*) FILTER (WHERE trade_was_correct) as correct,
        COUNT(*) FILTER (WHERE NOT trade_was_correct) as wrong,
        AVG(volume_usd) FILTER (WHERE trade_was_correct) as avg_vol_correct
    FROM flagged_trades
    WHERE flat_threshold_hit = true
""").df())