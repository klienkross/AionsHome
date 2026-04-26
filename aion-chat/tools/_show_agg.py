import sqlite3, sys
sys.stdout.reconfigure(encoding='utf-8')
conn = sqlite3.connect(r'D:\pyworks\AionsHome\aion-chat\data\chat.db')

print('=== 48 张聚合卡片 ===\n')
rows = conn.execute(
    "SELECT c.id, c.status, c.content, c.created_at, "
    "(SELECT COUNT(*) FROM memory_links WHERE to_id=c.id AND relation='aggregated_into') as member_count "
    "FROM memory_cards c WHERE c.type='aggregate' ORDER BY member_count DESC"
).fetchall()
for i, row in enumerate(rows):
    print(f'[{i+1:2d}] {row[1]:6s} | {row[4]:2d}张 | {row[2][:100]}')

print('\n=== 未被聚合的 open 卡片 ===\n')
indep = conn.execute(
    "SELECT type, COUNT(*) FROM memory_cards "
    "WHERE status='open' AND type != 'aggregate' "
    "AND id NOT IN (SELECT from_id FROM memory_links WHERE relation='aggregated_into') "
    "GROUP BY type ORDER BY COUNT(*) DESC"
).fetchall()
for row in indep:
    print(f'  {row[0]:12s} {row[1]}')

total = conn.execute(
    "SELECT COUNT(*) FROM memory_cards "
    "WHERE status='open' AND type != 'aggregate' "
    "AND id NOT IN (SELECT from_id FROM memory_links WHERE relation='aggregated_into')"
).fetchone()[0]
print(f'\n  独立 open 卡总计: {total}')

conn.close()
