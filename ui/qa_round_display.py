"""Present automatically collected rounds with the Manual Entry display."""
from ui.display import render_bet_advice, render_session_header, render_session_summary, render_spin, render_waiting
from utils.tracker import StatsTracker


class QARoundDisplay:
    def __init__(self, strategy, starting_balance, bet_per_number, keyboard):
        self.strategy = strategy
        self.starting_balance = starting_balance
        self.balance = starting_balance
        self.bet_per_number = bet_per_number
        self.keyboard = keyboard
        self.tracker = StatsTracker()
        self.rounds = 0
        self.advice = None

    def start(self, auto_train):
        render_session_header(self.strategy, self.balance, True, auto_train)

    def reset_advice(self):
        self.advice = None

    def show_advice(self, advice):
        self.advice = advice
        stakes = self.strategy.calculate_bets(advice['predicted'], self.balance, self.bet_per_number)
        self.keyboard.render(render_bet_advice, {**advice, 'bets': stakes, 'balance': self.balance,
                                                 'spin_num': self.rounds + 1, 'qa_live': True})
        self.keyboard.emit('  Waiting for the automatic result. Log any bets you placed, or leave it blank to skip.')

    def waiting(self, current, needed):
        if current:
            self.keyboard.render(render_waiting, current, needed)

    def result(self, row, closed_bets, streak):
        self.rounds += 1
        number = row['number']
        status = closed_bets['status'] if closed_bets else 'no_bet'
        advice = self.advice or {}
        predicted = advice.get('predicted', [])
        total_bet, net, is_win = 0.0, 0.0, False
        if status == 'recorded':
            stakes = {index: float(bet['amount']) for index, bet in enumerate(closed_bets['bets'])}
            total_bet = sum(stakes.values())
            returned = sum(stakes[index] * (bet['payout'] + 1) for index, bet in enumerate(closed_bets['bets'])
                           if number in bet['pockets'])
            net = round(returned - total_bet, 2)
            is_win = net > 0
            self.balance = round(self.balance + net, 2)
            self.tracker.record(number, predicted, stakes, is_win, net, self.balance)

        self.keyboard.render(render_spin, {
            'number': number, 'predicted': predicted, 'bet_label': advice.get('bet_label'),
            'total_bet': total_bet, 'is_win': is_win, 'net': net, 'balance': self.balance,
            'win_rate': self.tracker.win_rate, 'roi': self.tracker.roi, 'spin_num': self.rounds,
            'streak': self.tracker.streak(), 'qa_status': status, 'received_at': row['observed_at'],
            'phase': row['phase'] or 'unknown', 'qa_streak': streak['id'], 'qa_length': streak['length'],
        })
        self.reset_advice()

    def finish(self):
        render_session_summary(self.tracker, self.strategy, self.starting_balance, qa_rounds=self.rounds)
