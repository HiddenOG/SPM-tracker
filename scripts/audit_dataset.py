import sys
sys.path.insert(0, 'scripts')
from db import get_client
client = get_client()

orders = client.table('orders').select('*').order('notification_received_at').execute().data
spm = client.table('spm_purchase_orders').select('*').execute().data
links = client.table('spm_po_chevron_links').select('*').execute().data
groups = client.table('so_dispatch_groups').select('*').execute().data

print('=== DATASET SUMMARY ===')
print('Orders: ' + str(len(orders)))
print('SPM POs: ' + str(len(spm)))
print('Junction links: ' + str(len(links)))
print('Dispatch groups: ' + str(len(groups)))
print('')

# Consistency checks — flag anything that DOESN'T make sense
print('=== CONSISTENCY CHECKS ===')
issues = 0

for o in orders:
    po = o['buyer_po_number']
    # Check 1: acknowledged but no ack date (known-acceptable, just count)
    if o['acknowledgment_status'] == 'acknowledged' and not o['acknowledged_at']:
        pass  # known: acknowledged via inference, PDF never captured
    # Check 2: has spm_po_sent_at but no spm_po_number (inconsistent)
    if o.get('spm_po_sent_at') and not o.get('spm_po_number'):
        print('  ⚠️  ' + po + ': spm_po_sent_at set but spm_po_number null'); issues += 1
    # Check 3: sent_to_warehouse before notification (time-travel)
    if o.get('sent_to_warehouse_at') and o.get('notification_received_at'):
        if o['sent_to_warehouse_at'] < o['notification_received_at']:
            # Known-OK for revived POs; flag for awareness
            print('  ℹ️  ' + po + ': warehouse(' + str(o['sent_to_warehouse_at'])[:10] + ') before notif(' + str(o['notification_received_at'])[:10] + ') [revived PO?]')
    # Check 4: stock_check but not acknowledged
    if o.get('stock_check_completed_at') and o['acknowledgment_status'] != 'acknowledged':
        print('  ⚠️  ' + po + ': stock checked but not acknowledged'); issues += 1

# Check 5: SPM PO with sent but no SO ack (could be Felix/pending, informational)
sent_no_ack = [s['spm_po_ref'] for s in spm if s.get('sent_to_supplier_at') and not s.get('so_number')]
if sent_no_ack:
    print('  ℹ️  SPM POs sent but no SO ack yet: ' + ', '.join(sent_no_ack))

# Check 6: orphan junction links (link to non-existent order or spm)
order_ids = {o['id'] for o in orders}
spm_ids = {s['id'] for s in spm}
for l in links:
    if l.get('order_id') and l['order_id'] not in order_ids:
        print('  ⚠️  Orphan link: order_id not found'); issues += 1
    if l['spm_po_id'] not in spm_ids:
        print('  ⚠️  Orphan link: spm_po_id not found'); issues += 1

print('')
print('Hard inconsistencies found: ' + str(issues))
print('(ℹ️ items are informational/known-OK, not errors)')

# Stage completeness snapshot
print('')
print('=== STAGE COMPLETENESS (of ' + str(len(orders)) + ' orders) ===')
def count(pred): return sum(1 for o in orders if pred(o))
print('  notification_received: ' + str(count(lambda o: o.get('notification_received_at'))))
print('  acknowledged:          ' + str(count(lambda o: o['acknowledgment_status']=='acknowledged')))
print('    ...with ack date:    ' + str(count(lambda o: o.get('acknowledged_at'))))
print('  sent_to_warehouse:     ' + str(count(lambda o: o.get('sent_to_warehouse_at'))))
print('  stock_checked:         ' + str(count(lambda o: o.get('stock_check_completed_at'))))
print('  spm_po_sent:           ' + str(count(lambda o: o.get('spm_po_sent_at'))))
