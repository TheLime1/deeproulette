"""Read short manual bet commands without blocking the Windows live console."""
import sys


class BetInput:
    def __init__(self):
        self.draft = ''
        self.round_id = None

    def emit(self, message):
        if self.draft:
            print()
        print(message, flush=True)
        if self.draft:
            print(f'> {self.draft}', end='', flush=True)

    def poll(self, ledger):
        if not sys.stdin.isatty():
            return
        import msvcrt

        if self.draft and self.round_id != ledger.round_id:
            self.draft = ''
            print('\nDraft cleared because the round changed.', flush=True)
        for _ in range(128):
            if not msvcrt.kbhit():
                break
            key = msvcrt.getwch()
            if key in ('\x00', '\xe0'):
                msvcrt.getwch()
            elif key == '\x03':
                raise KeyboardInterrupt
            elif key == '\r':
                if self.draft:
                    command, self.draft = self.draft, ''
                    print()
                    ledger.record(command, self.round_id)
            elif key == '\b':
                if self.draft:
                    self.draft = self.draft[:-1]
                    print('\b \b', end='', flush=True)
            elif key.isascii() and key.isprintable() and len(self.draft) < 128:
                if not self.draft:
                    self.round_id = ledger.round_id
                    print('> ', end='', flush=True)
                self.draft += key
                print(key, end='', flush=True)
