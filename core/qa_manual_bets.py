"""Journal bets the user reports placing in the game."""
from decimal import Decimal, InvalidOperation

from config.settings import PAYOUT_RATIO
from robrun import utcnow
from streaks import stamp


BET_HELP = 'Type bet <selection> <amount>, then Enter. Examples: bet 17 100, bet red 100, bet col 2 100.'


def parse_bet(command):
    from strategies.outside_bets import OUTSIDE_CATEGORIES

    parts = command.strip().split()
    if len(parts) < 3 or parts[0].lower() != 'bet':
        raise ValueError(BET_HELP)
    selection = ' '.join(parts[1:-1])
    if selection.isascii() and selection.isdecimal() and 0 <= int(selection) <= 36:
        selection = str(int(selection))
        pockets, payout = [int(selection)], PAYOUT_RATIO
    else:
        for label, (numbers, payout) in OUTSIDE_CATEGORIES.items():
            if selection.casefold() == label.casefold():
                selection, pockets = label, sorted(numbers)
                break
        else:
            raise ValueError('Use a pocket from 0 to 36, red/black, odd/even, small/big, 1st/2nd/3rd 12, or col 1/2/3.')
    try:
        amount = Decimal(parts[-1])
    except InvalidOperation as error:
        raise ValueError('Enter a positive stake in game units.') from error
    if not amount.is_finite() or not 0 < amount <= 10**15:
        raise ValueError('Enter a positive stake of up to 1 quadrillion game units.')
    return {'selection': selection, 'amount': str(amount), 'pockets': pockets, 'payout': payout}


class ManualBetLog:
    def __init__(self, journal, emit, timeout_seconds):
        self.journal = journal
        self.emit = emit
        self.timeout_seconds = timeout_seconds
        self.anchor = None
        self.bets = []

    @property
    def round_id(self):
        return self.anchor['round_id'] if self.anchor else None

    def open_fast_start(self, at):
        self.anchor = {'round_id': 'fast-start', 'observed_at': at, 'source_id': None}

    def record(self, command, round_id, entered_at=None):
        if self.anchor is None or round_id != self.round_id:
            self.emit('Bet entry discarded because the displayed round changed or closed. Wait for the next result.')
            return
        entered_at = entered_at or utcnow()
        age = (stamp(entered_at) - stamp(self.anchor['observed_at'])).total_seconds()
        if not 0 <= age < self.timeout_seconds:
            self.emit('Bet entry window expired. Wait for the next result.')
            return
        try:
            bet = parse_bet(command)
        except ValueError as error:
            self.emit(str(error))
            return
        bet['entered_utc'] = entered_at
        self.journal.append('manual-bets.jsonl', {'event': 'bet_recorded', 'after_round_id': self.round_id, **bet})
        self.bets.append(bet)
        self.emit(f"  Manual bet recorded: {bet['selection']} | {bet['amount']} game units")

    def close(self, result=None, reason=None):
        if self.anchor is None:
            return
        if result is None:
            if not self.bets:
                self.anchor = None
                return
            reason = reason or 'no result available'
        opened_at = self.anchor['observed_at']
        entry = {'event': 'round_closed', 'after_round_id': self.round_id, 'opened_utc': opened_at, 'bets': self.bets}
        if result is not None:
            received = stamp(result['observed_at'])
            entry.update(result_round_id=result['round_id'], result_received_utc=result['observed_at'],
                         result=result['number'], result_quality=result['quality'])
            gap = (received - stamp(opened_at)).total_seconds()
            source = self.anchor['source_id']
            if (source is not None and result['source_id'] != source) or not 0 < gap < self.timeout_seconds:
                reason = 'result gap'
            elif any(stamp(bet['entered_utc']) >= received for bet in self.bets):
                reason = 'entry reached the app after result receipt'
        if reason:
            entry.update(status='unresolved', reason=reason)
            message = f'  Manual bet log: round unresolved ({reason}).'
        elif not self.bets:
            entry['status'] = 'skipped'
            message = '  Manual bet log: skipped (no bet entered).'
        else:
            matches = sum(result['number'] in bet['pockets'] for bet in self.bets)
            entry.update(status='recorded', matching_bets=matches)
            message = f'  Manual bet log: {matches}/{len(self.bets)} recorded bets matched the collected result.'
        self.journal.append('manual-bets.jsonl', entry)
        self.anchor = None
        self.bets = []
        self.emit(message)
        return entry

    def observe(self, row):
        closed = self.close(result=row)
        self.anchor = row
        return closed

    def expire(self, now):
        if self.anchor and (now - stamp(self.anchor['observed_at'])).total_seconds() >= self.timeout_seconds:
            self.close(reason='60-second result timeout')
