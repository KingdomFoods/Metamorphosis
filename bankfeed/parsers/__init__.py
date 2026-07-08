"""parsers — turn a bank's downloaded statement file into the canonical shape.

For backfill (and for banks with no live feed), we parse the same .xls/.csv the
user exports from net-banking. Each parser returns:
    {"account": {"number","last4","bank"}, "transactions": [{date,narration,ref,withdrawal,deposit,balance}]}
which the poster/MockConnector already understand.
"""
