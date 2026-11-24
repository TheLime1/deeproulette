"""Offline normal-state codec. Caller must supply a proven roulette entity boundary.
No pattern search or current-round inference is performed here.
"""
MIN_TS=1767225600000
MAX_TS=2019686400000
PHASES=('bets','rolling','dolly','reward')
BET_CAPACITY=157  #157 concrete RouletteBetDescription static fields; confirm initializer for strict use.

def phase(b):
    return PHASES[b.ranged(0,3)] if b.read(1) else None

def removed_count(b,capacity):
    if not b.read(1):return 0
    return b.ranged(0,capacity if b.read(1) else 0x7fffffff)

def dictionary(b,capacity,key,value,diff):
    if capacity is None:raise ValueError('Runtime dictionary capacity has not been established')
    removals=[]
    if diff:
        n=removed_count(b,capacity)
        if n>100000:raise ValueError('Removal count unreasonable for offline decoder')
        removals=[key(b) for _ in range(n)]
    n=b.ranged(0,capacity)
    return {'removed':removals,'updated':[(key(b),value(b)) for _ in range(n)]}

def bets(b,character_capacity,diff):
    def character_dict(b):
        return dictionary(b,character_capacity,lambda q:q.ranged(-1,0xffffe),
                          lambda q:q.ranged(0,0xfffffffffffffff),diff)
    return dictionary(b,BET_CAPACITY,lambda q:q.ranged(0,BET_CAPACITY-1),character_dict,diff)

def roulette(b,*,diff=True,character_capacity=None):
    """Read a located RouletteData composite, returning only fields carried here.
    Missing fields in diff retain their prior values; this function does not track them.
    The schema has no round ID; caller must correlate ordered tick,entity,phase,deadline.
    """
    start=b.pos;out={}
    for name,read in [('bets',lambda q:bets(q,character_capacity,diff)),
                      ('winning_number',lambda q:q.ranged(-1,36)),
                      ('phase',phase),('phase_end_ts',lambda q:q.ranged(MIN_TS,MAX_TS))]:
        if not diff or b.read(1):out[name]=read(b)
    return dict(fields=out,start_bit=start,end_bit=b.pos)
