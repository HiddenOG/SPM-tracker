import sys
sys.path.insert(0, 'scripts')
from db import get_client
get_client().table('sync_state').delete().eq('account', 'gmail_supplier').execute()
print('Reset gmail_supplier cursor')
